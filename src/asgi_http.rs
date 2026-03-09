use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::http::{Method, StatusCode, Version};
use actix_web::{web, HttpRequest, HttpResponse};
use bytes::Bytes;
use futures_util::{stream, StreamExt};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::{mpsc, oneshot, Mutex as AsyncMutex, Notify};

use crate::handler::handle_python_error;
use crate::state::{AsgiMount, TASK_LOCALS};

struct AsgiResponseStart {
    status: u16,
    headers: Vec<(Vec<u8>, Vec<u8>)>,
}

/// Shared state between `AsgiSend`, `AsgiDoneCallback`, and the Rust handler.
struct AsgiSendState {
    /// Fires once on `http.response.start`; dropped by `AsgiDoneCallback` if
    /// the coroutine finishes before sending, causing `start_rx` to error.
    response_start_tx: Mutex<Option<oneshot::Sender<AsgiResponseStart>>>,
    body_tx: Mutex<Option<mpsc::Sender<Bytes>>>,
    /// Exception message from the ASGI coroutine, set by `AsgiDoneCallback`.
    exception_msg: Mutex<Option<String>>,
}

const ASGI_MOUNT_BODY_CHANNEL_CAPACITY: usize = 32;

#[pyclass]
struct AsgiReceive {
    body: Arc<AsyncMutex<Option<Vec<u8>>>>,
    response_done: Arc<Notify>,
}

#[pymethods]
impl AsgiReceive {
    fn __call__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let body = self.body.clone();
        let response_done = self.response_done.clone();

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut body_guard = body.lock().await;
            if let Some(request_body) = body_guard.take() {
                return Python::attach(|py| {
                    let message = PyDict::new(py);
                    message.set_item("type", "http.request")?;
                    message.set_item("body", PyBytes::new(py, &request_body))?;
                    message.set_item("more_body", false)?;
                    Ok(message.into_any().unbind())
                });
            }
            drop(body_guard);

            response_done.notified().await;
            Python::attach(|py| {
                let message = PyDict::new(py);
                message.set_item("type", "http.disconnect")?;
                Ok(message.into_any().unbind())
            })
        })
    }
}

#[pyclass]
struct AsgiSend {
    state: Arc<AsgiSendState>,
}

#[pymethods]
impl AsgiSend {
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        message: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let msg_type: String = message
            .get_item("type")?
            .ok_or_else(|| PyValueError::new_err("Missing ASGI message type"))?
            .extract()?;

        match msg_type.as_str() {
            "http.response.start" => {
                let status: u16 = message
                    .get_item("status")?
                    .ok_or_else(|| PyValueError::new_err("Missing status in http.response.start"))?
                    .extract()?;
                let headers = parse_asgi_headers(message)?;

                let mut start_guard = self
                    .state
                    .response_start_tx
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("Failed to lock response_start_tx"))?;

                match start_guard.take() {
                    Some(tx) => {
                        let _ = tx.send(AsgiResponseStart { status, headers });
                    }
                    None => {
                        return Err(PyRuntimeError::new_err(
                            "http.response.start sent more than once",
                        ));
                    }
                }
            }
            "http.response.body" => {
                let body = parse_asgi_body(message)?;
                let more_body = message
                    .get_item("more_body")?
                    .map(|item| item.extract::<bool>())
                    .transpose()?
                    .unwrap_or(false);

                let mut tx_guard = self
                    .state
                    .body_tx
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("Failed to lock body channel"))?;

                let tx = if let Some(ref tx) = *tx_guard {
                    let tx = tx.clone();
                    if !more_body {
                        // Mark the stream as closed immediately so duplicate body messages fail fast.
                        tx_guard.take();
                    }
                    Some(tx)
                } else if more_body || !body.is_empty() {
                    return Err(PyRuntimeError::new_err(
                        "Response body channel is already closed",
                    ));
                } else {
                    None
                };

                return pyo3_async_runtimes::tokio::future_into_py(py, async move {
                    if let Some(tx) = tx {
                        if !body.is_empty() {
                            if tx.send(Bytes::from(body)).await.is_err() {
                                log::debug!(
                                    "ASGI mount: body chunk dropped — receiver already closed \
                                     (request may have timed out or been cancelled)"
                                );
                            }
                        }
                    }
                    Ok(())
                });
            }
            _ => {
                return Err(PyValueError::new_err(format!(
                    "Unsupported ASGI message type: {}",
                    msg_type
                )))
            }
        }

        pyo3_async_runtimes::tokio::future_into_py(py, async move { Ok(()) })
    }
}

