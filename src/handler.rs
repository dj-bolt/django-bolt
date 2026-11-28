use actix_multipart::Multipart;
use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::{HttpRequest, HttpResponse, http::StatusCode, web};
use ahash::AHashMap;
use bytes::Bytes;
use futures_util::StreamExt;
use futures_util::stream;
use pyo3::prelude::*;
use pyo3::pybacked::{PyBackedBytes, PyBackedStr};
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use std::borrow::{Borrow, Cow};
use std::collections::HashMap;
use std::hash::Hash;
use std::io::ErrorKind;
use std::sync::Arc;
use tokio::fs::File;
use tokio::io::AsyncReadExt;

use bolt_asgi::asgi_http;
use bolt_core::error::{self, handle_python_error};
use bolt_core::form_parsing::{
    DEFAULT_MAX_FIELDS, DEFAULT_MAX_PARTS, DEFAULT_MEMORY_LIMIT, ERROR_TYPE_PAYLOAD_TOO_LARGE,
    FileContent, FileFieldConstraints, FileInfo, FormParseResult, FormValue, LimitedPayload,
    parse_multipart, parse_urlencoded,
};
use bolt_core::metadata::{RustArgBinding, RustArgSource};
use bolt_core::middleware;
use bolt_core::middleware::auth::populate_auth_context;
use bolt_core::request::{self, PyRequest};
use bolt_core::request_pipeline::{
    EMPTY_TYPES, build_validation_error_response, extract_cookie_header, extract_headers,
    query_sequences, set_query_sequences, validate_and_cache_source,
    validate_and_cache_typed_params,
};
use bolt_core::response_builder;
use bolt_core::response_meta::ResponseMeta;
use bolt_core::responses;
use bolt_core::router::{QueryParams, QuerySentLens, normalize_request_path, parse_query_string};
use bolt_core::state::{AppState, find_asgi_mount};
use bolt_core::streaming::{StreamConfig, create_python_stream, create_sse_stream};
use bolt_core::type_coercion::{
    CoercedValue, TypeHints, coerced_value_to_py, string_map_to_py_dict,
};
use bolt_core::validation::{
    AuthGuardResult, parse_cookies_inline, parse_selected_cookies, validate_auth_and_guards,
};

use std::future::Future;
use std::pin::Pin;

use crate::response_body::AttachedBytes;

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
pub(crate) const SKIP_CORS_HEADER_NAME: HeaderName = HeaderName::from_static("x-bolt-skip-cors");
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
) -> HttpResponse {
    match File::open(file_path).await {
        Ok(mut file) => {
            // Get file size
            let file_size = match file.metadata().await {
                Ok(metadata) => metadata.len(),
                Err(e) => {
                    let response = HttpResponse::InternalServerError()
                        .content_type("text/plain; charset=utf-8")
                        .body(format!("Failed to read file metadata: {}", e));
                    return error::with_error_info(response, || {
                        error::ErrorInfo::internal(format!("Failed to read file metadata: {}", e))
                    });
                }
            };

            // For small files (<10MB), read into memory for better performance
            if file_size < 10 * 1024 * 1024 {
                let mut buffer = Vec::with_capacity(file_size as usize);
                match file.read_to_end(&mut buffer).await {
                    Ok(_) => {
                        let mut builder = HttpResponse::build(status);
                        for (k, v) in headers {
                            if let Ok(name) = HeaderName::try_from(k)
                                && let Ok(val) = HeaderValue::try_from(v)
                            {
                                builder.append_header((name, val));
                            }
                        }
                        if skip_compression {
                            builder.append_header(("content-encoding", "identity"));
                        }
                        builder.body(buffer)
                    }
                    Err(e) => {
                        let response = HttpResponse::InternalServerError()
                            .content_type("text/plain; charset=utf-8")
                            .body(format!("Failed to read file: {}", e));
                        error::with_error_info(response, || {
                            error::ErrorInfo::internal(format!("Failed to read file: {}", e))
                        })
                    }
                }
            } else {
                // For large files, use streaming
                let mut builder = HttpResponse::build(status);
                for (k, v) in headers {
                    if let Ok(name) = HeaderName::try_from(k)
                        && let Ok(val) = HeaderValue::try_from(v)
                    {
                        builder.append_header((name, val));
                    }
                }
                if skip_compression {
                    builder.append_header(("content-encoding", "identity"));
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
        // FileResponse checks the file when the handler makes it. The file can
        // go away, or lose its permission, before this opens it.
        Err(e) => match e.kind() {
            ErrorKind::NotFound => {
                let response = HttpResponse::NotFound()
                    .content_type("text/plain; charset=utf-8")
                    .body("File not found");
                error::with_error_info(response, || {
                    error::ErrorInfo::core("not_found", "File not found")
                })
            }
            ErrorKind::PermissionDenied => {
                let response = HttpResponse::Forbidden()
                    .content_type("text/plain; charset=utf-8")
                    .body("Permission denied");
                error::with_error_info(response, || {
                    error::ErrorInfo::core("forbidden", "Permission denied")
                })
            }
            _ => {
                let response = HttpResponse::InternalServerError()
                    .content_type("text/plain; charset=utf-8")
                    .body(format!("File error: {}", e));
                error::with_error_info(response, || {
                    error::ErrorInfo::internal(format!("File error: {}", e))
                })
            }
        },
    }
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
/// # let result: bolt_core::form_parsing::FormParseResult = unimplemented!();
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
                let py_val = coerced_value_to_py(py, v)?;
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
                let items = vs
                    .iter()
                    .map(|v| coerced_value_to_py(py, v))
                    .collect::<PyResult<Vec<Py<PyAny>>>>()?;
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
    /// Keep Python storage alive until the HTTP body is released.
    ZeroCopyBytes(AttachedBytes),
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

/// Read the status of a response wire.
///
/// A value that is not a final HTTP status (200 to 599) is an error. It must
/// not become a success, and it must not go to the client as it is.
#[inline]
fn parse_wire_status(item: &Bound<'_, PyAny>) -> PyResult<StatusCode> {
    match item.extract::<u16>().ok().and_then(responses::final_status) {
        Some(status) => Ok(status),
        None => Err(invalid_wire_status(item)),
    }
}

#[cold]
fn invalid_wire_status(item: &Bound<'_, PyAny>) -> PyErr {
    let value = item
        .repr()
        .map(|repr| repr.to_string())
        .unwrap_or_else(|_| "?".to_string());
    // The client gets no detail of a 500 when DEBUG is off, so the log names the status.
    log::error!(
        "The response status must be an int from 200 to 599, got {value}. Bolt sends a 500."
    );
    pyo3::exceptions::PyValueError::new_err(format!(
        "The response status must be an int from 200 to 599, got {value}"
    ))
}

fn parse_response_wire(py: Python<'_>, result_obj: &Py<PyAny>) -> PyResult<ParsedResponseWire> {
    let obj = result_obj.bind(py);
    let tuple = obj.cast::<PyTuple>()?;
    if tuple.len() != 4 {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "ResponseWireV1 must be a 4-tuple: (status, meta, body_kind, body_payload)",
        ));
    }

    let status = parse_wire_status(&tuple.get_item(0)?)?;
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
                )));
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
            // Keep the Python buffer without copying its contents.
            if let Ok(backed) = payload.extract::<PyBackedBytes>() {
                ResponseWireBody::ZeroCopyBytes(AttachedBytes::from(backed))
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
            )));
        }
    };

    Ok(ParsedResponseWire { status, meta, body })
}

