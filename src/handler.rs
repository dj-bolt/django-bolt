use actix_multipart::Multipart;
use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::{http::StatusCode, web, HttpRequest, HttpResponse};
use ahash::AHashMap;
use bytes::Bytes;
use futures_util::stream;
use futures_util::StreamExt;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::collections::HashMap;
use std::io::ErrorKind;
use std::sync::Arc;
use tokio::fs::File;
use tokio::io::AsyncReadExt;

use crate::asgi_http;
use crate::error;
use crate::form_parsing::{
    parse_multipart, parse_urlencoded, FileContent, FileFieldConstraints, FileInfo,
    FormParseResult, ValidationError, DEFAULT_MAX_PARTS, DEFAULT_MEMORY_LIMIT,
};
use crate::metadata::{RustArgBinding, RustArgSource};
use crate::middleware;
use crate::middleware::auth::populate_auth_context;
use crate::request::PyRequest;
use crate::request_pipeline::validate_and_cache_typed_params;
use crate::response_builder;
use crate::response_meta::ResponseMeta;
use crate::responses;
use crate::router::parse_query_string;
use crate::state::{find_asgi_mount, AppState, GLOBAL_ROUTER, ROUTE_METADATA, TASK_LOCALS};
use crate::streaming::{create_python_stream, create_sse_stream};
use crate::type_coercion::{params_to_py_dict, CoercedValue};
use crate::validation::{parse_cookies_inline, validate_auth_and_guards, AuthGuardResult};

use std::future::Future;
use std::pin::Pin;

/// Result of the unified dispatch block.
/// Sync-eligible routes return Ready (no async bridge overhead).
/// Async routes return Pending (existing coroutine + future path).
enum DispatchOutcome {
    Ready(HttpResponse),
    /// Sync dispatch completed but body requires async post-processing (stream/file).
    /// Carries the already-parsed wire to avoid re-acquiring GIL and re-parsing.
    SyncResult(ParsedResponseWire),
    Pending(Pin<Box<dyn Future<Output = PyResult<Py<PyAny>>> + Send>>),
}

// Cache Python classes for type construction (avoids repeated imports)
static UUID_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATETIME_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static STREAMING_RESPONSE_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
const SKIP_CORS_HEADER_NAME: HeaderName = HeaderName::from_static("x-bolt-skip-cors");
const SKIP_CORS_HEADER_VALUE: HeaderValue = HeaderValue::from_static("true");

fn get_streaming_response_class(py: Python<'_>) -> &Py<PyAny> {
    STREAMING_RESPONSE_CLASS.get_or_init(py, || {
        py.import("django_bolt.responses")
            .unwrap()
            .getattr("StreamingResponse")
            .unwrap()
            .unbind()
    })
}

fn get_uuid_class(py: Python<'_>) -> &Py<PyAny> {
    UUID_CLASS.get_or_init(py, || {
        py.import("uuid").unwrap().getattr("UUID").unwrap().unbind()
    })
}

fn get_decimal_class(py: Python<'_>) -> &Py<PyAny> {
    DECIMAL_CLASS.get_or_init(py, || {
        py.import("decimal")
            .unwrap()
            .getattr("Decimal")
            .unwrap()
            .unbind()
    })
}

fn get_datetime_class(py: Python<'_>) -> &Py<PyAny> {
    DATETIME_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("datetime")
            .unwrap()
            .unbind()
    })
}

fn get_date_class(py: Python<'_>) -> &Py<PyAny> {
    DATE_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("date")
            .unwrap()
            .unbind()
    })
}

fn get_time_class(py: Python<'_>) -> &Py<PyAny> {
    TIME_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("time")
            .unwrap()
            .unbind()
    })
}

// Reuse the global Python asyncio event loop created at server startup (TASK_LOCALS)

/// Build an HTTP response for a file path.
/// Handles both small files (loaded into memory) and large files (streamed).
/// Note: Not inlined as it's async and relatively large
pub async fn build_file_response(
    file_path: &str,
    status: StatusCode,
    headers: Vec<(String, String)>,
    skip_compression: bool,
    is_head_request: bool,
) -> HttpResponse {
    match File::open(file_path).await {
        Ok(mut file) => {
            // Get file size
            let file_size = match file.metadata().await {
                Ok(metadata) => metadata.len(),
                Err(e) => {
                    return HttpResponse::InternalServerError()
                        .content_type("text/plain; charset=utf-8")
                        .body(format!("Failed to read file metadata: {}", e));
                }
            };

            // For small files (<10MB), read into memory for better performance
            if file_size < 10 * 1024 * 1024 {
                let mut buffer = Vec::with_capacity(file_size as usize);
                match file.read_to_end(&mut buffer).await {
                    Ok(_) => {
                        let mut builder = HttpResponse::build(status);
                        for (k, v) in headers {
                            if let Ok(name) = HeaderName::try_from(k) {
                                if let Ok(val) = HeaderValue::try_from(v) {
                                    builder.append_header((name, val));
                                }
                            }
                        }
                        if skip_compression {
                            builder.append_header(("content-encoding", "identity"));
                        }
                        let body = if is_head_request { Vec::new() } else { buffer };
                        builder.body(body)
                    }
                    Err(e) => HttpResponse::InternalServerError()
                        .content_type("text/plain; charset=utf-8")
                        .body(format!("Failed to read file: {}", e)),
                }
            } else {
                // For large files, use streaming
                let mut builder = HttpResponse::build(status);
                for (k, v) in headers {
                    if let Ok(name) = HeaderName::try_from(k) {
                        if let Ok(val) = HeaderValue::try_from(v) {
                            builder.append_header((name, val));
                        }
                    }
                }
                if skip_compression {
                    builder.append_header(("content-encoding", "identity"));
                }
                if is_head_request {
                    return builder.body(Vec::<u8>::new());
                }
                let stream = stream::unfold(file, |mut file| async move {
                    let mut buffer = vec![0u8; 64 * 1024];
                    match file.read(&mut buffer).await {
                        Ok(0) => None,
                        Ok(n) => {
                            buffer.truncate(n);
                            Some((Ok::<_, std::io::Error>(Bytes::from(buffer)), file))
                        }
                        Err(e) => Some((Err(e), file)),
                    }
                });
                builder.streaming(stream)
            }
        }
        Err(e) => match e.kind() {
            ErrorKind::NotFound => HttpResponse::NotFound()
                .content_type("text/plain; charset=utf-8")
                .body("File not found"),
            ErrorKind::PermissionDenied => HttpResponse::Forbidden()
                .content_type("text/plain; charset=utf-8")
                .body("Permission denied"),
            _ => HttpResponse::InternalServerError()
                .content_type("text/plain; charset=utf-8")
                .body(format!("File error: {}", e)),
        },
    }
}