/// Done-callback that closes both channels when the ASGI coroutine finishes,
/// ensuring the body stream always terminates.
#[pyclass]
struct AsgiDoneCallback {
    state: Arc<AsgiSendState>,
    response_done: Arc<Notify>,
}

#[pymethods]
impl AsgiDoneCallback {
    fn __call__(&self, _py: Python<'_>, future: &Bound<'_, PyAny>) {
        match future.call_method0("exception") {
            Err(_) => {
                log::debug!("ASGI mount: coroutine was cancelled");
            }
            Ok(exc) if !exc.is_none() => {
                let exc_type = exc
                    .get_type()
                    .name()
                    .ok()
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "Exception".to_string());
                let exc_str = exc
                    .call_method0("__str__")
                    .ok()
                    .and_then(|s| s.extract::<String>().ok())
                    .unwrap_or_default();
                let msg = format!("{}: {}", exc_type, exc_str);
                log::error!("ASGI mount: coroutine raised an exception: {}", msg);
                if let Ok(mut guard) = self.state.exception_msg.lock() {
                    *guard = Some(msg);
                }
            }
            Ok(_) => {}
        }

        // Drop channels so the handler observes end-of-stream / error.
        if let Ok(mut guard) = self.state.response_start_tx.lock() {
            guard.take();
        }
        if let Ok(mut guard) = self.state.body_tx.lock() {
            guard.take();
        }

        self.response_done.notify_waiters();
    }
}

fn parse_asgi_headers(message: &Bound<'_, PyDict>) -> PyResult<Vec<(Vec<u8>, Vec<u8>)>> {
    let mut headers = Vec::new();
    if let Some(headers_obj) = message.get_item("headers")? {
        for header in headers_obj.try_iter()? {
            let header = header?;
            let pair: Vec<PyBackedBytes> = header.extract()?;
            if pair.len() != 2 {
                return Err(PyValueError::new_err(
                    "ASGI headers must be [name, value] byte pairs",
                ));
            }
            headers.push((pair[0].as_ref().to_vec(), pair[1].as_ref().to_vec()));
        }
    }
    Ok(headers)
}

fn parse_asgi_body(message: &Bound<'_, PyDict>) -> PyResult<Vec<u8>> {
    let Some(body_obj) = message.get_item("body")? else {
        return Ok(Vec::new());
    };

    if body_obj.is_none() {
        return Ok(Vec::new());
    }

    if let Ok(body_bytes) = body_obj.cast::<PyBytes>() {
        return Ok(body_bytes.as_bytes().to_vec());
    }

    if let Ok(body_bytes) = body_obj.extract::<PyBackedBytes>() {
        return Ok(body_bytes.as_ref().to_vec());
    }

    body_obj.extract::<Vec<u8>>()
}

#[inline]
fn parse_host_port(host: &str, scheme: &str) -> (String, u16) {
    let default_port = if scheme.eq_ignore_ascii_case("https") {
        443
    } else {
        80
    };

    if let Some(bracket_end) = host.find(']') {
        if host.len() > bracket_end + 2 && host.as_bytes()[bracket_end + 1] == b':' {
            if let Ok(port) = host[bracket_end + 2..].parse::<u16>() {
                return (host[..bracket_end + 1].to_string(), port);
            }
        }
        return (host[..bracket_end + 1].to_string(), default_port);
    }

    if let Some((name, port)) = host.rsplit_once(':') {
        if let Ok(parsed_port) = port.parse::<u16>() {
            return (name.to_string(), parsed_port);
        }
    }

    (host.to_string(), default_port)
}

#[inline]
fn mounted_subpath(request_path: &str, mount_prefix: &str) -> String {
    if mount_prefix == "/" {
        return request_path.to_string();
    }

    if request_path == mount_prefix {
        return "/".to_string();
    }

    if request_path.starts_with(mount_prefix) {
        let path = &request_path[mount_prefix.len()..];
        if path.is_empty() {
            "/".to_string()
        } else {
            path.to_string()
        }
    } else {
        request_path.to_string()
    }
}