/// Borrow the global `CompressionConfig` from `AppState` attached to the
/// request. Returns `None` when no `BoltAPI(compression=...)` was configured.
fn compression_config_from_req(
    req: &HttpRequest,
) -> Option<&bolt_core::metadata::CompressionConfig> {
    req.app_data::<actix_web::web::Data<std::sync::Arc<bolt_core::state::AppState>>>()
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
    stream_config: StreamConfig,
) -> HttpResponse {
    let meta_ref = parsed.meta.as_ref();

    match parsed.body {
        ResponseWireBody::Bytes(body) => {
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
            let mut response = response_builder::build_response_from_meta(
                parsed.status,
                meta_ref,
                Bytes::from_owner(backed),
                skip_compression,
            );
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::FilePath(file_path) => {
            let headers = response_builder::meta_to_headers(meta_ref);
            let mut response =
                build_file_response(&file_path, parsed.status, headers, skip_compression).await;
            mark_skip_cors(&mut response, skip_cors);
            response
        }
        ResponseWireBody::Stream {
            media_type,
            content_obj,
            is_async_generator,
            ping_interval,
        } => {
            let mut headers = response_builder::meta_to_headers(meta_ref);

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
                bolt_core::streaming_compression::select_stream_encoding(
                    req,
                    compression_config_from_req(req),
                    skip_compression,
                )
            };
            let encoding_name = codec.map_or("identity", |c| c.header_name());
            if codec.is_some() {
                // RFC 9110, section 8.8.1: Bolt codes this body, so a strong
                // ETag of the plain body becomes weak (`"x"` gives `W/"x"`).
                for (name, value) in &mut headers {
                    if name.eq_ignore_ascii_case("etag") && value.starts_with('"') {
                        value.insert_str(0, "W/");
                    }
                }
            }

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
                let stream = create_sse_stream(
                    content_obj,
                    is_async_generator,
                    ping_interval,
                    codec,
                    stream_config,
                );
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
            let inner = create_python_stream(content_obj, is_async_generator, stream_config);
            let stream = bolt_core::streaming::maybe_wrap_codec(inner, codec);
            let mut response = builder.streaming(stream);
            mark_skip_cors(&mut response, skip_cors);
            response
        }
    }
}

/// Log the cause of a response that Rust cannot build, for example a status
/// that is not a u16. Python did not see this error, so Python did not log it.
/// The client gets the 500 of each other exception, which tells the cause
/// only with DEBUG on.
#[cold]
fn log_invalid_response(req: &HttpRequest, err: &PyErr) {
    log::error!(
        "{} {}: cannot build the response of the handler: {err}",
        req.method(),
        req.path()
    );
}

pub async fn response_from_wire_result(
    result_obj: Py<PyAny>,
    skip_compression: bool,
    skip_cors: bool,
    is_head_request: bool,
    req: &HttpRequest,
    stream_config: StreamConfig,
) -> PyResult<HttpResponse> {
    let parsed = Python::attach(|py| parse_response_wire(py, &result_obj))?;
    Ok(build_response_from_parsed(
        parsed,
        skip_compression,
        skip_cors,
        is_head_request,
        req,
        stream_config,
    )
    .await)
}

/// One request source as Rust holds it: the string map and its coerced values.
/// The path and query maps borrow their strings; the header and cookie maps own them.
pub(crate) struct SourceValues<'a, K = String, V = String> {
    pub values: Option<&'a AHashMap<K, V>>,
    pub coerced: &'a [(&'a str, CoercedValue)],
}

impl<K: Borrow<str> + Eq + Hash, V: AsRef<str>> SourceValues<'_, K, V> {
    /// Look up `key`: a coerced value first, then the string map.
    #[inline]
    fn get(&self, py: Python<'_>, key: &str) -> PyResult<Option<Py<PyAny>>> {
        if let Some((_, typed)) = self.coerced.iter().find(|(name, _)| *name == key) {
            return coerced_value_to_py(py, typed).map(Some);
        }
        Ok(self
            .values
            .and_then(|values| values.get(key))
            .map(|value| PyString::new(py, value.as_ref()).into_any().unbind()))
    }
}

