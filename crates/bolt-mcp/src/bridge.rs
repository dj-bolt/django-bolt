//! Actix <-> rmcp HTTP bridge.
//!
//! actix-http 3 speaks `http` 0.2 while rmcp speaks `http` 1.x, so the
//! request is rebuilt from bytes (method/uri/header copies — cheap for MCP
//! traffic) rather than converted. Tier-1 auth (Rust JWT/API-key + guards)
//! and the Tier-2 OAuth hook run BEFORE rmcp sees the request; the resolved
//! principal rides the request extensions into every handler method.

use actix_web::{web, HttpRequest, HttpResponse};
use bytes::Bytes;
use futures_util::StreamExt;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::{McpAuthExt, McpMount};
use bolt_core::middleware::auth::AuthContext;
use bolt_core::request_pipeline::extract_headers;
use bolt_core::responses;
use bolt_core::state::AppState;
use bolt_core::validation::{cookie_csrf_blocks, validate_auth_and_guards, AuthGuardResult};

/// Buffer the request body, enforcing the payload cap. Mirrors the bounded
/// read loop used for regular routes; rmcp enforces its own configured cap as
/// defense in depth.
async fn read_body(
    payload: &mut web::Payload,
    max_payload_size: usize,
) -> Result<Bytes, HttpResponse> {
    let mut body = web::BytesMut::new();
    while let Some(chunk) = payload.next().await {
        let chunk = match chunk {
            Ok(chunk) => chunk,
            Err(_) => return Err(HttpResponse::BadRequest().finish()),
        };
        if body.len() + chunk.len() > max_payload_size {
            return Err(HttpResponse::PayloadTooLarge().finish());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body.freeze())
}

/// Build an `AuthContext` from the principal dict a Python `verify_token`
/// returns: {"user_id": str|None, "is_staff": bool, "is_superuser": bool,
/// "permissions": [str], "claims_json": str|None, "backend": str}.
/// All keys must be present (registration-time-guarantee rule applies to the
/// verifier's contract too).
fn auth_context_from_principal(dict: &Bound<'_, PyDict>) -> PyResult<AuthContext> {
    use pyo3::exceptions::PyValueError;
    fn item<'py>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, pyo3::PyAny>> {
        dict.get_item(key)?
            .ok_or_else(|| PyValueError::new_err(format!("verify_token principal missing '{key}'")))
    }
    let claims_json: Option<String> = item(dict, "claims_json")?.extract()?;
    let claims = match claims_json {
        Some(raw) => Some(serde_json::from_str(&raw).map_err(|e| {
            PyValueError::new_err(format!("verify_token principal 'claims_json': {e}"))
        })?),
        None => None,
    };
    let permissions: Vec<String> = item(dict, "permissions")?.extract()?;
    Ok(AuthContext {
        user_id: item(dict, "user_id")?.extract()?,
        is_staff: item(dict, "is_staff")?.extract()?,
        is_superuser: item(dict, "is_superuser")?.extract()?,
        backend: item(dict, "backend")?.extract()?,
        claims,
        permissions: permissions.into_iter().collect(),
        cookie_csrf: false,
    })
}