fn build_scope(py: Python<'_>, req: &HttpRequest, mount: &AsgiMount) -> PyResult<Py<PyDict>> {
    let scope = PyDict::new(py);
    let asgi_info = PyDict::new(py);
    asgi_info.set_item("version", "3.0")?;
    asgi_info.set_item("spec_version", "2.3")?;
    scope.set_item("asgi", asgi_info)?;

    scope.set_item("type", "http")?;
    scope.set_item(
        "http_version",
        match req.version() {
            Version::HTTP_09 => "0.9",
            Version::HTTP_10 => "1",
            Version::HTTP_11 => "1.1",
            Version::HTTP_2 => "2",
            Version::HTTP_3 => "3",
            _ => "1.1",
        },
    )?;
    scope.set_item("method", req.method().as_str())?;

    let conn_info = req.connection_info();
    scope.set_item("scheme", conn_info.scheme())?;

    let request_path = req.path();
    let subpath = mounted_subpath(request_path, &mount.prefix);
    scope.set_item("path", &subpath)?;
    scope.set_item("raw_path", PyBytes::new(py, subpath.as_bytes()))?;
    scope.set_item(
        "query_string",
        PyBytes::new(py, req.query_string().as_bytes()),
    )?;
    if mount.prefix == "/" {
        scope.set_item("root_path", "")?;
    } else {
        scope.set_item("root_path", &mount.prefix)?;
    }

    let headers = PyList::empty(py);
    for (name, value) in req.headers() {
        headers.append((
            PyBytes::new(py, name.as_str().as_bytes()),
            PyBytes::new(py, value.as_bytes()),
        ))?;
    }

    if !req.headers().contains_key("host") {
        headers.append((
            PyBytes::new(py, b"host"),
            PyBytes::new(py, conn_info.host().as_bytes()),
        ))?;
    }

    scope.set_item("headers", headers)?;

    let (server_host, server_port) = parse_host_port(conn_info.host(), conn_info.scheme());
    scope.set_item("server", (server_host, server_port))?;

    let client_host = conn_info
        .realip_remote_addr()
        .unwrap_or("127.0.0.1")
        .to_string();
    scope.set_item("client", (client_host, 0u16))?;

    scope.set_item("extensions", PyDict::new(py))?;
    Ok(scope.unbind())
}

/// Submit a coroutine to the Python asyncio event loop via `run_coroutine_threadsafe`.
/// Returns the `concurrent.futures.Future` wrapping the asyncio task.
fn submit_to_event_loop(py: Python<'_>, coroutine: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let locals = TASK_LOCALS
        .get()
        .ok_or_else(|| PyRuntimeError::new_err("Asyncio loop not initialized"))?;
    let event_loop = locals.event_loop(py);
    let asyncio = py.import("asyncio")?;
    let future =
        asyncio.call_method1("run_coroutine_threadsafe", (coroutine.bind(py), event_loop))?;
    Ok(future.unbind())
}