/// Bind handler arguments straight from the Rust request maps.
///
/// `Ok(None)` means a required value is missing; the Python injector then
/// raises the 422 from the source dicts. A `prebind_only` route builds no
/// source dict when this succeeds, because nothing else reads them.
pub(crate) fn build_prebound_from_values(
    py: Python<'_>,
    bindings: &[RustArgBinding],
    path: &SourceValues<'_, &str, Cow<'_, str>>,
    query: &SourceValues<'_, Cow<'_, str>, Cow<'_, str>>,
    headers: &SourceValues<'_>,
    cookies: &SourceValues<'_>,
) -> PyResult<Option<(Py<PyList>, Py<PyDict>)>> {
    let args = PyList::empty(py);
    let kwargs = PyDict::new(py);

    for binding in bindings {
        let value = match binding.source {
            RustArgSource::Path => path.get(py, &binding.key)?,
            RustArgSource::Query => query.get(py, &binding.key)?,
            RustArgSource::Header => headers.get(py, &binding.key)?,
            RustArgSource::Cookie => cookies.get(py, &binding.key)?,
        };
        match value {
            Some(value) => {
                if binding.positional {
                    args.append(value)?;
                } else {
                    kwargs.set_item(binding.arg_name.bind(py), value)?;
                }
            }
            None => {
                if binding.required {
                    return Ok(None);
                }
                if binding.inject_none {
                    kwargs.set_item(binding.arg_name.bind(py), py.None())?;
                }
            }
        }
    }

    Ok(Some((args.unbind(), kwargs.unbind())))
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

/// Read the whole body. The total is bounded by `max_payload_size` while it
/// streams, so a chunked request (no Content-Length) is bounded too.
#[expect(
    clippy::result_large_err,
    reason = "the error is the response to send, as for the request_pipeline helpers; the path is cold"
)]
pub(crate) async fn read_body(
    payload: &mut web::Payload,
    max_payload_size: usize,
) -> Result<Vec<u8>, HttpResponse> {
    // Not sized from Content-Length: a client could claim the limit and
    // send nothing, so memory follows the bytes that arrive.
    let mut body = Vec::new();
    while let Some(chunk) = payload.next().await {
        match chunk {
            Ok(data) => {
                if data.len() > max_payload_size - body.len() {
                    return Err(responses::error_413());
                }
                body.extend_from_slice(&data);
            }
            Err(e) => {
                let response = HttpResponse::BadRequest()
                    .content_type("application/json")
                    .body(format!(
                        "{{\"error\": \"Failed to read request body: {}\"}}",
                        e
                    ));
                return Err(error::with_error_info(response, || {
                    error::ErrorInfo::core("bad_request", "Failed to read request body")
                }));
            }
        }
    }
    Ok(body)
}

pub async fn handle_request<const ACCESS_LOG: bool>(
    req: HttpRequest,
    payload: web::Payload,
    state: web::Data<Arc<AppState>>,
) -> HttpResponse {
    // Without the access log and an error formatter, this is the whole cost: one Option check.
    if !ACCESS_LOG && state.error_renderer.is_none() {
        return route_request(req, payload, state, &mut None).await;
    }
    // ACCESS_LOG: the compiler removes the timer when ACCESS_LOG=false (const generic).
    let access_log_start = if ACCESS_LOG {
        Some(std::time::Instant::now())
    } else {
        None
    };
    let kept_req = req.clone();
    let kept_state = state.clone();
    let mut access_log_route = None;
    let mut response = route_request(req, payload, state, &mut access_log_route).await;
    if kept_state.error_renderer.is_some() && response.status().as_u16() >= 400 {
        response = crate::error_formatter::format_error_response(response, &kept_req, &kept_state);
    }
    if let Some(start) = access_log_start
        && let Some(handler_id) = access_log_route
    {
        write_access_log(
            &kept_state,
            &kept_req,
            response.status().as_u16(),
            start,
            handler_id,
        );
    }
    response
}

/// The level bit and the logger method for the access-log line of `status`.
/// The bits are those of `AccessLogger::levels`.
#[inline]
fn access_log_level(status: u16) -> (u8, &'static str) {
    if status >= 500 {
        (4, "error")
    } else if status >= 400 {
        (2, "warning")
    } else {
        (1, "info")
    }
}

/// Write the access-log line of one response to the logger of the route
/// `handler_id`. The filter of the route holds the index of its logger, the
/// skipped paths and status codes, the sample rate and the slow-request
/// threshold. A 4xx or a 5xx always passes the last two. A route with no
/// metadata has no filter and writes to `django.server`.
fn write_access_log(
    state: &AppState,
    req: &HttpRequest,
    status: u16,
    start: std::time::Instant,
    handler_id: usize,
) {
    let (bit, level) = access_log_level(status);
    let meta = state.route_metadata.get(handler_id);
    let logger_index = meta.map_or(0, |meta| meta.access_log_filter.logger);
    let Some(logger) = state.access_loggers.get(logger_index) else {
        return;
    };
    if logger.levels & bit == 0 {
        return;
    }
    let dur_ms = start.elapsed().as_secs_f64() * 1000.0;
    if let Some(meta) = meta {
        let filter = &meta.access_log_filter;
        if filter.skips(req.path(), status) || !filter.keeps(status, dur_ms) {
            return;
        }
    }
    // A flood of 429 responses writes one line for each second and logger on
    // this worker. A 429 that the filter skips takes no line and is not in the count.
    let suppressed = if status == 429 {
        match bolt_core::access_log::rejection_line_at(logger_index, start) {
            Some(suppressed) => suppressed,
            None => return,
        }
    } else {
        0
    };
    let msg = format!(
        "{} {} {} {:.1}ms",
        req.method().as_str(),
        req.path(),
        status,
        dur_ms
    );
    // The line of a 429 counts the 429 responses before it that wrote no line.
    let msg = if suppressed == 0 {
        msg
    } else {
        format!("{msg} | suppressed: {suppressed}")
    };
    Python::attach(|py| {
        let _ = logger.logger.call_method1(py, level, (msg,));
    });
}

/// The error formatter does not change the responses of an MCP or an ASGI mount.
/// Some of their errors come from shared builders, so remove the `ErrorInfo` here.
#[inline]
fn without_error_info(mut response: HttpResponse, state: &AppState) -> HttpResponse {
    if state.error_renderer.is_some() {
        response.extensions_mut().remove::<error::ErrorInfo>();
    }
    response
}

