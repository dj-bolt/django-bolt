use actix_multipart::Multipart;
use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::{http::StatusCode, web, HttpRequest, HttpResponse};
use ahash::AHashMap;
use bytes::Bytes;
use futures_util::stream;
use futures_util::StreamExt;
use pyo3::prelude::*;
use pyo3::pybacked::{PyBackedBytes, PyBackedStr};
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
    FormParseResult, FormValue, ValidationError, DEFAULT_MAX_PARTS, DEFAULT_MEMORY_LIMIT,
    ERROR_TYPE_PAYLOAD_TOO_LARGE,
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
use crate::state::{find_asgi_mount, AppState, GLOBAL_ROUTER, ROUTE_METADATA};
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
/// directly — datetime/date/time go through PyO3's chrono integration (C-API
/// construction, no ISO-string round trip) and UUID is built from its 128-bit
/// value instead of re-parsing a hex string in Python.
pub fn coerced_value_to_py(py: Python<'_>, value: &CoercedValue) -> Py<PyAny> {
    match value {
        // Primitives - direct conversion
        CoercedValue::Int(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Float(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Bool(v) => v.into_pyobject(py).unwrap().to_owned().unbind().into_any(),
        CoercedValue::String(v) => v.into_pyobject(py).unwrap().into_any().unbind(),

        // UUID: construct from the 128-bit integer (uuid.UUID(int=...)) —
        // avoids a 36-char string alloc in Rust + hex parsing in Python.
        CoercedValue::Uuid(v) => uuid_to_py(py, *v).unwrap(),

        // Decimal: construct Python decimal.Decimal object
        CoercedValue::Decimal(v) => crate::type_coercion::get_decimal_class(py)
            .call1(py, (v.to_string(),))
            .unwrap(),

        // Temporal types: direct C-API construction via pyo3's chrono feature.
        CoercedValue::DateTime(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::NaiveDateTime(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Date(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
        CoercedValue::Time(v) => v.into_pyobject(py).unwrap().into_any().unbind(),

        CoercedValue::Null => py.None(),
    }
}

/// Build a Python `uuid.UUID` from the parsed 128-bit value.
#[inline]
pub(crate) fn uuid_to_py(py: Python<'_>, v: uuid::Uuid) -> PyResult<Py<PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "int"), v.as_u128())?;
    Ok(crate::type_coercion::get_uuid_class(py)
        .bind(py)
        .call((), Some(&kwargs))?
        .unbind())
}

/// Convert a FileInfo into an unbound Python dict for use in Python code.
///
/// The returned dict contains the following keys:
/// - `filename` (str)
/// - `content_type` (str)
/// - `size` (int)
/// - `content` (`bytes` when the file is memory-backed, `None` when spooled to disk)
/// - `temp_path` (str path when spooled to disk, `None` when memory-backed)
///
/// # Examples
///
/// ```ignore
/// use pyo3::prelude::*;
/// // Assume `FileInfo` and `FileContent` are in scope:
/// // let file = FileInfo { filename: "a.txt".into(), content_type: "text/plain".into(), size: 3, content: FileContent::Memory(vec![97,98,99]) };
/// Python::with_gil(|py| {
///     // let py_dict = file_info_to_py(py, &file).unwrap();
///     // assert_eq!(py_dict.get_item("filename").unwrap().extract::<String>().unwrap(), "a.txt");
/// });
/// ```
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

/// Convert a parsed multipart/form result into two Python dictionaries: one for form fields and one for uploaded files.
///
/// Single-value form fields are converted to scalar Python objects. Repeated form fields are converted to Python `list`s so consumers (e.g., msgspec) see `list[T]` for repeated keys. For files, a single uploaded file for a field becomes a single Python dict describing that file; multiple uploaded files for the same field become a Python `list` of file dicts. Returned dicts are unbound `PyDict`s ready to be attached to Python objects.
///
/// # Examples
///
/// ```ignore
/// use pyo3::prelude::*;
///
/// // Assume `result` is a FormParseResult obtained from multipart parsing.
/// # let result: crate::form_parsing::FormParseResult = unimplemented!();
/// let seq_fields: std::collections::HashSet<String> = std::collections::HashSet::new();
/// Python::with_gil(|py| {
///     let (form_dict, files_dict) =
///         crate::handler::form_result_to_py(py, &result, &seq_fields).unwrap();
///     // `form_dict` maps field names -> scalar Python objects or lists
///     // `files_dict` maps field names -> file dict or list of file dicts
///     let _ = form_dict;
///     let _ = files_dict;
/// });
/// ```
pub fn form_result_to_py(
    py: Python<'_>,
    result: &FormParseResult,
    seq_fields: &std::collections::HashSet<String>,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let form_dict = PyDict::new(py);
    for (key, value) in &result.form_map {
        match value {
            FormValue::Single(v) => {
                let py_val = coerced_value_to_py(py, v);
                if seq_fields.contains(key) {
                    // Always emit list[T] for fields annotated as list/set/tuple.
                    // The Python form-struct extractor skips its isinstance wrap-check.
                    let list = PyList::new(py, [py_val])?;
                    form_dict.set_item(key, list)?;
                } else {
                    form_dict.set_item(key, py_val)?;
                }
            }
            FormValue::Multi(vs) => {
                let items: Vec<Py<PyAny>> = vs.iter().map(|v| coerced_value_to_py(py, v)).collect();
                let list = PyList::new(py, items)?;
                form_dict.set_item(key, list)?;
            }
        }
    }

    let files_dict = PyDict::new(py);
    for (field_name, files) in &result.files_map {
        if files.len() == 1 {
            let file_dict = file_info_to_py(py, &files[0])?;
            files_dict.set_item(field_name, file_dict)?;
        } else {
            let mut items: Vec<Py<PyAny>> = Vec::with_capacity(files.len());
            for file in files {
                items.push(file_info_to_py(py, file)?.into_any());
            }
            let file_list = PyList::new(py, items)?;
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
        media_type: PyBackedStr,
        content_obj: Py<PyAny>,
        is_async_generator: bool,
        ping_interval: Option<f64>,
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
            let media_type: PyBackedStr = payload
                .getattr(pyo3::intern!(py, "media_type"))?
                .extract()?;
            let content_obj: Py<PyAny> = payload.getattr(pyo3::intern!(py, "content"))?.unbind();
            let is_async_generator: bool = payload
                .getattr(pyo3::intern!(py, "is_async_generator"))?
                .extract()
                .unwrap_or(false);

            // For SSE streams, extract optional keep-alive ping interval
            let ping_interval: Option<f64> = if media_type == "text/event-stream" {
                payload
                    .getattr(pyo3::intern!(py, "ping_interval"))
                    .ok()
                    .and_then(|v| v.extract().ok())
            } else {
                None
            };

            ResponseWireBody::Stream {
                media_type,
                content_obj,
                is_async_generator,
                ping_interval,
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

/// Borrow the global `CompressionConfig` from `AppState` attached to the
/// request. Returns `None` when no `BoltAPI(compression=...)` was configured.
fn compression_config_from_req(req: &HttpRequest) -> Option<&crate::metadata::CompressionConfig> {
    req.app_data::<actix_web::web::Data<std::sync::Arc<crate::state::AppState>>>()
        .and_then(|s| s.global_compression_config.as_deref())
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
    req: &HttpRequest,
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
                &file_path,
                parsed.status,
                headers,
                skip_compression,
                is_head_request,
            )
            .await;
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::Stream {
            media_type,
            content_obj,
            is_async_generator,
            ping_interval,
        } => {
            let headers = response_builder::meta_to_headers(meta_ref);

            // If the user passed `Content-Encoding` via StreamingResponse
            // headers they have already encoded the body (or are claiming
            // identity); skip the framework codec in that case so we don't
            // double-encode or clobber the caller's intent.
            let user_set_content_encoding = headers
                .iter()
                .any(|(k, _)| k.eq_ignore_ascii_case("content-encoding"));

            let codec = if user_set_content_encoding {
                None
            } else {
                crate::streaming_compression::select_stream_encoding(
                    req,
                    compression_config_from_req(req),
                    skip_compression,
                )
            };
            let encoding_name = codec.map_or("identity", |c| c.header_name());

            if media_type == "text/event-stream" {
                if is_head_request {
                    let mut response = response_builder::build_sse_response(
                        parsed.status,
                        headers,
                        encoding_name,
                        user_set_content_encoding,
                    )
                    .body(Vec::<u8>::new());
                    mark_skip_cors(&mut response, skip_cors);
                    return response;
                }
                let stream =
                    create_sse_stream(content_obj, is_async_generator, ping_interval, codec);
                let mut response = response_builder::build_sse_response(
                    parsed.status,
                    headers,
                    encoding_name,
                    user_set_content_encoding,
                )
                .streaming(stream);
                mark_skip_cors(&mut response, skip_cors);
                return response;
            }

            let mut builder = HttpResponse::build(parsed.status);
            for (k, v) in headers {
                builder.append_header((k, v));
            }
            if !user_set_content_encoding {
                if codec.is_some() {
                    builder.insert_header(("Content-Encoding", encoding_name));
                } else {
                    // Identity marker tells the global compression middleware to
                    // skip — buffering compressors would defeat streaming.
                    builder.insert_header(("Content-Encoding", "identity"));
                }
                // Whether or not we picked a codec, the *body* we send was
                // chosen based on Accept-Encoding (a brotli-capable client
                // would have gotten brotli). Advertise that so shared
                // caches don't serve the identity payload to a client that
                // accepts compression. `append` not `insert` to preserve
                // any Vary the caller set (e.g. CORS's `Vary: Origin`).
                builder.append_header(("Vary", "Accept-Encoding"));
            }
            if is_head_request {
                let mut response = builder.body(Vec::<u8>::new());
                mark_skip_cors(&mut response, skip_cors);
                return response;
            }
            let inner = create_python_stream(content_obj, is_async_generator);
            let stream = crate::streaming::maybe_wrap_codec(inner, codec);
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
    req: &HttpRequest,
) -> PyResult<HttpResponse> {
    let parsed = Python::attach(|py| parse_response_wire(py, &result_obj))?;
    Ok(build_response_from_parsed(parsed, skip_compression, skip_cors, is_head_request, req).await)
}

/// Build prebound Python args/kwargs from Rust binding metadata.
///
/// Returns None if any *required* binding value is missing so the Python
/// injector can execute as a safe fallback and preserve 422 error semantics.
/// Optional bindings are keyword-only: a missing value is either skipped
/// (function default applies) or injected as None (Optional[T] with no default).
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

        match source_dict.get_item(binding.lookup_key.bind(py)).ok()? {
            Some(value) => {
                if binding.positional {
                    args.append(&value).ok()?;
                } else {
                    kwargs.set_item(binding.arg_name.bind(py), &value).ok()?;
                }
            }
            None => {
                if binding.required {
                    // Fall back to the Python injector, which raises the 422.
                    return None;
                }
                if binding.inject_none {
                    kwargs.set_item(binding.arg_name.bind(py), py.None()).ok()?;
                }
                // else: omit — the handler's own default applies.
            }
        }
    }

    Some((args.unbind(), kwargs.unbind()))
}

/// Map an HTTP method to a `Cow::Borrowed` static string. Routable methods are
/// a fixed set, so the per-request `method.to_string()` allocation is avoided;
/// unknown methods (test-only paths) fall back to an owned copy.
#[inline]
pub(crate) fn static_method_name(method: &str) -> std::borrow::Cow<'static, str> {
    match method {
        "GET" => std::borrow::Cow::Borrowed("GET"),
        "POST" => std::borrow::Cow::Borrowed("POST"),
        "PUT" => std::borrow::Cow::Borrowed("PUT"),
        "PATCH" => std::borrow::Cow::Borrowed("PATCH"),
        "DELETE" => std::borrow::Cow::Borrowed("DELETE"),
        "HEAD" => std::borrow::Cow::Borrowed("HEAD"),
        "OPTIONS" => std::borrow::Cow::Borrowed("OPTIONS"),
        "QUERY" => std::borrow::Cow::Borrowed("QUERY"),
        other => std::borrow::Cow::Owned(other.to_string()),
    }
}

/// Parse the `Content-Length` header into a `usize`, if present and valid.
/// Shared by the production handler and the in-process test handler so both
/// enforce the payload limit identically (per src/CLAUDE.md: tests reuse
/// production code rather than duplicating it).
#[inline]
pub(crate) fn parse_content_length(req: &HttpRequest) -> Option<usize> {
    req.headers()
        .get(actix_web::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok())
}

/// Whether the given `Content-Length` exceeds the limit and must be fast-rejected
/// with 413 before reading the body. Callers pass `None` (e.g. no-body routes or
/// chunked requests with no Content-Length), for which this returns false and the
/// per-path streaming checks bound the body instead.
#[inline]
pub(crate) fn content_length_exceeds_limit(
    content_length: Option<usize>,
    max_payload_size: usize,
) -> bool {
    matches!(content_length, Some(len) if len > max_payload_size)
}

thread_local! {
    static TSTATE_PINNED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Pin a Python thread state to this (actix worker) thread on first use.
/// Without it every `Python::attach` creates+destroys a PyThreadState, and a
/// fresh thread state mmaps/munmaps a 16KiB frame-datastack chunk — measured
/// as exactly one mmap+munmap syscall pair PER REQUEST. Actix worker threads
/// live for the process lifetime, so the pinned state is never torn down.
#[inline]
fn ensure_pinned_thread_state() {
    TSTATE_PINNED.with(|c| {
        if !c.get() {
            crate::state::pin_python_thread_state();
            c.set(true);
        }
    });
}

pub async fn handle_request<const ACCESS_LOG: bool>(
    req: HttpRequest,
    mut payload: web::Payload,
    state: web::Data<Arc<AppState>>,
) -> HttpResponse {
    ensure_pinned_thread_state();

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

    // Find the route for the requested method and path.
    // RouteMatch enum allows us to skip path param processing for static routes.
    // The route reference borrows the 'static global router, so the handler
    // clone_ref is deferred into the main dispatch GIL block — no extra
    // Python::attach per request just to clone the callable.
    let (route, path_params, handler_id) = {
        if let Some(route_match) = router.find(method, path) {
            let handler_id = route_match.handler_id();
            let (route, raw_params) = route_match.into_parts();

            // URL-decode path parameters in place for consistency with query
            // string parsing (/items/hello%20world yields id="hello world").
            // Values are mutated in the existing map — no map rebuild.
            let path_params = raw_params.map(|mut params| {
                for v in params.values_mut() {
                    if v.as_bytes().iter().any(|&b| b == b'%' || b == b'+') {
                        let decoded = match urlencoding::decode(v) {
                            Ok(std::borrow::Cow::Owned(s)) => Some(s),
                            _ => None,
                        };
                        if let Some(s) = decoded {
                            *v = s;
                        }
                    }
                }
                params
            });
            (route, path_params, handler_id)
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

    // Method maps to a static str (known fixed set — no per-request alloc);
    // path is dynamic and must be owned for PyRequest.
    let method_static = static_method_name(method);
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

    // Max parameter length resolved once at startup; read the plain field here.
    let max_param_length = state.max_param_length;

    // Type validation for path and query parameters (Rust-native, no GIL)
    let (path_coerced, query_coerced) = if let Some(route_meta) = route_metadata {
        match validate_and_cache_typed_params(
            path_params.as_ref(),
            query_params.as_ref(),
            &route_meta.param_types,
            max_param_length,
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
    let is_async_handler = plan.map_or(false, |p| p.is_async());

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
                AuthGuardResult::Allow(ctx) => {
                    let requires_csrf = ctx.as_ref().is_some_and(|auth| auth.cookie_csrf);
                    if requires_csrf {
                        let scheme = req.connection_info().scheme().to_owned();
                        if crate::validation::cookie_csrf_blocks(method, headers_map, true, &scheme)
                        {
                            return responses::error_403();
                        }
                    }
                    ctx
                }
                AuthGuardResult::Unauthorized => {
                    // CORS headers will be added by CorsMiddleware
                    return responses::error_401();
                }
                AuthGuardResult::Forbidden(denial) => {
                    // CORS headers will be added by CorsMiddleware
                    return responses::error_403_denial(denial.as_deref());
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
        // No META needed → empty strings (META getter will return defaults)
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

    // Parse Content-Length and fast-reject oversized requests before reading any
    // body bytes. Applies to both multipart and non-multipart paths so the
    // payload-size limit is enforced consistently regardless of content type.
    // Only computed for routes that read a body — no-body routes (e.g. the hot
    // GET path) skip the header lookup entirely.
    let reads_body = needs_body || needs_form_parsing;
    let content_length = if reads_body {
        parse_content_length(&req)
    } else {
        None
    };
    if content_length_exceeds_limit(content_length, state.max_payload_size) {
        return responses::error_413();
    }

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
                max_param_length,
                state.max_payload_size,
            )
            .await
            {
                Ok(result) => (Vec::new(), Some(result)),
                Err(validation_error) => {
                    // Aggregate-size overflow maps to 413 to match the non-multipart
                    // path; other multipart failures stay 422 validation errors.
                    if validation_error.error_type == ERROR_TYPE_PAYLOAD_TOO_LARGE {
                        return responses::error_413();
                    }
                    return build_validation_error_response(&validation_error);
                }
            }
        } else {
            // Read payload as bytes (for non-multipart requests).
            // The aggregate size is bounded against max_payload_size during
            // streaming so chunked requests (no Content-Length) are enforced too.
            let mut body_vec = Vec::new();
            let mut total_read: usize = 0;
            while let Some(chunk) = payload.next().await {
                match chunk {
                    Ok(data) => {
                        total_read = match total_read.checked_add(data.len()) {
                            Some(v) => v,
                            None => return responses::error_413(),
                        };
                        if total_read > state.max_payload_size {
                            return responses::error_413();
                        }
                        body_vec.extend_from_slice(&data);
                    }
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

                match parse_urlencoded(&body_vec, form_type_hints, max_param_length) {
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

    // Unified GIL block: build request + dispatch (sync or async).
    // OPTIMIZATION: The ONLY GIL acquisition on the happy path. Dispatch goes
    // through the per-route bound callables (partial(handler, handler_id))
    // stored in the Route — single-argument calls, no per-request handler
    // clone_ref or handler_id conversion.
    let dispatch_result: Result<DispatchOutcome, PyErr> = Python::attach(|py| {
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
                Some(params_to_py_dict(py, headers_map, param_types, max_param_length)?.unbind())
            } else {
                Some(PyDict::new(py).unbind())
            }
        } else {
            None
        };
        let cookies_py: Option<Py<PyDict>> = if needs_cookies {
            if let Some(cookies_map) = cookies.as_ref() {
                Some(params_to_py_dict(py, cookies_map, param_types, max_param_length)?.unbind())
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
                state_dict.set_item(pyo3::intern!(py, "_bolt_prebound_args"), pre_args)?;
                state_dict.set_item(pyo3::intern!(py, "_bolt_prebound_kwargs"), pre_kwargs)?;
                let _ = state_lock.set(state_dict.unbind());
            }
        }

        // Only create form/files dicts when form data is present (saves 2 allocs per request).
        let (form_map_opt, files_map_opt) = if let Some(ref result) = form_result {
            static EMPTY_SEQ: std::sync::OnceLock<std::collections::HashSet<String>> =
                std::sync::OnceLock::new();
            let seq_fields = route_metadata
                .map(|m| &m.form_seq_fields)
                .unwrap_or_else(|| EMPTY_SEQ.get_or_init(std::collections::HashSet::new));
            let (fm, fi) = form_result_to_py(py, result, seq_fields)?;
            (Some(fm), Some(fi))
        } else {
            (None, None)
        };

        // OPTIMIZATION: Move owned strings into PyRequest — no .clone() needed.
        // Error paths reconstruct from req.method()/req.path() (cold path, HttpRequest still alive).
        let request = PyRequest {
            method: method_static,
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
            // SYNC PATH: Call dispatch_sync directly → returns wire tuple or bare
            // body bytes (not a coroutine). Parse and build the HTTP response in
            // the same GIL block. Eliminates: coroutine creation,
            // into_future_with_locals, asyncio polling.
            let result_obj = if is_async_handler {
                crate::worker_loop::dispatch_sync(py, &route.dispatch_sync, &request_obj)?
            } else {
                route.dispatch_sync.call1(py, (request_obj,))?
            };

            // Bare-bytes fast path: sync executors return just the encoded JSON
            // body when (status, meta) == (route default, JSON). Skips the
            // 4-tuple construction in Python and its parsing here.
            if result_obj.bind(py).is_instance_of::<PyBytes>() {
                let backed: PyBackedBytes = result_obj.extract(py)?;
                let status = route_metadata
                    .map(|m| StatusCode::from_u16(m.default_status_code).unwrap_or(StatusCode::OK))
                    .unwrap_or(StatusCode::OK);
                let body = if is_head_request {
                    drop(backed);
                    Bytes::new()
                } else {
                    Bytes::from_owner(backed)
                };
                let mut resp = response_builder::build_response_from_meta(
                    status,
                    &crate::response_meta::STATIC_META_JSON,
                    body,
                    skip_compression,
                );
                mark_skip_cors(&mut resp, skip_cors);
                return Ok(DispatchOutcome::Ready(resp));
            }

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
            // ASYNC PATH: submit to the process-lived WorkerLoop whose ready
            // queue is serviced by a persistent Tokio pump.
            let dispatch = route.dispatch.clone_ref(py);
            let fut = crate::worker_loop::dispatch(dispatch, request_obj.into());
            Ok(DispatchOutcome::Pending(Box::pin(fut)))
        }
    });

    let response = match dispatch_result {
        Ok(DispatchOutcome::Ready(response)) => response,
        // Sync dispatch completed but body needs async post-processing (stream/file).
        // Already parsed — no duplicate GIL acquire or wire re-parse.
        Ok(DispatchOutcome::SyncResult(parsed)) => {
            build_response_from_parsed(parsed, skip_compression, skip_cors, is_head_request, &req)
                .await
        }
        Ok(DispatchOutcome::Pending(fut)) => match fut.await {
            Ok(result_obj) => {
                match response_from_wire_result(
                    result_obj,
                    skip_compression,
                    skip_cors,
                    is_head_request,
                    &req,
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
        // Format OUTSIDE the GIL — only the logger call itself needs Python.
        let msg = format!("{} {} {} {:.1}ms", method, path, status, dur_ms);
        // access_logger is always Some when ACCESS_LOG=true (guaranteed by server.rs startup).
        Python::attach(|py| {
            let _ = state
                .access_logger
                .as_ref()
                .unwrap()
                .call_method1(py, "info", (msg,));
        });
    }

    response
}