/// 401 with the mount's OAuth challenge (RFC 9728 resource-metadata pointer).
fn oauth_401(www_authenticate: &str) -> HttpResponse {
    HttpResponse::Unauthorized()
        .insert_header(("WWW-Authenticate", www_authenticate))
        .insert_header(("Content-Type", "application/json"))
        .body(r#"{"detail":"Unauthorized"}"#)
}

pub async fn handle_mcp_request(
    req: HttpRequest,
    mut payload: web::Payload,
    mount: &McpMount,
    state: &AppState,
) -> HttpResponse {
    let headers = match extract_headers(&req, state.max_header_size) {
        Ok(headers) => headers,
        Err(response) => return response,
    };
    let method = req.method().as_str().to_owned();

    // ---- Tier-1 auth + mount guards (Rust, before rmcp) ----
    let mut auth_ctx: Option<AuthContext> =
        match validate_auth_and_guards(&headers, &mount.auth_backends, &mount.guards) {
            AuthGuardResult::Allow(ctx) => {
                if ctx.as_ref().is_some_and(|auth| auth.cookie_csrf) {
                    let scheme = req.connection_info().scheme().to_owned();
                    if cookie_csrf_blocks(&method, &headers, true, &scheme) {
                        return responses::error_403();
                    }
                }
                ctx
            }
            AuthGuardResult::Unauthorized => return responses::error_401(),
            AuthGuardResult::Forbidden(denial) => {
                return responses::error_403_denial(denial.as_deref())
            }
        };

    // ---- Tier-2 OAuth resource server (Python verify_token) ----
    if let Some(oauth) = &mount.oauth {
        let bearer = headers
            .get("authorization")
            .and_then(|value| {
                let (scheme, token) = value.split_once(' ')?;
                (scheme.eq_ignore_ascii_case("bearer") && !token.is_empty()).then_some(token)
            })
            .map(str::to_owned);
        let Some(token) = bearer else {
            return oauth_401(&oauth.www_authenticate);
        };
        let principal = Python::attach(|py| -> PyResult<Option<AuthContext>> {
            let result = oauth.verify_token.call1(py, (token,))?;
            let bound = result.bind(py);
            if bound.is_none() {
                return Ok(None);
            }
            let dict = bound.cast::<PyDict>().map_err(|_| {
                pyo3::exceptions::PyValueError::new_err("verify_token must return a dict or None")
            })?;
            Ok(Some(auth_context_from_principal(dict)?))
        });
        match principal {
            Ok(Some(ctx)) => auth_ctx = Some(ctx),
            Ok(None) => return oauth_401(&oauth.www_authenticate),
            Err(err) => {
                log::error!("MCP verify_token raised: {err}");
                return HttpResponse::InternalServerError().finish();
            }
        }
    }

    // ---- Rebuild as http 1.x request ----
    let body = match read_body(&mut payload, state.max_payload_size).await {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut builder = http::Request::builder()
        .method(match http::Method::from_bytes(method.as_bytes()) {
            Ok(m) => m,
            Err(_) => return HttpResponse::MethodNotAllowed().finish(),
        })
        .uri(match req.uri().to_string().parse::<http::Uri>() {
            Ok(uri) => uri,
            Err(_) => return HttpResponse::BadRequest().finish(),
        });
    for (name, value) in req.headers() {
        builder = builder.header(name.as_str(), value.as_bytes());
    }
    let mut request = match builder.body(http_body_util::Full::new(body)) {
        Ok(request) => request,
        Err(err) => {
            log::error!("MCP request rebuild failed: {err}");
            return HttpResponse::BadRequest().finish();
        }
    };
    request
        .extensions_mut()
        .insert(McpAuthExt(auth_ctx.map(std::sync::Arc::new)));

    // ---- Serve via rmcp ----
    let response = mount.service.handle(request).await;

    // ---- Convert response back to actix ----
    let (parts, body) = response.into_parts();
    let mut builder = HttpResponse::build(
        actix_web::http::StatusCode::from_u16(parts.status.as_u16())
            .unwrap_or(actix_web::http::StatusCode::INTERNAL_SERVER_ERROR),
    );
    let mut is_sse = false;
    for (name, value) in &parts.headers {
        if name == http::header::CONTENT_TYPE {
            if let Ok(v) = value.to_str() {
                if v.starts_with("text/event-stream") {
                    is_sse = true;
                }
            }
        }
        if let Ok(value) = value.to_str() {
            builder.append_header((name.as_str(), value));
        }
    }
    if is_sse {
        // Identity marker tells the global compression middleware to skip —
        // buffering compressors would break per-event SSE delivery. Same
        // mechanism as Bolt's own streaming responses.
        builder.insert_header(("Content-Encoding", "identity"));
        builder.append_header(("Vary", "Accept-Encoding"));
    }
    let stream = http_body_util::BodyStream::new(body).filter_map(|frame| async move {
        match frame {
            Ok(frame) => frame
                .into_data()
                .ok()
                .map(Ok::<Bytes, std::convert::Infallible>),
            Err(never) => match never {},
        }
    });
    builder.streaming(stream)
}