/// The content type and the other headers of an answer of Rust, as text for
/// the middleware. The middleware chain sends a header again from its text.
/// A value that is not visible ASCII cannot go back into a response, so this
/// logs it and leaves it out, as the response builder does.
fn answer_headers(map: &actix_web::http::header::HeaderMap) -> (String, Vec<(String, String)>) {
    let mut content_type = String::new();
    let mut headers = Vec::with_capacity(map.len());
    for (name, value) in map {
        if *name == actix_web::http::header::CONTENT_LENGTH || *name == SKIP_CORS_HEADER_NAME {
            continue;
        }
        let Ok(value) = value.to_str() else {
            log::warn!(
                "The response header '{name}' is not ASCII text. The middleware does not get it, and Bolt does not send it."
            );
            continue;
        };
        if *name == actix_web::http::header::CONTENT_TYPE {
            content_type = value.to_owned();
        } else {
            headers.push((name.as_str().to_owned(), value.to_owned()));
        }
    }
    (content_type, headers)
}

/// A response that Rust gave before the handler, read for the Python middleware.
struct RustAnswer {
    status: u16,
    content_type: String,
    headers: Vec<(String, String)>,
    body: Bytes,
}

impl RustAnswer {
    /// Read the parts of `answer`. With an error formatter, the middleware gets
    /// the formatted response, as the client would without middleware.
    #[expect(
        clippy::result_large_err,
        reason = "the error is the response to send; the path is cold"
    )]
    fn read(
        answer: HttpResponse,
        req: &HttpRequest,
        state: &Arc<AppState>,
    ) -> Result<Self, HttpResponse> {
        let answer = if state.error_renderer.is_some() {
            crate::error_formatter::format_error_response(answer, req, state)
        } else {
            answer
        };
        let status = answer.status().as_u16();
        let (content_type, headers) = answer_headers(answer.headers());
        // The body of a response that Rust builds is in memory.
        let body = match actix_web::body::MessageBody::try_into_bytes(answer.into_body()) {
            Ok(body) => body,
            Err(_) => {
                return Err(error::with_error_info(
                    HttpResponse::InternalServerError().finish(),
                    || error::ErrorInfo::core("internal_error", "The response has no bytes body"),
                ));
            }
        };
        Ok(Self {
            status,
            content_type,
            headers,
            body,
        })
    }

    /// The ResponseWireV1 of the answer: `(status, meta, 0, body)`. An empty
    /// content type means none.
    fn into_wire(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let headers = if self.headers.is_empty() {
            py.None()
        } else {
            self.headers.into_pyobject(py)?.into_any().unbind()
        };
        let meta = (
            pyo3::intern!(py, "octetstream"),
            self.content_type,
            headers,
            py.None(),
        );
        Ok((self.status, meta, 0u8, PyBytes::new(py, &self.body))
            .into_pyobject(py)?
            .into_any()
            .unbind())
    }
}