/// Handle a request by delegating to an HTTP ASGI mount.
///
/// Streams the response: body chunks are forwarded via mpsc as they arrive,
/// so SSE/chunked streaming works in real time. `asgi_mount_timeout` applies
/// only to the initial `http.response.start`; the body streams indefinitely.
pub async fn handle_asgi_mount_request(
    req: HttpRequest,
    mut payload: web::Payload,
    mount: &AsgiMount,
    debug: bool,
    max_payload_size: usize,
    asgi_mount_timeout: Duration,
) -> HttpResponse {
    // 1. Buffer request body.
    // NOTE: This read loop currently has no timeout. Slow-client request body timeouts
    // should be enforced at the edge proxy/load balancer for now.
    let mut request_body = Vec::new();
    while let Some(chunk) = payload.next().await {
        match chunk {
            Ok(data) => {
                if request_body.len().saturating_add(data.len()) > max_payload_size {
                    return HttpResponse::PayloadTooLarge()
                        .content_type("text/plain; charset=utf-8")
                        .body(format!(
                            "Request body too large for ASGI mount (limit: {} bytes)",
                            max_payload_size
                        ));
                }
                request_body.extend_from_slice(&data);
            }
            Err(err) => {
                return HttpResponse::BadRequest()
                    .content_type("text/plain; charset=utf-8")
                    .body(format!("Failed to read request body: {}", err));
            }
        }
    }

    // 2. Build scope/protocol objects, submit coroutine, register done-callback.
    //
    // run_coroutine_threadsafe and add_done_callback are non-blocking Python
    // calls, so we do everything in one inline GIL acquisition instead of
    // dispatching through spawn_blocking.
    let (body_tx, body_rx) = mpsc::channel::<Bytes>(ASGI_MOUNT_BODY_CHANNEL_CAPACITY);
    let (start_tx, start_rx) = oneshot::channel::<AsgiResponseStart>();
    let response_done = Arc::new(Notify::new());

    let send_state = Arc::new(AsgiSendState {
        response_start_tx: Mutex::new(Some(start_tx)),
        body_tx: Mutex::new(Some(body_tx)),
        exception_msg: Mutex::new(None),
    });

    let py_future: Py<PyAny> = match Python::attach(|py| -> PyResult<Py<PyAny>> {
        let scope = build_scope(py, &req, mount)?;
        let receive_obj = Py::new(
            py,
            AsgiReceive {
                body: Arc::new(AsyncMutex::new(Some(request_body))),
                response_done: response_done.clone(),
            },
        )?;
        let send_obj = Py::new(
            py,
            AsgiSend {
                state: send_state.clone(),
            },
        )?;
        let coroutine = mount.app.call1(py, (scope, receive_obj, send_obj))?;

        let py_future = submit_to_event_loop(py, coroutine)?;
        let callback = Py::new(
            py,
            AsgiDoneCallback {
                state: send_state.clone(),
                response_done: response_done.clone(),
            },
        )?;
        py_future
            .bind(py)
            .call_method1("add_done_callback", (callback,))?;
        Ok(py_future)
    }) {
        Ok(f) => f,
        Err(err) => {
            return Python::attach(|py| {
                handle_python_error(py, err, req.path(), req.method().as_str(), debug)
            });
        }
    };

    // 3. Wait for http.response.start (with timeout).
    let response_start = tokio::select! {
        result = start_rx => {
            match result {
                Ok(start) => start,
                Err(_) => {
                    let detail = match send_state.exception_msg.lock() {
                        Ok(guard) if guard.is_some() => {
                            format!(
                                "ASGI app raised an exception: {}",
                                guard.as_ref().unwrap()
                            )
                        }
                        _ => "ASGI app did not send http.response.start".to_string(),
                    };
                    return HttpResponse::InternalServerError()
                        .content_type("text/plain; charset=utf-8")
                        .body(detail);
                }
            }
        }
        _ = tokio::time::sleep(asgi_mount_timeout) => {
            Python::attach(|py| {
                let _ = py_future.call_method0(py, "cancel");
            });
            response_done.notify_waiters();
            return HttpResponse::GatewayTimeout()
                .content_type("text/plain; charset=utf-8")
                .body(format!(
                    "ASGI mount timed out after {:.3} seconds waiting for response headers",
                    asgi_mount_timeout.as_secs_f64()
                ));
        }
    };

    // 4. Build and stream the response.
    let status = StatusCode::from_u16(response_start.status).unwrap_or(StatusCode::OK);
    let mut builder = HttpResponse::build(status);

    let mut is_event_stream = false;
    for (name_bytes, value_bytes) in response_start.headers {
        match HeaderName::from_bytes(&name_bytes) {
            Ok(name) => match HeaderValue::from_bytes(&value_bytes) {
                Ok(value) => {
                    if name == "content-type" && value.as_bytes().starts_with(b"text/event-stream")
                    {
                        is_event_stream = true;
                    }
                    builder.append_header((name, value));
                }
                Err(_) => {
                    log::warn!(
                        "ASGI mount: dropping response header {:?} — value contains \
                         invalid bytes (embedded CR/LF or non-ASCII)",
                        String::from_utf8_lossy(&name_bytes)
                    );
                }
            },
            Err(_) => {
                log::warn!(
                    "ASGI mount: dropping response header with invalid name bytes: {:?}",
                    String::from_utf8_lossy(&name_bytes)
                );
            }
        }
    }

    // SSE: add anti-buffering headers (mirrors the native django-bolt SSE path).
    // Content-Encoding: identity tells our CompressionMiddleware to skip
    // compression (it strips the header before sending). Without this, the
    // compressor buffers chunks and the browser receives nothing until flush.
    if is_event_stream {
        builder.insert_header(("X-Accel-Buffering", "no"));
        builder.insert_header(("Cache-Control", "no-cache, no-store, must-revalidate"));
        builder.insert_header(("Pragma", "no-cache"));
        builder.insert_header(("Expires", "0"));
        builder.insert_header(("Content-Encoding", "identity"));
    }

    if req.method() == Method::HEAD {
        return builder.body(Vec::<u8>::new());
    }

    let body_stream = stream::unfold(body_rx, |mut rx| async move {
        rx.recv()
            .await
            .map(|chunk| (Ok::<Bytes, std::io::Error>(chunk), rx))
    });
    builder.streaming(body_stream)
}
