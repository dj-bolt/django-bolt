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
use tokio::sync::{mpsc, Mutex as AsyncMutex, Notify};

use crate::handler::handle_python_error;
use crate::state::{AsgiMount, TASK_LOCALS};

struct AsgiResponseStart {
    status: u16,
    headers: Vec<(Vec<u8>, Vec<u8>)>,
}

struct AsgiSendState {
    response_start: Mutex<Option<AsgiResponseStart>>,
    body_tx: Mutex<Option<mpsc::UnboundedSender<Bytes>>>,
}

enum AwaitCoroutineError {
    TimedOut,
    Python(PyErr),
}

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
                    .response_start
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("Failed to lock response_start"))?;
                if start_guard.is_some() {
                    return Err(PyRuntimeError::new_err(
                        "http.response.start sent more than once",
                    ));
                }
                *start_guard = Some(AsgiResponseStart { status, headers });
            }
            "http.response.body" => {
                let has_start = self
                    .state
                    .response_start
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("Failed to lock response_start"))?
                    .is_some();
                if !has_start {
                    return Err(PyRuntimeError::new_err(
                        "http.response.body received before http.response.start",
                    ));
                }

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

                if let Some(ref tx) = *tx_guard {
                    if !body.is_empty() {
                        let _ = tx.send(Bytes::from(body));
                    }

                    if !more_body {
                        tx_guard.take();
                    }
                } else if more_body || !body.is_empty() {
                    return Err(PyRuntimeError::new_err(
                        "Response body channel is already closed",
                    ));
                }
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

async fn await_coroutine_on_task_loop(
    coroutine: Py<PyAny>,
    timeout: Duration,
) -> Result<Py<PyAny>, AwaitCoroutineError> {
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| -> Result<Py<PyAny>, AwaitCoroutineError> {
            let locals = TASK_LOCALS.get().ok_or_else(|| {
                AwaitCoroutineError::Python(PyRuntimeError::new_err("Asyncio loop not initialized"))
            })?;
            let event_loop = locals.event_loop(py);

            let asyncio = py.import("asyncio").map_err(AwaitCoroutineError::Python)?;
            let future = asyncio
                .call_method1("run_coroutine_threadsafe", (coroutine.bind(py), event_loop))
                .map_err(AwaitCoroutineError::Python)?;

            match future.call_method1("result", (timeout.as_secs_f64(),)) {
                Ok(result) => Ok(result.unbind()),
                Err(err) => {
                    let is_timeout = err.is_instance_of::<pyo3::exceptions::PyTimeoutError>(py)
                        || (|| -> PyResult<bool> {
                            let timeout_cls =
                                py.import("concurrent.futures")?.getattr("TimeoutError")?;
                            err.value(py).is_instance(&timeout_cls)
                        })()
                        .unwrap_or(false);

                    if is_timeout {
                        let _ = future.call_method0("cancel");
                        return Err(AwaitCoroutineError::TimedOut);
                    }

                    Err(AwaitCoroutineError::Python(err))
                }
            }
        })
    })
    .await
    .map_err(|err| {
        AwaitCoroutineError::Python(PyRuntimeError::new_err(format!(
            "ASGI coroutine join error: {}",
            err
        )))
    })?
}

/// Handle a request by delegating to an HTTP ASGI mount.
///
/// This is used only on Bolt router misses, so API hot-path routing remains unchanged.
/// Current bridge behavior buffers ASGI body chunks until the app coroutine completes.
pub async fn handle_asgi_mount_request(
    req: HttpRequest,
    mut payload: web::Payload,
    mount: &AsgiMount,
    debug: bool,
    max_payload_size: usize,
    asgi_mount_timeout: Duration,
) -> HttpResponse {
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

    let (body_tx, body_rx) = mpsc::unbounded_channel::<Bytes>();
    let send_state = Arc::new(AsgiSendState {
        response_start: Mutex::new(None),
        body_tx: Mutex::new(Some(body_tx)),
    });
    let response_done = Arc::new(Notify::new());
    let receive = Arc::new(AsyncMutex::new(Some(request_body)));

    let app_coroutine = match Python::attach(|py| -> PyResult<_> {
        let scope = build_scope(py, &req, mount)?;
        let receive_obj = Py::new(
            py,
            AsgiReceive {
                body: receive.clone(),
                response_done: response_done.clone(),
            },
        )?;
        let send_obj = Py::new(
            py,
            AsgiSend {
                state: send_state.clone(),
            },
        )?;

        let app_callable = mount.app.clone_ref(py);
        let coroutine = app_callable.call1(py, (scope, receive_obj, send_obj))?;
        Ok(coroutine)
    }) {
        Ok(coroutine) => coroutine,
        Err(err) => {
            return Python::attach(|py| {
                handle_python_error(py, err, req.path(), req.method().as_str(), debug)
            })
        }
    };

    let app_result = await_coroutine_on_task_loop(app_coroutine, asgi_mount_timeout).await;
    response_done.notify_waiters();
    let _ = send_state
        .body_tx
        .lock()
        .map(|mut tx_guard| tx_guard.take())
        .ok();

    if let Err(err) = app_result {
        let response_started = send_state
            .response_start
            .lock()
            .map(|guard| guard.is_some())
            .unwrap_or(false);

        if !response_started {
            return match err {
                AwaitCoroutineError::TimedOut => HttpResponse::GatewayTimeout()
                    .content_type("text/plain; charset=utf-8")
                    .body(format!(
                        "ASGI mount timed out after {:.3} seconds",
                        asgi_mount_timeout.as_secs_f64()
                    )),
                AwaitCoroutineError::Python(err) => Python::attach(|py| {
                    handle_python_error(py, err, req.path(), req.method().as_str(), debug)
                }),
            };
        }

        if let AwaitCoroutineError::Python(err) = err {
            Python::attach(|py| {
                err.print(py);
            });
        }
    }

    let response_start = match send_state
        .response_start
        .lock()
        .ok()
        .and_then(|mut guard| guard.take())
    {
        Some(start) => start,
        None => {
            return HttpResponse::InternalServerError()
                .content_type("text/plain; charset=utf-8")
                .body("ASGI app did not send http.response.start")
        }
    };

    let status = StatusCode::from_u16(response_start.status).unwrap_or(StatusCode::OK);
    let mut builder = HttpResponse::build(status);

    for (name_bytes, value_bytes) in response_start.headers {
        if let Ok(name) = HeaderName::from_bytes(&name_bytes) {
            if let Ok(value) = HeaderValue::from_bytes(&value_bytes) {
                builder.append_header((name, value));
            }
        }
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