/// Route one request. `access_log_route` becomes the handler id of the route
/// when the response is for a Bolt route: each response after the route match,
/// the redirect to the other slash form of a route path, the automatic OPTIONS
/// answer, and the 405 for a method that the route path does not have.
/// A 404 of a path with no route gets the no-route route of the app.
/// `handle_request` writes the access-log line for these, if
/// the filter of that route keeps it. A native route, a mount and the
/// preflight answer of global CORS leave it `None`.
async fn route_request(
    req: HttpRequest,
    mut payload: web::Payload,
    state: web::Data<Arc<AppState>>,
    access_log_route: &mut Option<usize>,
) -> HttpResponse {
    // Keep as &str - no allocation, only clone on error paths
    let method = req.method().as_str();
    let path = req.path();

    let router = &state.router;
    // The router compares one form of each `%XX` escape. A path with no `%`
    // stays borrowed.
    let route_path = normalize_request_path(path);
    let route_path = route_path.as_ref();

    // Find the route for the requested method and path.
    // RouteMatch enum allows us to skip path param processing for static routes.
    // The route reference borrows the 'static global router, so the handler
    // clone_ref is deferred into the main dispatch GIL block — no extra
    // Python::attach per request just to clone the callable.
    //
    // `rust_answer` holds a response that Rust gives before the handler: a
    // 404 or a 405 for a request with no route, and a 401 or a 403 of the
    // route. When the route runs Python middleware, the response goes through
    // that middleware as the response of the handler (see `RustAnswer`).
    let (route, path_params, handler_id, mut rust_answer) = {
        if let Some(route_match) = router.find(method, route_path) {
            let handler_id = route_match.handler_id();
            let (route, path_params) = route_match.into_parts();
            (route, path_params, handler_id, None)
        } else {
            // No route found - check for trailing slash redirect FIRST
            // This only runs when route doesn't match (minimal overhead)
            // Starlette-style: redirect to canonical URL if alternate path exists
            let alternate_path = if route_path == "/" {
                None
            } else if route_path.ends_with('/') {
                Some(route_path.trim_end_matches('/').to_string())
            } else {
                Some(format!("{}/", route_path))
            };

            // Try alternate path - if it matches, send 308 redirect
            if let Some(alternate_path) = &alternate_path
                && let Some(alternate) = router.find(method, alternate_path)
            {
                // The line of the redirect has the filter of the route at the other form.
                *access_log_route = Some(alternate.handler_id());
                let query = req.query_string();
                // The client sees the path behind the script name (FORCE_SCRIPT_NAME).
                let mut location =
                    request::client_path(state.script_name.as_deref(), alternate_path.clone());
                if !query.is_empty() {
                    location.push('?');
                    location.push_str(query);
                }
                return HttpResponse::PermanentRedirect() // 308
                    .insert_header(("Location", location))
                    .finish();
            }

            // No explicit handler found - check for automatic OPTIONS
            if method == "OPTIONS" {
                let available_methods = router.find_all_methods(route_path);
                if !available_methods.is_empty() {
                    let allow_header = available_methods.join(", ");
                    // CORS headers will be added by CorsMiddleware
                    // The line of this answer has the filter of the first route of the path.
                    *access_log_route = router
                        .find(&available_methods[0], route_path)
                        .map(|found| found.handler_id());
                    return HttpResponse::NoContent()
                        .insert_header(("Allow", allow_header))
                        .insert_header(("Content-Type", "application/json"))
                        .finish();
                }
            }

            // MCP mount check: only on router miss, so the non-MCP hot path
            // pays nothing. Before the ASGI fallback so an overlapping prefix
            // cannot shadow an MCP endpoint.
            if let Some(mcp_mount) = bolt_mcp::find_mcp_mount(state.get_ref(), path) {
                let response =
                    bolt_mcp::bridge::handle_mcp_request(req, payload, mcp_mount, state.get_ref())
                        .await;
                return without_error_info(response, &state);
            }

            // The path has routes, but none for this method: 405 with `Allow`
            // (RFC 9110 section 15.5.6). This lookup runs on a miss only, and before
            // the ASGI mounts, so a mount at "/" does not take a path of a route.
            // The other slash form of the path gets the same check. That form
            // redirects for a method of its routes, so the path is a route path.
            let mut allowed_path = route_path;
            let mut allowed_methods = router.find_all_methods(route_path);
            let other_form = allowed_methods.is_empty();
            if other_form && let Some(alternate_path) = &alternate_path {
                allowed_path = alternate_path;
                allowed_methods = router.find_all_methods(alternate_path);
            }
            let answer = if !allowed_methods.is_empty() {
                // The line of this answer has the filter of the first route of the path.
                *access_log_route = router
                    .find(&allowed_methods[0], allowed_path)
                    .map(|found| found.handler_id());
                // The automatic OPTIONS answer of the route path.
                if other_form && method == "OPTIONS" {
                    return HttpResponse::NoContent()
                        .insert_header(("Allow", allowed_methods.join(", ")))
                        .insert_header(("Content-Type", "application/json"))
                        .finish();
                }
                responses::error_405(&allowed_methods)
            } else {
                // HTTP ASGI mount fallback:
                // - only after Bolt route miss
                // - only after trailing-slash/API-method near-miss checks above
                if let Some(asgi_mount) = find_asgi_mount(state.get_ref(), path) {
                    let response =
                        asgi_http::handle_asgi_mount_request(req, payload, asgi_mount, &state)
                            .await;
                    return without_error_info(response, &state);
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
                responses::error_404()
            };

            // The no-route route of the app has the access-log filter of the
            // API. When the API has Python middleware, the answer goes through it.
            // A 405 keeps the filter of the route of its path. With merged APIs,
            // that route can be of another API than the no-route route.
            let Some(no_route) = router.no_route() else {
                return answer;
            };
            if access_log_route.is_none() {
                *access_log_route = Some(no_route.handler_id);
            }
            let runs_middleware = state
                .route_metadata
                .get(no_route.handler_id)
                .is_some_and(|meta| meta.plan.runs_python_middleware());
            if !runs_middleware {
                return answer;
            }
            (no_route, None, no_route.handler_id, Some(answer))
        }
    };

    // A native route goes to a Rust handler in another extension module. Auth,
    // guards, rate limits, Python middleware and the access log do not run;
    // Actix middleware such as CORS and compression does. The boxed future
    // keeps the future of this function small for Python routes.
    if let Some(native) = route.native {
        return Box::pin(crate::native::dispatch(
            &req,
            payload,
            native,
            path_params,
            &state,
        ))
        .await;
    }

    // Method maps to a static str (known fixed set — no per-request alloc);
    // path is dynamic and must be owned for PyRequest.
    let method_static = static_method_name(method);
    let path_owned = path.to_string();

    // Get parsed route metadata (Rust-native) by reference.
    // NOTE: Fetch metadata EARLY so we can use optimization flags to skip unnecessary parsing.
    let route_metadata = state.route_metadata.get(handler_id);
    // A 405 keeps the filter of the route of its path.
    if access_log_route.is_none() {
        *access_log_route = Some(handler_id);
    }

    // OPTIMIZATION: Extract the execution plan bitfield once (Copy u16).
    // All subsequent flag reads are direct bit-tests, avoiding repeated Option::map closures.
    let plan = route_metadata.map(|m| m.plan);

    let needs_query = plan.is_none_or(|p| p.needs_query());
    // The map borrows its keys and values from the URI when they need no decode.
    let query_string = if needs_query { req.uri().query() } else { None };
    let query_params: Option<QueryParams<'_>> = query_string
        .map(parse_query_string)
        .filter(|parsed| !parsed.is_empty());

    let needs_body = plan.is_none_or(|p| p.needs_body());

    // Max parameter length resolved once at startup; read the plain field here.
    let max_param_length = state.max_param_length;

    // Each length check of a query value shares one pass over the raw query.
    let mut query_lens = QuerySentLens::new(query_string.unwrap_or_default(), max_param_length);

    // A no-route answer has no handler, so no query value is checked for it.
    // Thus a 422 cannot take the place of the 404 or the 405.
    let check_query = rust_answer.is_none();

    // A sequence query parameter takes each value of its repeated key.
    let query_sequences = match route_metadata {
        Some(route_meta) if needs_query && check_query => {
            match query_sequences(&mut query_lens, &route_meta.query_seq_fields) {
                Ok(sequences) => sequences,
                Err(error) => return error.into_response(),
            }
        }
        _ => Vec::new(),
    };

    // Type validation for path and query parameters (Rust-native, no GIL)
    let (path_coerced, query_coerced) =
        if let Some(route_meta) = route_metadata.filter(|_| check_query) {
            match validate_and_cache_typed_params(
                path_params.as_ref(),
                query_params.as_ref(),
                &mut query_lens,
                &route_meta.path_types,
                &route_meta.query_types,
                max_param_length,
            ) {
                Ok(cached) => cached,
                Err(response) => return response,
            }
        } else {
            (Vec::new(), Vec::new())
        };

    let needs_headers = plan.is_none_or(|p| p.needs_headers());
    let needs_cookies = plan.is_none_or(|p| p.needs_cookies());
    let needs_form_parsing = plan.is_some_and(|p| p.needs_form_parsing());
    let has_route_auth_or_guards = plan.is_some_and(|p| p.has_auth_or_guards());
    let has_route_rate_limit = plan.is_some_and(|p| p.has_rate_limit());
    let must_extract_headers =
        needs_headers || needs_form_parsing || has_route_auth_or_guards || has_route_rate_limit;
    let skip_cors = plan.is_some_and(|p| p.skip_cors());
    let skip_compression = plan.is_some_and(|p| p.skip_compression());
    let can_sync_dispatch = plan.is_some_and(|p| p.can_sync_dispatch());
    let can_lane_dispatch = plan.is_some_and(|p| p.can_lane_dispatch());
    let is_async_handler = plan.is_some_and(|p| p.is_async());
    let runs_python_middleware = plan.is_some_and(|p| p.runs_python_middleware());

    // Extract and validate headers
    let mut headers = if must_extract_headers {
        match extract_headers(&req, state.max_header_size) {
            Ok(h) => Some(h),
            Err(response) => return response,
        }
    } else {
        None
    };

    // Routes that do not extract headers skip client-address work entirely.
    // Routes that do need connection metadata and IP rate limiting share this
    // one canonical result. With no trusted proxy this is one `peer_addr()`.
    let client_ip = if must_extract_headers {
        middleware::client_ip::resolve_from_headers(
            req.headers(),
            req.peer_addr().map(|address| address.ip()),
            &state.trusted_proxies,
        )
    } else {
        None
    };

    // Rate limiting (Rust-native, no GIL). Address and header keys run before
    // authentication so a flood never pays for token verification. Identity
    // keys run after it; see the second call below.
    let rate_config = route_metadata.and_then(|m| m.rate_limit_config.as_ref());
    if let Some(config) = rate_config {
        if let Some(headers_map) = headers.as_ref() {
            if let Some(response) = middleware::rate_limit::check_before_auth(
                &state.rate_limiters,
                handler_id,
                headers_map,
                client_ip.as_ref(),
                config,
                method,
                path,
            ) {
                // A 429 stays in Rust: the limit sheds load before Python
                // runs. CORS headers will be added by CorsMiddleware.
                return response;
            }
        } else {
            debug_assert!(false, "rate-limited route missing extracted headers");
        }
    }

    // Execute authentication and guards using shared validation logic.
    let mut auth_ctx = None;
    if has_route_auth_or_guards && let Some(route_meta) = route_metadata {
        let empty_headers = AHashMap::new();
        let headers_map = headers.as_ref().unwrap_or(&empty_headers);
        let mut rejection = match validate_auth_and_guards(
            headers_map,
            &route_meta.auth_backends,
            &route_meta.guards,
        ) {
            AuthGuardResult::Allow(ctx) => {
                auth_ctx = ctx;
                None
            }
            // The Python dispatch checks the guards against the Django user.
            AuthGuardResult::Deferred(ctx) => {
                auth_ctx = Some(ctx);
                None
            }
            AuthGuardResult::Unauthorized => Some(responses::error_401_challenge(
                route_meta.www_authenticate.as_ref(),
            )),
            AuthGuardResult::Forbidden(denial) => {
                Some(responses::error_403_denial(denial.as_deref()))
            }
        };
        // A safe method passes the check, so it needs no connection info.
        let requires_csrf = auth_ctx.as_ref().is_some_and(|auth| auth.cookie_csrf)
            && !bolt_core::validation::is_safe_method(method);
        // Any peer may set the scheme here. See `passes_cookie_csrf`.
        if requires_csrf
            && bolt_core::validation::cookie_csrf_blocks(
                method,
                headers_map,
                true,
                req.connection_info().scheme(),
            )
        {
            rejection = Some(responses::error_403());
        }
        if let Some(response) = rejection {
            // CORS headers will be added by CorsMiddleware.
            if !runs_python_middleware {
                return response;
            }
            rust_answer = Some(response);
            auth_ctx = None;
        }
    }

    // A rejected request does not count against an identity limit.
    if rust_answer.is_none()
        && let Some(config) = rate_config
        && let Some(headers_map) = headers.as_ref()
        && let Some(response) = middleware::rate_limit::check_after_auth(
            &state.rate_limiters,
            handler_id,
            headers_map,
            client_ip.as_ref(),
            auth_ctx.as_ref(),
            config,
            method,
            path,
        )
    {
        // CORS headers will be added by CorsMiddleware
        return response;
    }

    // Optimization: Only parse cookies if handler needs them
    // Cookie parsing can be expensive for requests with many cookies
    let mut cookies = if needs_cookies {
        let cookie_header = if let Some(headers) = &headers {
            headers
                .get("cookie")
                .map(|value| Cow::Borrowed(value.as_str()))
        } else {
            match extract_cookie_header(&req, state.max_header_size) {
                Ok(value) => value,
                Err(response) => return response,
            }
        };
        Some(
            if let Some(meta) = route_metadata.filter(|meta| meta.prebind_only) {
                parse_selected_cookies(
                    cookie_header.as_deref(),
                    |name| {
                        meta.rust_arg_bindings.as_ref().is_some_and(|bindings| {
                            bindings.iter().any(|binding| {
                                binding.source == RustArgSource::Cookie && binding.key == name
                            })
                        })
                    },
                    max_param_length,
                )
            } else {
                parse_cookies_inline(cookie_header.as_deref())
            },
        )
    } else {
        None
    };

    // Type validation for the header and cookie values that Python receives
    // (Rust-native, no GIL). Same length limit and 422 format as path/query.
    let empty_types: &TypeHints = &EMPTY_TYPES;
    let header_types = route_metadata.map_or(empty_types, |m| &m.header_types);
    let cookie_types = route_metadata.map_or(empty_types, |m| &m.cookie_types);
    // A request that Rust answers does not reach the handler, so it has no
    // values to check. Thus a 422 cannot take the place of the answer.
    let answered = rust_answer.is_some();
    let headers_coerced = match headers.as_ref() {
        Some(headers_map) if needs_headers && !answered => {
            match validate_and_cache_source(headers_map, header_types, max_param_length, "Header") {
                Ok(cached) => cached,
                Err(response) => return response,
            }
        }
        _ => Vec::new(),
    };
    let cookies_coerced = match cookies.as_ref() {
        Some(cookies_map) if !answered => {
            match validate_and_cache_source(cookies_map, cookie_types, max_param_length, "Cookie") {
                Ok(cached) => cached,
                Err(response) => return response,
            }
        }
        _ => Vec::new(),
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
        // Same trust rule as the client address: only a trusted proxy sets the
        // scheme with `X-Forwarded-Proto`.
        let scheme = middleware::client_ip::resolve_scheme(
            req.headers(),
            req.peer_addr().map(|address| address.ip()),
            req.app_config().secure(),
            &state.trusted_proxies,
        );
        (host, scheme, client_ip)
    } else {
        // No META needed → empty strings (META getter will return defaults)
        (String::new(), "", None)
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
    // Bolt does not read the body of a request that Rust answers: a flood of
    // rejected requests reads no body, and a 413 cannot replace the answer.
    let reads_body = (needs_body || needs_form_parsing) && !answered;
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
    let (body, form_result): (Vec<u8>, Option<FormParseResult>) = if !reads_body {
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

        // Create Multipart from the payload. The limit covers each byte of the body,
        // as the parser buffers a part header with no limit of its own.
        let (limited, exceeded) = LimitedPayload::new(payload, state.max_payload_size);
        let multipart = Multipart::new(req.headers(), limited);

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
                if exceeded.get() || validation_error.error_type == ERROR_TYPE_PAYLOAD_TOO_LARGE {
                    return responses::error_413();
                }
                return build_validation_error_response(&validation_error);
            }
        }
    } else {
        let body_vec = match read_body(&mut payload, state.max_payload_size).await {
            Ok(body) => body,
            Err(response) => return response,
        };

        // URL-encoded form parsing
        if needs_form_parsing && is_urlencoded {
            let empty_form_type_hints: HashMap<String, u8> = HashMap::new();
            let form_type_hints = route_metadata
                .map(|m| &m.form_type_hints)
                .unwrap_or(&empty_form_type_hints);

            match parse_urlencoded(
                &body_vec,
                form_type_hints,
                max_param_length,
                DEFAULT_MAX_FIELDS,
            ) {
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

    // The middleware gets the answer in the form that the client would get.
    let rust_answer = match rust_answer {
        Some(answer) => match RustAnswer::read(answer, &req, &state) {
            Ok(answer) => Some(answer),
            Err(response) => return response,
        },
        None => None,
    };

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

        // Bind the handler arguments from the Rust maps. A prebind_only route
        // then needs no Python source dicts.
        let state_lock = std::sync::OnceLock::new();
        let mut skip_dicts = false;
        // The innermost layer of the middleware chain returns the wire of an
        // answer. A route that runs Python middleware has no sync dispatch,
        // so the wire goes to that chain.
        debug_assert!(rust_answer.is_none() || !can_sync_dispatch);
        let rust_answer = rust_answer.map(|answer| answer.into_wire(py)).transpose()?;
        // The handler does not run for an answer, so it gets no arguments.
        if rust_answer.is_none()
            && let Some(bindings) = route_metadata.and_then(|m| m.rust_arg_bindings.as_deref())
        {
            let prebound = build_prebound_from_values(
                py,
                bindings,
                &SourceValues {
                    values: path_params.as_ref(),
                    coerced: &path_coerced,
                },
                &SourceValues {
                    values: query_params.as_ref(),
                    coerced: &query_coerced,
                },
                &SourceValues {
                    values: headers.as_ref(),
                    coerced: &headers_coerced,
                },
                &SourceValues {
                    values: cookies.as_ref(),
                    coerced: &cookies_coerced,
                },
            );
            if let Some((pre_args, pre_kwargs)) = prebound? {
                let state_dict = PyDict::new(py);
                state_dict.set_item(pyo3::intern!(py, "_bolt_prebound_args"), pre_args)?;
                state_dict.set_item(pyo3::intern!(py, "_bolt_prebound_kwargs"), pre_kwargs)?;
                let _ = state_lock.set(state_dict.unbind());
                skip_dicts = route_metadata.is_some_and(|m| m.prebind_only);
            }
        }

        // OPTIMIZATION: Create typed PyDicts only when non-empty.
        // Saves 1 Python heap alloc per empty source (up to 4 for simple API handlers).
        let path_params_py: Option<Py<PyDict>> = match path_params.as_ref() {
            Some(_) if skip_dicts => None,
            Some(path_params) => {
                Some(string_map_to_py_dict(py, path_params, &path_coerced)?.unbind())
            }
            None => None,
        };

        let query_params_py: Option<Py<PyDict>> = match query_params.as_ref() {
            Some(_) if skip_dicts => None,
            Some(query_params) => {
                let query_dict = string_map_to_py_dict(py, query_params, &query_coerced)?;
                set_query_sequences(py, &query_dict, &query_sequences)?;
                Some(query_dict.unbind())
            }
            None => None,
        };

        let headers_py: Option<Py<PyDict>> = if needs_headers && !skip_dicts {
            if let Some(headers_map) = headers.as_ref() {
                Some(string_map_to_py_dict(py, headers_map, &headers_coerced)?.unbind())
            } else {
                Some(PyDict::new(py).unbind())
            }
        } else {
            None
        };
        let cookies_py: Option<Py<PyDict>> = if needs_cookies && !skip_dicts {
            if let Some(cookies_map) = cookies.as_ref() {
                Some(string_map_to_py_dict(py, cookies_map, &cookies_coerced)?.unbind())
            } else {
                Some(PyDict::new(py).unbind())
            }
        } else {
            None
        };
        // A typed header or cookie replaces its string in the dicts above.
        // Django middleware reads the original strings, so keep the Rust maps
        // when a value was converted. A move, no copy.
        let keep_raw_headers = !headers_coerced.is_empty();
        let keep_raw_cookies = !cookies_coerced.is_empty();

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
            path: request::client_path(state.script_name.as_deref(), path_owned),
            body,
            path_params: path_params_py,
            query_params: query_params_py,
            query_string: query_string.map(str::to_owned).unwrap_or_default(),
            headers: headers_py,
            cookies: cookies_py,
            raw_headers: if keep_raw_headers {
                headers.take()
            } else {
                None
            },
            raw_cookies: if keep_raw_cookies {
                cookies.take()
            } else {
                None
            },
            context,
            user: None,
            anonymous_user: pyo3::sync::PyOnceLock::new(),
            state: state_lock,
            form_map: form_map_opt,
            files_map: files_map_opt,
            meta_cache: std::sync::OnceLock::new(),
            conn_host,
            conn_scheme,
            conn_remote_addr,
            rust_answer,
            script_name: state.script_name.clone(),
        };
        let request_obj = Py::new(py, request)?;

        if can_sync_dispatch {
            // SYNC PATH: Call dispatch_sync directly → returns wire tuple or bare
            // body bytes (not a coroutine). Parse and build the HTTP response in
            // the same GIL block. Eliminates: coroutine creation,
            // into_future_with_locals, asyncio polling.
            let result_obj = if is_async_handler {
                bolt_loop::dispatch_sync(py, &route.dispatch_sync, request_obj.as_any())?
            } else {
                route.dispatch_sync.call1(py, (request_obj.bind(py),))?
            };

            // Bare-bytes fast path: sync executors return just the encoded JSON
            // body when (status, meta) == (route default, JSON). Skips the
            // 4-tuple construction in Python and its parsing here.
            if result_obj.bind(py).is_instance_of::<PyBytes>() {
                let backed = AttachedBytes::from(result_obj.extract::<PyBackedBytes>(py)?);
                let status = route_metadata
                    .map(|m| m.default_status_code)
                    .unwrap_or(StatusCode::OK);
                let mut resp = response_builder::build_response_from_meta(
                    status,
                    &bolt_core::response_meta::STATIC_META_JSON,
                    Bytes::from_owner(backed),
                    skip_compression,
                );
                mark_skip_cors(&mut resp, skip_cors);
                return Ok(DispatchOutcome::Ready(resp));
            }

            // A sync executor returns a callable when its result needs the event
            // loop, for example a QuerySet that must load on the ORM pool. A wire
            // is bytes or a tuple, so it is never callable. Run the call on the
            // worker loop, as the async path runs the dispatch.
            if result_obj.bind(py).is_callable() {
                let fut = bolt_loop::dispatch(result_obj, request_obj.into_any());
                return Ok(DispatchOutcome::Pending(Box::pin(fut)));
            }

            let parsed = parse_response_wire(py, &result_obj)
                .inspect_err(|err| log_invalid_response(&req, err))?;
            let meta_ref = parsed.meta.as_ref();
            let response = match parsed.body {
                ResponseWireBody::ZeroCopyBytes(backed) => {
                    let mut resp = response_builder::build_response_from_meta(
                        parsed.status,
                        meta_ref,
                        Bytes::from_owner(backed),
                        skip_compression,
                    );
                    mark_skip_cors(&mut resp, skip_cors);
                    resp
                }
                ResponseWireBody::Bytes(body) => {
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
        } else if can_lane_dispatch {
            // LANE PATH: one lane thread runs the complete sync request (bolt_core::lane).
            let fut = bolt_core::lane::dispatch(
                route.dispatch_sync.clone_ref(py),
                request_obj.into_any(),
            );
            Ok(DispatchOutcome::Pending(Box::pin(fut)))
        } else {
            // ASYNC PATH: submit to this worker thread's WorkerLoop, whose
            // ready queue is serviced by a persistent Tokio pump on this thread.
            let dispatch = route.dispatch.clone_ref(py);
            let fut = bolt_loop::dispatch(dispatch, request_obj.into());
            Ok(DispatchOutcome::Pending(Box::pin(fut)))
        }
    });

    let mut response = match dispatch_result {
        Ok(DispatchOutcome::Ready(response)) => response,
        // Sync dispatch completed but body needs async post-processing (stream/file).
        // Already parsed — no duplicate GIL acquire or wire re-parse.
        Ok(DispatchOutcome::SyncResult(parsed)) => {
            build_response_from_parsed(
                parsed,
                skip_compression,
                skip_cors,
                is_head_request,
                &req,
                state.stream_config,
            )
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
                    state.stream_config,
                )
                .await
                {
                    Ok(response) => response,
                    Err(e) => {
                        log_invalid_response(&req, &e);
                        Python::attach(|py| {
                            handle_python_error(
                                py,
                                e,
                                req.path(),
                                req.method().as_str(),
                                state.debug,
                            )
                        })
                    }
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

    // RFC 9110, section 9.3.2: a HEAD answer has the header fields of the GET
    // answer and no body. The compression middleware reads the size of the
    // GET body, so it sets the same coding fields.
    if is_head_request {
        response = bolt_core::middleware::compression::head_answer(response);
    }

    // A 401 from Python gets the challenge of the route, as a 401 from Rust.
    responses::add_challenge(
        &mut response,
        route_metadata.and_then(|m| m.www_authenticate.as_ref()),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::{SKIP_CORS_HEADER_NAME, access_log_level, answer_headers};
    use actix_web::http::header::{
        CONTENT_LENGTH, CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue,
    };

    #[test]
    fn the_middleware_gets_each_text_header_of_an_answer() {
        let mut map = HeaderMap::new();
        map.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        map.insert(CONTENT_LENGTH, HeaderValue::from_static("2"));
        map.insert(SKIP_CORS_HEADER_NAME, HeaderValue::from_static("1"));
        map.insert(
            HeaderName::from_static("allow"),
            HeaderValue::from_static("GET, POST"),
        );
        // A value with a byte that is not ASCII cannot go back into a response.
        map.insert(
            HeaderName::from_static("x-name"),
            HeaderValue::from_bytes(b"caf\xe9").unwrap(),
        );

        let (content_type, headers) = answer_headers(&map);

        assert_eq!(content_type, "application/json");
        assert_eq!(headers, [("allow".to_owned(), "GET, POST".to_owned())]);
    }

    #[test]
    fn the_access_log_level_follows_the_status_class() {
        // The bits are INFO = 1, WARNING = 2 and ERROR = 4.
        for (status, expected) in [
            (200, (1, "info")),
            (204, (1, "info")),
            (308, (1, "info")),
            (399, (1, "info")),
            (400, (2, "warning")),
            (401, (2, "warning")),
            (429, (2, "warning")),
            (499, (2, "warning")),
            (500, (4, "error")),
            (503, (4, "error")),
        ] {
            assert_eq!(access_log_level(status), expected, "status {status}");
        }
    }
}
