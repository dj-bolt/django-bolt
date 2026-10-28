//! Give the framework error responses to the error formatter of the app.
//!
//! The builders of the error responses put an `ErrorInfo` in the response
//! extensions. When the app sets `BoltAPI(error_formatter=...)`, the handler
//! calls `format_error_response` for each response with a status of 400 or
//! more. A response with no `ErrorInfo` comes from a handler, and stays as it is.
//! This code runs on error paths only.

use actix_web::http::StatusCode;
use actix_web::http::header::{CONTENT_LENGTH, CONTENT_TYPE, HeaderName, HeaderValue};
use actix_web::{HttpRequest, HttpResponse};
use ahash::AHashMap;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;

use bolt_core::error::ErrorInfo;
use bolt_core::middleware::client_ip;
use bolt_core::request::PyRequest;
use bolt_core::request_pipeline::insert_header_line;
use bolt_core::state::AppState;
use bolt_core::type_coercion::string_map_to_py_dict;

use crate::handler::SKIP_CORS_HEADER_NAME;

/// The same limit as `extract_headers`.
const MAX_HEADERS: usize = 100;

/// The response of the formatter for a framework error, or `response` itself.
///
/// The formatter does not get `response` when it has no `ErrorInfo`. When the
/// formatter fails, Python logs the failure and the default response goes out.
#[cold]
#[inline(never)]
pub fn format_error_response(
    mut response: HttpResponse,
    req: &HttpRequest,
    state: &Arc<AppState>,
) -> HttpResponse {
    let Some(renderer) = state.error_renderer.as_ref() else {
        return response;
    };
    let Some(info) = response.extensions_mut().remove::<ErrorInfo>() else {
        return response;
    };
    let formatted = Python::attach(|py| -> PyResult<Option<HttpResponse>> {
        let request = Py::new(py, error_request(py, req, state)?)?;
        // The skip-CORS marker is internal. The CORS middleware also finds the skip from the route.
        let headers: Vec<(&str, &str)> = response
            .headers()
            .iter()
            .filter(|(name, _)| {
                **name != CONTENT_TYPE
                    && **name != CONTENT_LENGTH
                    && **name != SKIP_CORS_HEADER_NAME
            })
            .filter_map(|(name, value)| Some((name.as_str(), value.to_str().ok()?)))
            .collect();
        let status = response.status().as_u16();
        let result = match &info {
            ErrorInfo::Core {
                kind,
                detail,
                errors,
                extra,
                reason,
            } => renderer.call1(
                py,
                (
                    status,
                    *kind,
                    detail.as_ref(),
                    json_bytes(py, errors.as_ref()),
                    json_bytes(py, extra.as_ref()),
                    headers,
                    request,
                    py.None(),
                    reason.as_deref(),
                ),
            )?,
            ErrorInfo::Exception(exc) => renderer.call1(
                py,
                (
                    status,
                    "",
                    "",
                    py.None(),
                    py.None(),
                    headers,
                    request,
                    exc.clone_ref(py),
                    py.None(),
                ),
            )?,
        };
        if result.is_none(py) {
            return Ok(None);
        }
        let (status, headers, body): (u16, Vec<(String, String)>, Vec<u8>) = result.extract(py)?;
        // Python checks the result. A wrong one here sends the default response.
        let status = StatusCode::from_u16(status)
            .ok()
            .filter(|status| status.is_client_error() || status.is_server_error())
            .ok_or_else(|| PyValueError::new_err(format!("status {status} is not an error")))?;
        let mut builder = HttpResponse::build(status);
        for (name, value) in headers {
            let name =
                HeaderName::try_from(name).map_err(|err| PyValueError::new_err(err.to_string()))?;
            let value = HeaderValue::try_from(value)
                .map_err(|err| PyValueError::new_err(err.to_string()))?;
            builder.append_header((name, value));
        }
        Ok(Some(builder.body(body)))
    });
    match formatted {
        Ok(Some(formatted)) => formatted,
        Ok(None) => response,
        Err(err) => {
            // The Python renderer catches the errors of the formatter, so this
            // is a wrong result. Report it, and send the default response.
            eprintln!("[django-bolt] Warning: error_formatter result is not valid: {err}");
            response
        }
    }
}

fn json_bytes<'py>(
    py: Python<'py>,
    value: Option<&serde_json::Value>,
) -> Option<Bound<'py, PyBytes>> {
    value.map(|value| {
        PyBytes::new(
            py,
            &serde_json::to_vec(value).expect("a JSON value always serializes"),
        )
    })
}

/// The request for the formatter: method, path, query string and headers.
///
/// The Rust core can reject a request before the request object exists, so
/// this builds one. Headers over the size limit are left out.
fn error_request(py: Python<'_>, req: &HttpRequest, state: &AppState) -> PyResult<PyRequest> {
    let mut headers: AHashMap<String, String> = AHashMap::with_capacity(16);
    for (name, value) in req.headers().iter().take(MAX_HEADERS) {
        if let Ok(value) = value.to_str() {
            if value.len() <= state.max_header_size {
                insert_header_line(&mut headers, name.as_str(), value);
            }
        }
    }
    let conn_host = headers.get("host").cloned().unwrap_or_default();
    let peer_ip = req.peer_addr().map(|address| address.ip());
    // Same trust rule as a handler request: only a trusted proxy sets the
    // scheme with `X-Forwarded-Proto`.
    let conn_scheme = client_ip::resolve_scheme(
        req.headers(),
        peer_ip,
        req.app_config().secure(),
        &state.trusted_proxies,
    );
    let conn_remote_addr =
        client_ip::resolve_from_headers(req.headers(), peer_ip, &state.trusted_proxies);
    let headers_py = string_map_to_py_dict(py, &headers, &[])?.unbind();
    Ok(PyRequest {
        method: req.method().as_str().to_owned().into(),
        path: req.path().to_owned(),
        body: Vec::new(),
        path_params: None,
        query_params: None,
        query_string: req.query_string().to_owned(),
        headers: Some(headers_py),
        cookies: None,
        raw_headers: None,
        raw_cookies: None,
        context: None,
        user: None,
        state: std::sync::OnceLock::new(),
        form_map: None,
        files_map: None,
        meta_cache: std::sync::OnceLock::new(),
        conn_host,
        conn_scheme,
        conn_remote_addr,
    })
}