/// Handle Python errors and convert to HTTP response
/// OPTIMIZATION: #[inline(never)] on error path - keeps hot path code smaller
#[inline(never)]
pub fn handle_python_error(
    py: Python<'_>,
    err: PyErr,
    path: &str,
    method: &str,
    debug: bool,
) -> HttpResponse {
    err.restore(py);
    if let Some(exc) = PyErr::take(py) {
        let exc_value = exc.value(py);
        error::handle_python_exception(py, exc_value, path, method, debug)
    } else {
        error::build_error_response(
            py,
            500,
            "Handler execution error".to_string(),
            vec![],
            None,
            debug,
        )
    }
}

/// Extract headers from request with validation
/// OPTIMIZATION: HeaderName::as_str() already returns lowercase (http crate canonical form)
/// so we skip the redundant to_ascii_lowercase() call (~50ns saved per header)
/// OPTIMIZATION: #[inline] on hot path - called on every request
#[inline]
pub fn extract_headers(
    req: &HttpRequest,
    max_header_size: usize,
) -> Result<AHashMap<String, String>, HttpResponse> {
    const MAX_HEADERS: usize = 100;
    let mut headers: AHashMap<String, String> = AHashMap::with_capacity(16);
    let mut header_count = 0;

    for (name, value) in req.headers().iter() {
        header_count += 1;
        if header_count > MAX_HEADERS {
            return Err(responses::error_400_too_many_headers());
        }
        if let Ok(v) = value.to_str() {
            if v.len() > max_header_size {
                return Err(responses::error_400_header_too_large(max_header_size));
            }
            // HeaderName::as_str() returns lowercase already (http crate stores canonically)
            headers.insert(name.as_str().to_owned(), v.to_owned());
        }
    }
    Ok(headers)
}

/// Build HTTP 422 response for validation errors
pub fn build_validation_error_response(error: &ValidationError) -> HttpResponse {
    let body = serde_json::json!({
        "detail": [error.to_json()]
    });
    HttpResponse::UnprocessableEntity()
        .content_type("application/json")
        .body(body.to_string())
}

/// Convert CoercedValue to Python object
///
/// Constructs actual Python typed objects (uuid.UUID, decimal.Decimal, datetime, etc.)
/// instead of strings, eliminating double-parsing on the Python side.
pub fn coerced_value_to_py(py: Python<'_>, value: &CoercedValue) -> Py<PyAny> {
    match value {
        // Primitives - direct conversion
        CoercedValue::Int(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Float(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Bool(v) => v.into_pyobject(py).unwrap().to_owned().unbind().into_any(),
        CoercedValue::String(v) => v.into_pyobject(py).unwrap().into_any().unbind(),

        // UUID: construct Python uuid.UUID object
        CoercedValue::Uuid(v) => get_uuid_class(py).call1(py, (v.to_string(),)).unwrap(),

        // Decimal: construct Python decimal.Decimal object
        CoercedValue::Decimal(v) => get_decimal_class(py).call1(py, (v.to_string(),)).unwrap(),

        // DateTime (with timezone): construct Python datetime.datetime
        CoercedValue::DateTime(v) => {
            let iso_str = v.to_rfc3339().replace('Z', "+00:00");
            get_datetime_class(py)
                .call_method1(py, "fromisoformat", (iso_str,))
                .unwrap()
        }

        // NaiveDateTime: construct Python datetime.datetime (no timezone)
        CoercedValue::NaiveDateTime(v) => get_datetime_class(py)
            .call_method1(py, "fromisoformat", (v.to_string(),))
            .unwrap(),

        // Date: construct Python datetime.date
        CoercedValue::Date(v) => get_date_class(py)
            .call_method1(py, "fromisoformat", (v.to_string(),))
            .unwrap(),

        // Time: construct Python datetime.time
        CoercedValue::Time(v) => get_time_class(py)
            .call_method1(py, "fromisoformat", (v.to_string(),))
            .unwrap(),

        CoercedValue::Null => py.None(),
    }
}

/// Convert FileInfo to Python dict
pub fn file_info_to_py(py: Python<'_>, file: &FileInfo) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("filename", &file.filename)?;
    dict.set_item("content_type", &file.content_type)?;
    dict.set_item("size", file.size)?;

    match &file.content {
        FileContent::Memory(bytes) => {
            dict.set_item("content", PyBytes::new(py, bytes))?;
            dict.set_item("temp_path", py.None())?;
        }
        FileContent::Disk(temp_file) => {
            // For disk-spooled files, pass the temp path instead of content
            dict.set_item("content", py.None())?;
            dict.set_item("temp_path", temp_file.path().to_string_lossy().to_string())?;
        }
    }

    Ok(dict.unbind())
}

/// Convert FormParseResult to Python dicts
pub fn form_result_to_py(
    py: Python<'_>,
    result: &FormParseResult,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    // Convert form_map
    let form_dict = PyDict::new(py);
    for (key, value) in &result.form_map {
        form_dict.set_item(key, coerced_value_to_py(py, value))?;
    }

    // Convert files_map - each field can have multiple files
    let files_dict = PyDict::new(py);
    for (field_name, files) in &result.files_map {
        if files.len() == 1 {
            // Single file - store directly
            let file_dict = file_info_to_py(py, &files[0])?;
            files_dict.set_item(field_name, file_dict)?;
        } else {
            // Multiple files - store as list
            let file_list = PyList::empty(py);
            for file in files {
                let file_dict = file_info_to_py(py, file)?;
                file_list.append(file_dict)?;
            }
            files_dict.set_item(field_name, file_list)?;
        }
    }

    Ok((form_dict.unbind(), files_dict.unbind()))
}

enum ResponseWireBody {
    Bytes(Vec<u8>),
    /// Zero-copy path: PyBackedBytes holds a reference to the Python bytes object.
    /// bytes::Bytes::from_owner wraps it directly — no memcpy at any point.
    ZeroCopyBytes(PyBackedBytes),
    FilePath(String),
    Stream {
        media_type: String,
        content_obj: Py<PyAny>,
        is_async_generator: bool,
    },
}

enum MetaRef {
    /// Fast path: static reference from integer tag (no allocation)
    Static(&'static ResponseMeta),
    /// Slow path: parsed from Python tuple (custom headers/cookies)
    Owned(ResponseMeta),
}

impl MetaRef {
    #[inline]
    fn as_ref(&self) -> &ResponseMeta {
        match self {
            MetaRef::Static(s) => s,
            MetaRef::Owned(o) => o,
        }
    }
}

struct ParsedResponseWire {
    status: StatusCode,
    meta: MetaRef,
    body: ResponseWireBody,
}

fn parse_response_wire(py: Python<'_>, result_obj: &Py<PyAny>) -> PyResult<ParsedResponseWire> {
    let obj = result_obj.bind(py);
    let tuple = obj.cast::<PyTuple>()?;
    if tuple.len() != 4 {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "ResponseWireV1 must be a 4-tuple: (status, meta, body_kind, body_payload)",
        ));
    }

    let status_code: u16 = tuple.get_item(0)?.extract()?;
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::OK);
    // Fast path: integer meta tag maps to static ResponseMeta (no String alloc, no tuple parse).
    // Slow path: parse full tuple for responses with custom headers/cookies.
    let meta_item = tuple.get_item(1)?;
    let meta = if let Ok(tag) = meta_item.extract::<u8>() {
        match ResponseMeta::from_tag(tag) {
            Some(static_meta) => MetaRef::Static(static_meta),
            None => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Unknown response meta tag: {}",
                    tag
                )))
            }
        }
    } else {
        MetaRef::Owned(ResponseMeta::from_python(&meta_item)?)
    };
    // body_kind is an integer tag: 0=bytes, 1=stream, 2=file (avoids String alloc per response)
    let body_kind: u8 = tuple.get_item(2)?.extract()?;
    let payload = tuple.get_item(3)?;

    // body_kind integer tags must match Python's _BODY_BYTES/STREAM/FILE in serialization.py:
    //   0 = bytes, 1 = stream, 2 = file
    let body = match body_kind {
        0 => {
            // Zero-copy path: PyBackedBytes holds a Python reference + slice pointer.
            // No memcpy here. bytes::Bytes::from_owner (outside GIL) wraps it directly.
            if let Ok(backed) = payload.extract::<PyBackedBytes>() {
                ResponseWireBody::ZeroCopyBytes(backed)
            } else {
                // Fallback for non-bytes payloads (uncommon).
                ResponseWireBody::Bytes(payload.extract::<Vec<u8>>()?)
            }
        }
        1 => {
            // stream
            let streaming_cls = get_streaming_response_class(py);
            if !payload.is_instance(streaming_cls.bind(py))? {
                return Err(pyo3::exceptions::PyTypeError::new_err(
                    "stream payload must be StreamingResponse",
                ));
            }
            let media_type: String = payload
                .getattr(pyo3::intern!(py, "media_type"))?
                .extract()
                .unwrap_or_else(|_| "application/octet-stream".to_string());
            let content_obj: Py<PyAny> = payload.getattr(pyo3::intern!(py, "content"))?.unbind();
            let is_async_generator: bool = payload
                .getattr(pyo3::intern!(py, "is_async_generator"))?
                .extract()
                .unwrap_or(false);

            ResponseWireBody::Stream {
                media_type,
                content_obj,
                is_async_generator,
            }
        }
        2 => ResponseWireBody::FilePath(payload.extract::<String>()?), // file
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unsupported response body kind: {}",
                other
            )))
        }
    };

    Ok(ParsedResponseWire { status, meta, body })
}

#[inline]
fn mark_skip_cors(response: &mut HttpResponse, skip_cors: bool) {
    if skip_cors {
        response
            .headers_mut()
            .insert(SKIP_CORS_HEADER_NAME, SKIP_CORS_HEADER_VALUE);
    }
}

/// Build HttpResponse from an already-parsed wire result.
/// Shared by both sync-result fallback (stream/file from trivially-async) and async path.
async fn build_response_from_parsed(
    parsed: ParsedResponseWire,
    skip_compression: bool,
    skip_cors: bool,
    is_head_request: bool,
) -> HttpResponse {
    let meta_ref = parsed.meta.as_ref();

    match parsed.body {
        ResponseWireBody::Bytes(body_bytes) => {
            let body = if is_head_request {
                Vec::new()
            } else {
                body_bytes
            };
            let mut response = response_builder::build_response_from_meta(
                parsed.status,
                meta_ref,
                body,
                skip_compression,
            );
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::ZeroCopyBytes(backed) => {
            let body = if is_head_request {
                drop(backed);
                Bytes::new()
            } else {
                Bytes::from_owner(backed)
            };
            let mut response = response_builder::build_response_from_meta(
                parsed.status,
                meta_ref,
                body,
                skip_compression,
            );
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::FilePath(file_path) => {
            let headers = response_builder::meta_to_headers(meta_ref);
            let mut response = build_file_response(
                &file_path, parsed.status, headers, skip_compression, is_head_request,
            )
            .await;
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::Stream {
            media_type,
            content_obj,
            is_async_generator,
        } => {
            let headers = response_builder::meta_to_headers(meta_ref);
            if media_type == "text/event-stream" {
                if is_head_request {
                    let mut response = response_builder::build_sse_response(
                        parsed.status, headers, skip_compression,
                    )
                    .body(Vec::<u8>::new());
                    mark_skip_cors(&mut response, skip_cors);
                    return response;
                }
                let stream = create_sse_stream(content_obj, is_async_generator);
                let mut response =
                    response_builder::build_sse_response(parsed.status, headers, skip_compression)
                        .streaming(stream);
                mark_skip_cors(&mut response, skip_cors);
                return response;
            }

            let mut builder = HttpResponse::build(parsed.status);
            for (k, v) in headers {
                builder.append_header((k, v));
            }
            if is_head_request {
                if skip_compression {
                    builder.append_header(("Content-Encoding", "identity"));
                }
                let mut response = builder.body(Vec::<u8>::new());
                mark_skip_cors(&mut response, skip_cors);
                return response;
            }
            if skip_compression {
                builder.append_header(("Content-Encoding", "identity"));
            }
            let stream = create_python_stream(content_obj, is_async_generator);
            let mut response = builder.streaming(stream);
            mark_skip_cors(&mut response, skip_cors);
            response
        }
    }
}

pub async fn response_from_wire_result(
    result_obj: Py<PyAny>,
    skip_compression: bool,
    skip_cors: bool,
    is_head_request: bool,
) -> PyResult<HttpResponse> {
    let parsed = Python::attach(|py| parse_response_wire(py, &result_obj))?;
    Ok(build_response_from_parsed(parsed, skip_compression, skip_cors, is_head_request).await)
}

/// Build prebound Python args/kwargs from Rust binding metadata.
///
/// Returns None if any required binding value is missing so Python injector can
/// execute as a safe fallback and preserve error semantics.
pub(crate) fn build_prebound_args_kwargs(
    py: Python<'_>,
    bindings: &[RustArgBinding],
    path_params: &Bound<'_, PyDict>,
    query_params: &Bound<'_, PyDict>,
    headers: &Bound<'_, PyDict>,
    cookies: &Bound<'_, PyDict>,
) -> Option<(Py<PyList>, Py<PyDict>)> {
    let args = PyList::empty(py);
    let kwargs = PyDict::new(py);

    for binding in bindings {
        let source_dict = match binding.source {
            RustArgSource::Path => path_params,
            RustArgSource::Query => query_params,
            RustArgSource::Header => headers,
            RustArgSource::Cookie => cookies,
        };

        let value = source_dict.get_item(&binding.lookup_key).ok().flatten()?;

        if binding.positional {
            args.append(&value).ok()?;
        } else {
            kwargs.set_item(&binding.arg_name, &value).ok()?;
        }
    }

    Some((args.unbind(), kwargs.unbind()))
}

pub async fn handle_request<const ACCESS_LOG: bool>(
    req: HttpRequest,
    mut payload: web::Payload,
    state: web::Data<Arc<AppState>>,
) -> HttpResponse {
    // Keep as &str - no allocation, only clone on error paths
    let method = req.method().as_str();
    let path = req.path();

    // ACCESS_LOG: compiler eliminates this entirely when ACCESS_LOG=false (const generic)
    let access_log_start = if ACCESS_LOG {
        Some(std::time::Instant::now())
    } else {
        None
    };

    let router = GLOBAL_ROUTER.get().expect("Router not initialized");

    // Find the route for the requested method and path
    // RouteMatch enum allows us to skip path param processing for static routes
    // Also capture the handler once to avoid a second route lookup before dispatch.
    let (route_handler, path_params, handler_id) = {
        if let Some(route_match) = router.find(method, path) {
            let handler_id = route_match.handler_id();
            let handler = Python::attach(|py| route_match.route().handler.clone_ref(py));
            let raw_params = route_match.path_params();

            // URL-decode path parameters for consistency with query string parsing
            // This ensures /items/hello%20world correctly yields id="hello world"
            let path_params = raw_params.map(|params| {
                params
                    .into_iter()
                    .map(|(k, v)| {
                        let decoded = if v.as_bytes().iter().any(|&b| b == b'%' || b == b'+') {
                            match urlencoding::decode(&v) {
                                Ok(cow) => cow.into_owned(),
                                Err(_) => v,
                            }
                        } else {
                            v
                        };
                        (k, decoded)
                    })
                    .collect()
            });
            (handler, path_params, handler_id)
        } else {
            // No route found - check for trailing slash redirect FIRST
            // This only runs when route doesn't match (minimal overhead)
            // Starlette-style: redirect to canonical URL if alternate path exists
            if path != "/" {
                let alternate_path = if path.ends_with('/') {
                    path.trim_end_matches('/').to_string()
                } else {
                    format!("{}/", path)
                };

                // Try alternate path - if it matches, send 308 redirect
                if router.find(method, &alternate_path).is_some() {
                    let query = req.query_string();
                    let location = if query.is_empty() {
                        alternate_path
                    } else {
                        format!("{}?{}", alternate_path, query)
                    };
                    return HttpResponse::PermanentRedirect() // 308
                        .insert_header(("Location", location))
                        .finish();
                }
            }

            // No explicit handler found - check for automatic OPTIONS
            if method == "OPTIONS" {
                let available_methods = router.find_all_methods(path);
                if !available_methods.is_empty() {
                    let allow_header = available_methods.join(", ");
                    // CORS headers will be added by CorsMiddleware
                    return HttpResponse::NoContent()
                        .insert_header(("Allow", allow_header))
                        .insert_header(("Content-Type", "application/json"))
                        .finish();
                }
            }

            // HTTP ASGI mount fallback:
            // - only after Bolt route miss
            // - only after trailing-slash/API-method near-miss checks above
            if let Some(asgi_mount) = find_asgi_mount(state.get_ref(), path) {
                return asgi_http::handle_asgi_mount_request(
                    req,
                    payload,
                    asgi_mount,
                    state.debug,
                    state.max_payload_size,
                    state.asgi_mount_timeout,
                )
                .await;
            }

            // Handle OPTIONS preflight for non-existent routes
            // IMPORTANT: Preflight MUST return 2xx status for browser to proceed with actual request
            // Browsers reject preflight responses with non-2xx status codes (like 404)
            if method == "OPTIONS" {
                // Check if global CORS is configured
                if state.global_cors_config.is_some() {
                    // CORS headers will be added by CorsMiddleware
                    return HttpResponse::NoContent().finish();
                }
            }

            // Route not found - return 404
            // CORS headers will be added by CorsMiddleware if configured
            return responses::error_404();
        }
    };

    // Store method/path as owned for Python (needed after route_match is dropped)
    // OPTIMIZATION: Use compact strings to reduce allocation overhead
    let method_owned = method.to_string();
    let path_owned = path.to_string();

    // Get parsed route metadata (Rust-native) by reference.
    // NOTE: Fetch metadata EARLY so we can use optimization flags to skip unnecessary parsing.
    let route_metadata = ROUTE_METADATA
        .get()
        .and_then(|meta_map| meta_map.get(handler_id));

    // OPTIMIZATION: Extract the execution plan bitfield once (Copy u16).
    // All subsequent flag reads are direct bit-tests, avoiding repeated Option::map closures.
    let plan = route_metadata.map(|m| m.plan);

    let needs_query = plan.map_or(true, |p| p.needs_query());
    let query_params = if needs_query {
        req.uri().query().and_then(|q| {
            let parsed = parse_query_string(q);
            if parsed.is_empty() {
                None
            } else {
                Some(parsed)
            }
        })
    } else {
        None
    };

    let needs_body = plan.map_or(true, |p| p.needs_body());

    // Type validation for path and query parameters (Rust-native, no GIL)
    let (path_coerced, query_coerced) = if let Some(route_meta) = route_metadata {
        match validate_and_cache_typed_params(
            path_params.as_ref(),
            query_params.as_ref(),
            &route_meta.param_types,
        ) {
            Ok(cached) => cached,
            Err(response) => return response,
        }
    } else {
        (None, None)
    };

    let needs_headers = plan.map_or(true, |p| p.needs_headers());
    let needs_cookies = plan.map_or(true, |p| p.needs_cookies());
    let needs_form_parsing = plan.map_or(false, |p| p.needs_form_parsing());
    let has_route_auth_or_guards = plan.map_or(false, |p| p.has_auth_or_guards());
    let has_route_rate_limit = plan.map_or(false, |p| p.has_rate_limit());
    let must_extract_headers = needs_headers
        || needs_cookies
        || needs_form_parsing
        || has_route_auth_or_guards
        || has_route_rate_limit;
    let skip_cors = plan.map_or(false, |p| p.skip_cors());
    let skip_compression = plan.map_or(false, |p| p.skip_compression());
    let can_sync_dispatch = plan.map_or(false, |p| p.can_sync_dispatch());

    // Extract and validate headers
    let headers = if must_extract_headers {
        match extract_headers(&req, state.max_header_size) {
            Ok(h) => Some(h),
            Err(response) => return response,
        }
    } else {
        None
    };

    // Get peer address only when needed (rate limiting or conn_remote_addr fallback).
    // Skip ip().to_string() for simple API routes with no auth/middleware/rate-limiting.
    let peer_addr = if has_route_rate_limit || must_extract_headers {
        req.peer_addr().map(|addr| addr.ip().to_string())
    } else {
        None
    };

    // Process rate limiting (Rust-native, no GIL)
    if let Some(route_meta) = route_metadata {
        if let Some(ref rate_config) = route_meta.rate_limit_config {
            if let Some(headers_map) = headers.as_ref() {
                if let Some(response) = middleware::rate_limit::check_rate_limit(
                    handler_id,
                    headers_map,
                    peer_addr.as_deref(),
                    rate_config,
                    &method,
                    &path,
                ) {
                    // CORS headers will be added by CorsMiddleware
                    return response;
                }
            } else {
                debug_assert!(false, "rate-limited route missing extracted headers");
            }
        }
    }

    // Execute authentication and guards using shared validation logic
    let auth_ctx = if has_route_auth_or_guards {
        if let Some(route_meta) = route_metadata {
            let empty_headers = AHashMap::new();
            let headers_map = headers.as_ref().unwrap_or(&empty_headers);
            match validate_auth_and_guards(
                headers_map,
                &route_meta.auth_backends,
                &route_meta.guards,
            ) {
                AuthGuardResult::Allow(ctx) => ctx,
                AuthGuardResult::Unauthorized => {
                    // CORS headers will be added by CorsMiddleware
                    return responses::error_401();
                }
                AuthGuardResult::Forbidden => {
                    // CORS headers will be added by CorsMiddleware
                    return responses::error_403();
                }
            }
        } else {
            None
        }
    } else {
        None
    };

    // Optimization: Only parse cookies if handler needs them
    // Cookie parsing can be expensive for requests with many cookies
    let cookies = if needs_cookies {
        Some(parse_cookies_inline(
            headers
                .as_ref()
                .and_then(|h| h.get("cookie").map(|s| s.as_str())),
        ))
    } else {
        None
    };

    // Derive connection info from already-extracted headers (avoids a second header-parse pass).
    // conn_info is only used for request.META (Django templates) and build_absolute_uri().
    // When headers weren't extracted (pure API routes with no auth/cookies/middleware),
    // empty strings are safe because no Django middleware will call META anyway.
    let (conn_host, conn_scheme, conn_remote_addr) = if must_extract_headers {
        let host = headers
            .as_ref()
            .and_then(|h| h.get("host"))
            .cloned()
            .unwrap_or_default();
        let scheme = headers
            .as_ref()
            .and_then(|h| h.get("x-forwarded-proto"))
            .cloned()
            .unwrap_or_else(|| "http".to_string());
        // X-Forwarded-For: leftmost IP is the original client (RFC 7239 §7.1)
        let remote_addr = headers
            .as_ref()
            .and_then(|h| h.get("x-forwarded-for"))
            .and_then(|v| v.split(',').next().map(|s| s.trim().to_string()))
            .or_else(|| headers.as_ref().and_then(|h| h.get("x-real-ip").cloned()))
            .or_else(|| peer_addr.clone())
            .unwrap_or_else(|| "127.0.0.1".to_string());
        (host, scheme, remote_addr)
    } else {
        // No headers extracted → no Django middleware → META won't be accessed
        (String::new(), String::new(), String::new())
    };

    // Determine if form parsing is needed and get content type
    let content_type = headers
        .as_ref()
        .and_then(|h| h.get("content-type"))
        .map(|s| s.as_str())
        .unwrap_or("");

    let is_multipart = content_type.starts_with("multipart/form-data");
    let is_urlencoded = content_type.starts_with("application/x-www-form-urlencoded");

    // Read body from payload only when needed.
    // For multipart, we need the payload stream directly.
    let (body, form_result): (Vec<u8>, Option<FormParseResult>) =
        if !needs_body && !needs_form_parsing {
            (Vec::new(), None)
        } else if needs_form_parsing && is_multipart {
            // Multipart form parsing - uses the payload stream directly
            let empty_form_type_hints: HashMap<String, u8> = HashMap::new();
            let empty_file_constraints: HashMap<String, FileFieldConstraints> = HashMap::new();
            let form_type_hints = route_metadata
                .map(|m| &m.form_type_hints)
                .unwrap_or(&empty_form_type_hints);
            let file_constraints = route_metadata
                .map(|m| &m.file_constraints)
                .unwrap_or(&empty_file_constraints);
            let max_upload_size = route_metadata
                .map(|m| m.max_upload_size)
                .unwrap_or(1024 * 1024);
            let memory_spool_threshold = route_metadata
                .map(|m| m.memory_spool_threshold)
                .unwrap_or(DEFAULT_MEMORY_LIMIT);

            // Create Multipart from the payload
            let multipart = Multipart::new(req.headers(), payload);

            match parse_multipart(
                multipart,
                form_type_hints,
                file_constraints,
                max_upload_size,
                memory_spool_threshold,
                DEFAULT_MAX_PARTS,
            )
            .await
            {
                Ok(result) => (Vec::new(), Some(result)),
                Err(validation_error) => {
                    return build_validation_error_response(&validation_error);
                }
            }
        } else {
            // Read payload as bytes (for non-multipart requests)
            let mut body_vec = Vec::new();
            while let Some(chunk) = payload.next().await {
                match chunk {
                    Ok(data) => body_vec.extend_from_slice(&data),
                    Err(e) => {
                        return HttpResponse::BadRequest()
                            .content_type("application/json")
                            .body(format!(
                                "{{\"error\": \"Failed to read request body: {}\"}}",
                                e
                            ));
                    }
                }
            }

            // URL-encoded form parsing
            if needs_form_parsing && is_urlencoded {
                let empty_form_type_hints: HashMap<String, u8> = HashMap::new();
                let form_type_hints = route_metadata
                    .map(|m| &m.form_type_hints)
                    .unwrap_or(&empty_form_type_hints);

                match parse_urlencoded(&body_vec, form_type_hints) {
                    Ok(form_map) => {
                        let result = FormParseResult {
                            form_map,
                            files_map: HashMap::new(),
                        };
                        (body_vec, Some(result))
                    }
                    Err(validation_error) => {
                        return build_validation_error_response(&validation_error);
                    }
                }
            } else {
                (body_vec, None)
            }
        };

    // Check if this is a HEAD request (needed for body stripping after Python handler)
    let is_head_request = method == "HEAD";

    // Unified GIL block: build request + dispatch (sync or async)
    // OPTIMIZATION: Single GIL acquisition — route_handler moved in (no clone_ref)
    let dispatch_result: Result<DispatchOutcome, PyErr> = Python::attach(|py| {
        let handler = route_handler;

        // Create context dict only if auth context is present
        let context = if let Some(ref auth) = auth_ctx {
            let ctx_dict = PyDict::new(py);
            let ctx_py = ctx_dict.unbind();
            populate_auth_context(&ctx_py, auth, py);
            Some(ctx_py)
        } else {
            None
        };

        // Get type hints for type coercion
        let empty_param_types: HashMap<String, u8> = HashMap::new();
        let param_types = route_metadata
            .map(|m| &m.param_types)
            .unwrap_or(&empty_param_types);

        // OPTIMIZATION: Create typed PyDicts only when non-empty.
        // Saves 1 Python heap alloc per empty source (up to 4 for simple API handlers).
        let path_params_py: Option<Py<PyDict>> = if let Some(path_params) = path_params.as_ref() {
            let dict = PyDict::new(py);
            for (name, value) in path_params {
                if let Some(coerced) = path_coerced.as_ref().and_then(|m| m.get(name)) {
                    dict.set_item(name, coerced_value_to_py(py, coerced))?;
                } else {
                    dict.set_item(name, value)?;
                }
            }
            Some(dict.unbind())
        } else {
            None
        };

        let query_params_py: Option<Py<PyDict>> = if let Some(query_params) = query_params.as_ref()
        {
            let dict = PyDict::new(py);
            for (name, value) in query_params {
                if let Some(coerced) = query_coerced.as_ref().and_then(|m| m.get(name)) {
                    dict.set_item(name, coerced_value_to_py(py, coerced))?;
                } else {
                    dict.set_item(name, value)?;
                }
            }
            Some(dict.unbind())
        } else {
            None
        };

        let headers_py: Option<Py<PyDict>> = if needs_headers {
            if let Some(headers_map) = headers.as_ref() {
                Some(params_to_py_dict(py, headers_map, param_types)?.unbind())
            } else {
                Some(PyDict::new(py).unbind())
            }
        } else {
            None
        };
        let cookies_py: Option<Py<PyDict>> = if needs_cookies {
            if let Some(cookies_map) = cookies.as_ref() {
                Some(params_to_py_dict(py, cookies_map, param_types)?.unbind())
            } else {
                Some(PyDict::new(py).unbind())
            }
        } else {
            None
        };

        // Only create state dict when Rust-side prebound args exist.
        // For fast-path handlers (no rust_arg_bindings), state is lazily allocated on first access.
        let state_lock = std::sync::OnceLock::new();
        if let Some(bindings) = route_metadata.and_then(|m| m.rust_arg_bindings.as_deref()) {
            // Create temp empty dict for prebound arg extraction (only when bindings exist)
            let empty_dict = PyDict::new(py);
            let pp_ref = match &path_params_py {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            let qp_ref = match &query_params_py {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            let hd_ref = match &headers_py {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            let ck_ref = match &cookies_py {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            if let Some((pre_args, pre_kwargs)) =
                build_prebound_args_kwargs(py, bindings, pp_ref, qp_ref, hd_ref, ck_ref)
            {
                let state_dict = PyDict::new(py);
                state_dict.set_item("_bolt_prebound_args", pre_args)?;
                state_dict.set_item("_bolt_prebound_kwargs", pre_kwargs)?;
                let _ = state_lock.set(state_dict.unbind());
            }
        }

        // Only create form/files dicts when form data is present (saves 2 allocs per request).
        let (form_map_opt, files_map_opt) = if let Some(ref result) = form_result {
            let (fm, fi) = form_result_to_py(py, result)?;
            (Some(fm), Some(fi))
        } else {
            (None, None)
        };

        // OPTIMIZATION: Move owned strings into PyRequest — no .clone() needed.
        // Error paths reconstruct from req.method()/req.path() (cold path, HttpRequest still alive).
        let request = PyRequest {
            method: method_owned,
            path: path_owned,
            body,
            path_params: path_params_py,
            query_params: query_params_py,
            headers: headers_py,
            cookies: cookies_py,
            context,
            user: None,
            state: state_lock,
            form_map: form_map_opt,
            files_map: files_map_opt,
            meta_cache: std::sync::OnceLock::new(),
            conn_host,
            conn_scheme,
            conn_remote_addr,
        };
        let request_obj = Py::new(py, request)?;

        if can_sync_dispatch {
            // SYNC PATH: Call dispatch_sync directly → returns tuple (not coroutine).
            // Parse response wire and build HTTP response in the same GIL block.
            // Eliminates: coroutine creation, into_future_with_locals, asyncio polling.
            let result_obj = state
                .dispatch_sync
                .call1(py, (handler, request_obj, handler_id))?;
            let parsed = parse_response_wire(py, &result_obj)?;
            let meta_ref = parsed.meta.as_ref();
            let response = match parsed.body {
                ResponseWireBody::ZeroCopyBytes(backed) => {
                    let body = if is_head_request {
                        drop(backed);
                        Bytes::new()
                    } else {
                        Bytes::from_owner(backed)
                    };
                    let mut resp = response_builder::build_response_from_meta(
                        parsed.status,
                        meta_ref,
                        body,
                        skip_compression,
                    );
                    mark_skip_cors(&mut resp, skip_cors);
                    resp
                }
                ResponseWireBody::Bytes(body_bytes) => {
                    let body = if is_head_request {
                        Vec::new()
                    } else {
                        body_bytes
                    };
                    let mut resp = response_builder::build_response_from_meta(
                        parsed.status,
                        meta_ref,
                        body,
                        skip_compression,
                    );
                    mark_skip_cors(&mut resp, skip_cors);
                    resp
                }
                // Stream/file body: sync dispatch ran the handler but the response
                // needs async processing (e.g. StreamingResponse from a trivially-async handler).
                // Pass the already-parsed wire to avoid re-acquiring GIL and re-parsing.
                _ => {
                    return Ok(DispatchOutcome::SyncResult(parsed));
                }
            };
            Ok(DispatchOutcome::Ready(response))
        } else {
            // ASYNC PATH: Create coroutine + future (existing behavior)
            let dispatch = state.dispatch.clone_ref(py);
            let locals = TASK_LOCALS.get().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized")
            })?;
            let coroutine = dispatch.call1(py, (handler, request_obj, handler_id))?;
            let fut =
                pyo3_async_runtimes::into_future_with_locals(locals, coroutine.into_bound(py))?;
            Ok(DispatchOutcome::Pending(Box::pin(fut)))
        }
    });

    let response = match dispatch_result {
        Ok(DispatchOutcome::Ready(response)) => response,
        // Sync dispatch completed but body needs async post-processing (stream/file).
        // Already parsed — no duplicate GIL acquire or wire re-parse.
        Ok(DispatchOutcome::SyncResult(parsed)) => {
            build_response_from_parsed(parsed, skip_compression, skip_cors, is_head_request).await
        }
        Ok(DispatchOutcome::Pending(fut)) => match fut.await {
            Ok(result_obj) => {
                match response_from_wire_result(
                    result_obj,
                    skip_compression,
                    skip_cors,
                    is_head_request,
                )
                .await
                {
                    Ok(response) => response,
                    Err(e) => Python::attach(|py| {
                        error::build_error_response(
                            py,
                            500,
                            format!("Handler returned unsupported response wire format: {}", e),
                            vec![],
                            None,
                            state.debug,
                        )
                    }),
                }
            }
            Err(e) => Python::attach(|py| {
                // Cold path: reconstruct from HttpRequest (strings were moved into PyRequest)
                handle_python_error(py, e, req.path(), req.method().as_str(), state.debug)
            }),
        },
        Err(e) => Python::attach(|py| {
            handle_python_error(py, e, req.path(), req.method().as_str(), state.debug)
        }),
    };

    // ACCESS_LOG: compiler eliminates this entire block when ACCESS_LOG=false (const generic).
    // When ACCESS_LOG=true, log method/path/status/duration via Django's logger.
    if ACCESS_LOG {
        // access_log_start is always Some when ACCESS_LOG=true; unwrap is safe.
        let dur_ms = access_log_start.unwrap().elapsed().as_secs_f64() * 1000.0;
        let status = response.status().as_u16();
        // access_logger is always Some when ACCESS_LOG=true (guaranteed by server.rs startup).
        Python::attach(|py| {
            let _ = state.access_logger.as_ref().unwrap().call_method1(
                py,
                "info",
                (format!("{} {} {} {:.1}ms", method, path, status, dur_ms),),
            );
        });
    }

    response
}
