//! WebSocket upgrade handler with full Python integration

use actix::Addr;
use actix_web::http::header::{
    HeaderMap, HeaderName, HeaderValue, CONNECTION, SEC_WEBSOCKET_ACCEPT, SEC_WEBSOCKET_EXTENSIONS,
    SEC_WEBSOCKET_PROTOCOL, UPGRADE,
};
use actix_web::http::StatusCode;
use actix_web::{web, HttpRequest, HttpResponse};
use actix_web_actors::ws;
use ahash::AHashMap;
use futures_util::FutureExt;
use once_cell::sync::OnceCell;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use tokio::sync::{mpsc, oneshot};

use bolt_core::metadata::{CorsConfig, RouteMetadata};
use bolt_core::middleware::auth::{populate_auth_context, AuthContext};
use bolt_core::middleware::rate_limit::{check_after_auth, check_before_auth};
use bolt_core::request_pipeline::{
    query_sequences, set_declared_item, set_param_item, set_query_sequences, EMPTY_TYPES,
};
use bolt_core::router::parse_query_string;
use bolt_core::state::{AppState, ROUTE_METADATA};
use bolt_core::type_coercion::TypeHints;
use bolt_core::validation::{validate_auth_and_guards, AuthGuardResult};

use super::actor::WebSocketActor;
use super::config::WS_CONFIG;
use super::messages::{SendToClient, WsMessage};
use super::{ConnectionSlot, ACTIVE_WS_CONNECTIONS};

/// Cached Python imports - loaded once at first WebSocket connection
static WS_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();
static BUILD_REQUEST_FN: OnceCell<Py<PyAny>> = OnceCell::new();
static DISCONNECT_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();

/// Get cached WebSocket class (imports once, reuses)
fn get_ws_class(py: Python<'_>) -> PyResult<&Py<PyAny>> {
    WS_CLASS.get_or_try_init(|| {
        let ws_module = py.import("django_bolt.websocket")?;
        let ws_class = ws_module.getattr("WebSocket")?;
        Ok(ws_class.unbind())
    })
}

/// Get cached WebSocketDisconnect class (imports once, reuses)
fn get_disconnect_class(py: Python<'_>) -> PyResult<&Py<PyAny>> {
    DISCONNECT_CLASS.get_or_try_init(|| {
        let ws_module = py.import("django_bolt.websocket")?;
        Ok(ws_module.getattr("WebSocketDisconnect")?.unbind())
    })
}

/// Whether a handler error only reports that the client left.
fn is_client_gone(error: &PyErr) -> bool {
    Python::attach(|py| {
        error.is_instance_of::<pyo3::exceptions::PyConnectionResetError>(py)
            || get_disconnect_class(py).is_ok_and(|class| error.is_instance(py, class.bind(py)))
    })
}

/// The error of a send to a client that left. ASGI requires an `OSError`.
fn client_gone() -> PyErr {
    pyo3::exceptions::PyConnectionResetError::new_err("WebSocket client disconnected")
}

/// Get cached build_websocket_request function (imports once, reuses)
fn get_build_request_fn(py: Python<'_>) -> PyResult<&Py<PyAny>> {
    BUILD_REQUEST_FN.get_or_try_init(|| {
        let handlers_module = py.import("django_bolt.websocket.handlers")?;
        let build_request = handlers_module.getattr("build_websocket_request")?;
        Ok(build_request.unbind())
    })
}

/// Check if a request is a WebSocket upgrade request
/// OPTIMIZATION: Use case-insensitive comparison without allocation
#[inline]
pub fn is_websocket_upgrade(req: &HttpRequest) -> bool {
    // Check for Connection: upgrade header (can be comma-separated list)
    let has_upgrade_connection = req
        .headers()
        .get("connection")
        .and_then(|v| v.to_str().ok())
        .map(|v| {
            v.split(',')
                .any(|p| p.trim().eq_ignore_ascii_case("upgrade"))
        })
        .unwrap_or(false);

    if !has_upgrade_connection {
        return false;
    }

    req.headers()
        .get("upgrade")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false)
}

/// Subprotocols that the client requests in `Sec-WebSocket-Protocol`, in order.
///
/// The header can occur more than once. Each value is a comma-separated list.
pub fn requested_subprotocols<'a>(values: impl Iterator<Item = &'a str>) -> Vec<String> {
    values
        .flat_map(|value| value.split(','))
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Build scope dict for Python WebSocket handler
///
/// Parses and coerces query, path, header and cookie values to typed Python
/// objects using the same type coercion as HTTP handlers.
fn build_scope(
    py: Python<'_>,
    req: &HttpRequest,
    subprotocols: &[String],
    path_params: &AHashMap<String, String>,
    route_meta: Option<&RouteMetadata>,
    max_param_length: usize,
) -> PyResult<Py<PyAny>> {
    let empty_types: &TypeHints = &EMPTY_TYPES;
    let param_types = route_meta.map_or(empty_types, |m| &m.param_types);
    let header_types = route_meta.map_or(empty_types, |m| &m.header_types);
    let cookie_types = route_meta.map_or(empty_types, |m| &m.cookie_types);

    let scope_dict = PyDict::new(py);
    scope_dict.set_item("type", "websocket")?;
    scope_dict.set_item("path", req.path())?;

    // Parse the query string with the HTTP parser: the last value of a repeated key wins.
    // A value that is too long or a bad typed value rejects the upgrade, as in HTTP.
    let query_dict = PyDict::new(py);
    for (key, value) in &parse_query_string(req.query_string()) {
        set_param_item(
            py,
            &query_dict,
            key,
            value,
            param_types,
            max_param_length,
            "Query parameter",
        )?;
    }
    // A sequence parameter takes each value of its repeated key, as in HTTP.
    // Each value gets the length check, as the loop above checks only the last one.
    if let Some(route_meta) = route_meta {
        let sequences = query_sequences(
            Some(req.query_string()),
            &route_meta.query_seq_fields,
            max_param_length,
        )
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
        set_query_sequences(py, &query_dict, &sequences)?;
    }
    scope_dict.set_item("query_params", query_dict)?;

    // Keep raw query_string for compatibility
    scope_dict.set_item("query_string", req.query_string().as_bytes())?;

    // Add headers as dict (FastAPI style)
    // OPTIMIZATION: HeaderName::as_str() already returns lowercase (http crate canonical form)
    // A typed header with a bad value rejects the upgrade, as in HTTP.
    let headers_dict = PyDict::new(py);
    for (key, value) in req.headers().iter() {
        if let Ok(v) = value.to_str() {
            set_declared_item(
                py,
                &headers_dict,
                key.as_str(),
                v,
                header_types,
                max_param_length,
                "Header",
            )?;
        }
    }
    scope_dict.set_item("headers", headers_dict)?;

    // Coerce path params using type hints
    let params_dict = PyDict::new(py);
    for (k, v) in path_params.iter() {
        set_param_item(
            py,
            &params_dict,
            k,
            v,
            param_types,
            max_param_length,
            "Path parameter",
        )?;
    }
    scope_dict.set_item("path_params", params_dict)?;

    // Add cookies
    let cookies_dict = PyDict::new(py);
    if let Some(cookie_header) = req.headers().get("cookie") {
        if let Ok(cookie_str) = cookie_header.to_str() {
            for pair in cookie_str.split(';') {
                let pair = pair.trim();
                if let Some(eq_pos) = pair.find('=') {
                    let key = &pair[..eq_pos];
                    let value = &pair[eq_pos + 1..];
                    set_declared_item(
                        py,
                        &cookies_dict,
                        key,
                        value,
                        cookie_types,
                        max_param_length,
                        "Cookie",
                    )?;
                }
            }
        }
    }
    scope_dict.set_item("cookies", cookies_dict)?;
    scope_dict.set_item("subprotocols", PyList::new(py, subprotocols)?)?;

    // Add client info
    if let Some(peer) = req.peer_addr() {
        let client = PyTuple::new(py, &[peer.ip().to_string(), peer.port().to_string()])?;
        scope_dict.set_item("client", client)?;
    }

    Ok(scope_dict.into())
}

/// The handshake decision of the handler.
enum Handshake {
    /// Send the 101 response. Bolt signals `ready` when the actor runs.
    Accept {
        subprotocol: Option<String>,
        headers: Vec<(HeaderName, HeaderValue)>,
        ready: oneshot::Sender<()>,
    },
    /// Refuse the upgrade with this HTTP status.
    Refuse(StatusCode),
}

/// Build an ASGI WebSocket scope for a mounted application.
///
/// `build_scope` builds the Bolt-specific scope that the `WebSocket` wrapper
/// reads. This one follows the ASGI specification instead.
///
/// `path` keeps the mount prefix, and `root_path` reports it, as the
/// specification requires. `path` is percent-decoded. `raw_path` keeps the
/// request bytes. A router such as `channels.routing.URLRouter`
/// strips `root_path` itself. A scope with the prefix already removed makes
/// such a router strip it twice and match nothing.
///
/// Takes parts, not an `HttpRequest`, so the `TestClient` backend builds the
/// same scope as the server.
pub fn build_asgi_scope_from_parts<'a, I>(
    py: Python<'_>,
    mount_prefix: &str,
    request_path: &str,
    query_string: &[u8],
    headers: I,
    secure: bool,
    client: Option<(String, u16)>,
) -> PyResult<Py<PyAny>>
where
    I: IntoIterator<Item = (&'a str, &'a [u8])>,
{
    let scope = PyDict::new(py);

    let asgi_info = PyDict::new(py);
    asgi_info.set_item("version", "3.0")?;
    asgi_info.set_item("spec_version", "2.3")?;
    scope.set_item("asgi", asgi_info)?;

    scope.set_item("type", "websocket")?;
    scope.set_item("scheme", if secure { "wss" } else { "ws" })?;

    scope.set_item("raw_path", PyBytes::new(py, request_path.as_bytes()))?;
    let path =
        String::from_utf8_lossy(&urlencoding::decode_binary(request_path.as_bytes())).into_owned();
    scope.set_item("path", path)?;
    scope.set_item("query_string", PyBytes::new(py, query_string))?;
    if mount_prefix == "/" {
        scope.set_item("root_path", "")?;
    } else {
        scope.set_item("root_path", mount_prefix)?;
    }

    let header_list = PyList::empty(py);
    let mut protocol_values = Vec::new();
    for (name, value) in headers {
        header_list.append((PyBytes::new(py, name.as_bytes()), PyBytes::new(py, value)))?;
        if name.eq_ignore_ascii_case("sec-websocket-protocol") {
            protocol_values.push(String::from_utf8_lossy(value).into_owned());
        }
    }
    scope.set_item("headers", header_list)?;
    let subprotocols = requested_subprotocols(protocol_values.iter().map(String::as_str));
    scope.set_item("subprotocols", subprotocols)?;

    if let Some((host, port)) = client {
        scope.set_item("client", (host, port))?;
    }

    Ok(scope.into())
}

fn build_asgi_scope(py: Python<'_>, req: &HttpRequest, mount_prefix: &str) -> PyResult<Py<PyAny>> {
    let secure = req.connection_info().scheme() == "https";
    let headers = req
        .headers()
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_bytes()));

    build_asgi_scope_from_parts(
        py,
        mount_prefix,
        req.path(),
        req.query_string().as_bytes(),
        headers,
        secure,
        req.peer_addr()
            .map(|peer| (peer.ip().to_string(), peer.port())),
    )
}

/// Shared state for WebSocket connection - passed to Python receive/send functions
struct WsConnectionState {
    /// Channel to receive messages from Actix actor
    from_actor_rx: tokio::sync::Mutex<mpsc::Receiver<WsMessage>>,
    /// Actor address to send messages to client, set on accept
    actor_addr: OnceLock<Addr<WebSocketActor>>,
    /// Sender of the handshake decision, taken by the first decision
    handshake: Mutex<Option<oneshot::Sender<Handshake>>>,
    /// Subprotocols that the client requested
    subprotocols: Vec<String>,
    /// Whether a receive before accept got the `websocket.connect` event
    connect_sent: AtomicBool,
}

impl WsConnectionState {
    fn take_handshake(&self) -> Option<oneshot::Sender<Handshake>> {
        self.handshake
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
    }

    /// Refuse the upgrade if the handler did not decide the handshake.
    fn refuse(&self, status: StatusCode) {
        if let Some(handshake) = self.take_handshake() {
            let _ = handshake.send(Handshake::Refuse(status));
        }
    }

    fn handshake_pending(&self) -> bool {
        self.handshake
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_some()
    }

    /// The actor of an accepted connection.
    ///
    /// Before accept, a send is an error of the handler. After accept, no
    /// actor means that the client left during the handshake.
    fn actor(&self) -> PyResult<Addr<WebSocketActor>> {
        if let Some(addr) = self.actor_addr.get() {
            return Ok(addr.clone());
        }
        if self.handshake_pending() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "WebSocket is not accepted: call accept() before send",
            ));
        }
        Err(client_gone())
    }
}

/// Response headers that the handshake sets. `accept(headers=...)` cannot set them.
///
/// RFC 6455 4.2.2 allows one `Sec-WebSocket-Protocol` field. Bolt sets it from `subprotocol`.
const HANDSHAKE_HEADERS: [HeaderName; 5] = [
    SEC_WEBSOCKET_PROTOCOL,
    SEC_WEBSOCKET_ACCEPT,
    SEC_WEBSOCKET_EXTENSIONS,
    UPGRADE,
    CONNECTION,
];

/// Read the `headers` of `websocket.accept`: an iterable of `[name, value]` byte pairs.
fn accept_headers(message: &Bound<'_, PyDict>) -> PyResult<Vec<(HeaderName, HeaderValue)>> {
    let Some(items) = message.get_item("headers")? else {
        return Ok(Vec::new());
    };
    let mut headers = Vec::new();
    for item in items.try_iter()? {
        let item = item?;
        let name: Vec<u8> = item.get_item(0)?.extract()?;
        let value: Vec<u8> = item.get_item(1)?.extract()?;
        let name = HeaderName::from_bytes(&name).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid accept header name: {}", e))
        })?;
        if HANDSHAKE_HEADERS.contains(&name) {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "The handshake sets the '{}' header. Use accept(subprotocol=...) to select a subprotocol.",
                name
            )));
        }
        let value = HeaderValue::from_bytes(&value).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid accept header value: {}", e))
        })?;
        headers.push((name, value));
    }
    Ok(headers)
}

/// Create Python receive function that reads from channel
fn create_receive_fn(py: Python<'_>, state: Arc<WsConnectionState>) -> PyResult<Py<PyAny>> {
    #[pyclass]
    struct ReceiveFn {
        state: Arc<WsConnectionState>,
    }

    #[pymethods]
    impl ReceiveFn {
        fn __call__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let state = self.state.clone();
            // ASGI: the first receive before accept gives `websocket.connect`.
            let connect = state.actor_addr.get().is_none()
                && state.handshake_pending()
                && !state.connect_sent.swap(true, Ordering::Relaxed);
            let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                if connect {
                    return Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.connect")?;
                        Ok(dict.unbind())
                    });
                }
                let mut rx = state.from_actor_rx.lock().await;
                match rx.recv().await {
                    Some(WsMessage::Text(text)) => Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.receive")?;
                        dict.set_item("text", text)?;
                        Ok(dict.unbind())
                    }),
                    Some(WsMessage::Binary(data)) => Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.receive")?;
                        dict.set_item("bytes", pyo3::types::PyBytes::new(py, &data))?;
                        Ok(dict.unbind())
                    }),
                    Some(WsMessage::Disconnect { code }) => Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.disconnect")?;
                        dict.set_item("code", code)?;
                        Ok(dict.unbind())
                    }),
                    None => Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.disconnect")?;
                        dict.set_item("code", 1000)?;
                        Ok(dict.unbind())
                    }),
                    _ => Python::attach(|py| {
                        let dict = PyDict::new(py);
                        dict.set_item("type", "websocket.receive")?;
                        Ok(dict.unbind())
                    }),
                }
            })?;
            Ok(future.into())
        }
    }

    let receive_fn = ReceiveFn { state };
    Ok(Py::new(py, receive_fn)?.into_any().into())
}

/// Create Python send function that sends to actor
fn create_send_fn(py: Python<'_>, state: Arc<WsConnectionState>) -> PyResult<Py<PyAny>> {
    #[pyclass]
    struct SendFn {
        state: Arc<WsConnectionState>,
    }

    #[pymethods]
    impl SendFn {
        fn __call__(&self, py: Python<'_>, message: &Bound<'_, PyDict>) -> PyResult<Py<PyAny>> {
            let msg_type: String = message
                .get_item("type")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Missing 'type' key"))?
                .extract()?;

            let state = &self.state;

            match msg_type.as_str() {
                "websocket.accept" => {
                    let subprotocol: Option<String> = message
                        .get_item("subprotocol")?
                        .map(|v| v.extract::<Option<String>>())
                        .transpose()?
                        .flatten();
                    // RFC 6455: the server selects one of the subprotocols of the client.
                    if let Some(ref selected) = subprotocol {
                        if !state.subprotocols.contains(selected) {
                            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                                "Subprotocol '{}' was not requested by the client (requested: {:?})",
                                selected, state.subprotocols
                            )));
                        }
                    }
                    let headers = accept_headers(message)?;
                    let handshake = state.take_handshake().ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err(
                            "WebSocket handshake is already complete",
                        )
                    })?;
                    let (ready_tx, ready_rx) = oneshot::channel();
                    let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                        // A failed send or a dropped `ready` means the client left during
                        // the handshake. The next receive() then gives a disconnect.
                        if handshake
                            .send(Handshake::Accept {
                                subprotocol,
                                headers,
                                ready: ready_tx,
                            })
                            .is_ok()
                        {
                            let _ = ready_rx.await;
                        }
                        Ok(Python::attach(|py| {
                            py.None().into_pyobject(py).unwrap().unbind()
                        }))
                    })?;
                    Ok(future.into())
                }
                "websocket.send" => {
                    if let Some(text) = message.get_item("text")? {
                        let text: String = text.extract()?;
                        let actor = state.actor()?;
                        let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                            actor
                                .send(SendToClient(WsMessage::SendText(text)))
                                .await
                                .map_err(|_| client_gone())?;
                            Ok(Python::attach(|py| {
                                py.None().into_pyobject(py).unwrap().unbind()
                            }))
                        })?;
                        Ok(future.into())
                    } else if let Some(bytes) = message.get_item("bytes")? {
                        let data: Vec<u8> = bytes.extract()?;
                        let actor = state.actor()?;
                        let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                            actor
                                .send(SendToClient(WsMessage::SendBinary(data)))
                                .await
                                .map_err(|_| client_gone())?;
                            Ok(Python::attach(|py| {
                                py.None().into_pyobject(py).unwrap().unbind()
                            }))
                        })?;
                        Ok(future.into())
                    } else {
                        Err(pyo3::exceptions::PyValueError::new_err(
                            "websocket.send requires 'text' or 'bytes'",
                        ))
                    }
                }
                "websocket.close" => {
                    let code: u16 = message
                        .get_item("code")?
                        .map(|v| v.extract())
                        .transpose()?
                        .unwrap_or(1000);
                    let reason: String = message
                        .get_item("reason")?
                        .map(|v| v.extract())
                        .transpose()?
                        .unwrap_or_default();
                    let actor = state.actor_addr.get().cloned();
                    if actor.is_none() {
                        // Close before accept refuses the upgrade with 403, as in uvicorn.
                        state.refuse(StatusCode::FORBIDDEN);
                    }
                    let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                        if let Some(actor) = actor {
                            actor
                                .send(SendToClient(WsMessage::Close { code, reason }))
                                .await
                                .map_err(|_| client_gone())?;
                        }
                        Ok(Python::attach(|py| {
                            py.None().into_pyobject(py).unwrap().unbind()
                        }))
                    })?;
                    Ok(future.into())
                }
                _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Unknown message type: {}",
                    msg_type
                ))),
            }
        }
    }

    let send_fn = SendFn { state };
    Ok(Py::new(py, send_fn)?.into_any().into())
}

/// Validate WebSocket origin header against CORS allowed origins
/// Uses the same CORS configuration as HTTP requests (like FastAPI)
///
/// Security behavior:
/// - If CORS is configured with allow_all_origins=true: allow all origins
/// - If CORS is configured with specific origins: only allow those origins
/// - If NO CORS is configured: DENY all cross-origin requests (fail-secure)
/// - Same-origin requests (no Origin header) are always allowed
fn validate_origin(req: &HttpRequest, state: &AppState) -> bool {
    // Get origin header from request
    let origin = match req.headers().get("origin") {
        Some(v) => match v.to_str() {
            Ok(s) => s,
            Err(_) => {
                eprintln!("[django-bolt] WebSocket: Invalid Origin header encoding");
                return false;
            }
        },
        None => {
            // No origin header - allow for same-origin requests
            // (browsers don't send Origin for same-origin WebSocket connections)
            return true;
        }
    };

    // Check global CORS config (same as HTTP)
    if let Some(ref cors_config) = state.global_cors_config {
        return is_origin_allowed(origin, cors_config, &state.cors_origin_regexes);
    }

    // SECURITY: No CORS configured = deny all cross-origin requests
    // This is a fail-secure default (unlike the old allow-all default)
    eprintln!(
        "[django-bolt] WebSocket: Rejecting cross-origin request from '{}' - no CORS configured. \
        Set CORS_ALLOWED_ORIGINS in Django settings to allow WebSocket connections.",
        origin
    );
    false
}

/// Check if an origin is allowed by the CORS configuration
/// Reuses the same logic as HTTP CORS validation
fn is_origin_allowed(
    origin: &str,
    cors_config: &CorsConfig,
    global_regexes: &[regex::Regex],
) -> bool {
    // Allow all origins if configured
    if cors_config.allow_all_origins {
        return true;
    }

    // O(1) exact match using HashSet
    if cors_config.origin_set.contains(origin) {
        return true;
    }

    // Check route-level regex patterns
    if cors_config
        .compiled_origin_regexes
        .iter()
        .any(|re| re.is_match(origin))
    {
        return true;
    }

    // Check global regex patterns
    if global_regexes.iter().any(|re| re.is_match(origin)) {
        return true;
    }

    false
}

/// What a WebSocket upgrade dispatches to.
pub enum WsTarget {
    /// A route registered with `@api.websocket`. Runs auth, guards, and rate
    /// limiting, then calls the handler with a `WebSocket` object.
    Route {
        handler: Py<PyAny>,
        handler_id: usize,
        path_params: AHashMap<String, String>,
        injector: Option<Py<PyAny>>,
    },
    /// An ASGI application registered with `api.mount_asgi`. Receives the
    /// scope, receive, and send triple directly.
    AsgiMount {
        app: Py<PyAny>,
        mount_prefix: String,
    },
}

/// Await the revocation check of a WebSocket route on this thread's WorkerLoop.
async fn token_revoked(check: &Py<PyAny>, auth_ctx: &AuthContext) -> PyResult<bool> {
    let future = Python::attach(|py| -> PyResult<_> {
        let context = PyDict::new(py).unbind();
        populate_auth_context(&context, auth_ctx, py);
        let coro = check.call1(py, (context,))?;
        let locals = bolt_loop::worker_task_locals(py)?;
        pyo3_async_runtimes::into_future_with_locals(&locals, coro.bind(py).clone())
    })?;
    let revoked = future.await?;
    Python::attach(|py| revoked.extract::<bool>(py))
}

/// HTTP handler for WebSocket upgrade with full Python integration
///
/// Handles:
/// - Connection limit checking
/// - Rate limiting (reuses HTTP rate limit infrastructure)
/// - Origin validation (CORS-like protection)
/// - Authentication and guards
/// - WebSocket upgrade and actor setup
///
/// Routes run the full middleware chain. ASGI mounts skip the steps that need
/// route metadata, exactly as HTTP mounts do.
pub async fn handle_websocket_upgrade(
    req: HttpRequest,
    stream: web::Payload,
    target: WsTarget,
    state: Arc<AppState>,
) -> actix_web::Result<HttpResponse> {
    let route_handler_id = match &target {
        WsTarget::Route { handler_id, .. } => Some(*handler_id),
        WsTarget::AsgiMount { .. } => None,
    };
    // Use cached config - no Python/GIL access
    let config = &*WS_CONFIG;

    // Validate request is actually a WebSocket upgrade
    if !is_websocket_upgrade(&req) {
        return Ok(HttpResponse::BadRequest().body("Expected WebSocket upgrade request"));
    }
    // Check the key and version before the handler runs, as the 101 comes later.
    if let Err(e) = ws::handshake(&req) {
        return Err(e.into());
    }

    // Refuse new connections while draining for shutdown/recycle; clients
    // that retry will land on a healthy worker via SO_REUSEPORT.
    // Best-effort: the acceptor is usually torn down (handle.stop) right after
    // draining begins, so most new upgrades are refused at the TCP layer before
    // reaching here. This mainly covers an upgrade racing in on an already-open
    // keep-alive connection during the drain window.
    if super::is_draining() {
        return Ok(HttpResponse::ServiceUnavailable()
            .content_type("application/json")
            .body(r#"{"detail":"Server is shutting down"}"#));
    }

    // Check connection limit FIRST (before any processing)
    let current_connections = ACTIVE_WS_CONNECTIONS.load(Ordering::Relaxed);
    if current_connections >= config.max_connections {
        eprintln!(
            "[django-bolt] WebSocket: Connection limit reached ({}/{})",
            current_connections, config.max_connections
        );
        return Ok(HttpResponse::ServiceUnavailable()
            .content_type("application/json")
            .body(r#"{"detail":"Too many WebSocket connections"}"#));
    }

    // Extract headers for rate limiting and auth
    // OPTIMIZATION: HeaderName::as_str() already returns lowercase (http crate canonical form)
    let mut headers: AHashMap<String, String> = AHashMap::new();
    for (key, value) in req.headers().iter() {
        if let Ok(v) = value.to_str() {
            headers.insert(key.as_str().to_owned(), v.to_owned());
        }
    }

    // Check rate limiting BEFORE origin validation (reuse HTTP rate limit).
    // Address and header keys run here; identity keys run after auth below.
    let mut client_ip = None;
    if let Some(handler_id) = route_handler_id {
        if let Some(route_meta) = ROUTE_METADATA.get().and_then(|m| m.get(handler_id)) {
            if let Some(ref rate_config) = route_meta.rate_limit_config {
                if !matches!(
                    rate_config.key,
                    bolt_core::metadata::RateLimitKey::Header(_)
                ) {
                    client_ip = bolt_core::middleware::client_ip::resolve_from_headers(
                        req.headers(),
                        req.peer_addr().map(|address| address.ip()),
                        &state.trusted_proxies,
                    );
                }
                if let Some(response) = check_before_auth(
                    handler_id,
                    &headers,
                    client_ip.as_ref(),
                    rate_config,
                    req.method().as_str(),
                    req.path(),
                ) {
                    return Ok(response);
                }
            }
        }
    }

    // Validate origin header (CORS-like protection for WebSocket)
    // Uses same CORS config as HTTP requests
    if !validate_origin(&req, &state) {
        return Ok(HttpResponse::Forbidden()
            .content_type("application/json")
            .body(r#"{"detail":"Origin not allowed"}"#));
    }

    // Evaluate authentication and guards before upgrading
    if let Some(handler_id) = route_handler_id {
        if let Some(route_meta) = ROUTE_METADATA.get().and_then(|m| m.get(handler_id)) {
            match validate_auth_and_guards(&headers, &route_meta.auth_backends, &route_meta.guards)
            {
                AuthGuardResult::Allow(ctx) => {
                    if let Some(ref rate_config) = route_meta.rate_limit_config {
                        if let Some(response) = check_after_auth(
                            handler_id,
                            &headers,
                            client_ip.as_ref(),
                            ctx.as_ref(),
                            rate_config,
                            req.method().as_str(),
                            req.path(),
                        ) {
                            return Ok(response);
                        }
                    }
                    // A revoked token fails the handshake, as on an HTTP route.
                    if let (Some(check), Some(auth_ctx)) =
                        (route_meta.websocket_revocation_check.as_ref(), ctx.as_ref())
                    {
                        match token_revoked(check, auth_ctx).await {
                            Ok(false) => {}
                            Ok(true) => return Ok(bolt_core::responses::error_401()),
                            Err(e) => {
                                eprintln!("[django-bolt] WebSocket revocation check error: {}", e);
                                return Ok(HttpResponse::InternalServerError()
                                    .content_type("application/json")
                                    .body(r#"{"detail":"Revocation check failed"}"#));
                            }
                        }
                    }
                }
                // Registration refuses a session backend on a WebSocket route. Fail closed.
                AuthGuardResult::Deferred(_) | AuthGuardResult::Unauthorized => {
                    return Ok(bolt_core::responses::error_401());
                }
                AuthGuardResult::Forbidden(denial) => {
                    return Ok(bolt_core::responses::error_403_denial(denial.as_deref()));
                }
            }
        }
    }

    // The slot counts this connection until the handshake fails or the actor stops.
    let slot = ConnectionSlot::acquire();

    // Create channels for bidirectional communication (configurable size)
    let (to_python_tx, to_python_rx) = mpsc::channel::<WsMessage>(config.channel_buffer_size);

    let subprotocols = requested_subprotocols(
        req.headers()
            .get_all(SEC_WEBSOCKET_PROTOCOL)
            .filter_map(|value| value.to_str().ok()),
    );

    // Build scope for Python. A bad value rejects the upgrade.
    let scope = match Python::attach(|py| match &target {
        WsTarget::Route {
            handler_id,
            path_params,
            ..
        } => {
            // Route metadata holds the type hints and the sequence query keys
            let route_meta = ROUTE_METADATA.get().and_then(|m| m.get(*handler_id));
            build_scope(
                py,
                &req,
                &subprotocols,
                path_params,
                route_meta,
                state.max_param_length,
            )
        }
        WsTarget::AsgiMount { mount_prefix, .. } => build_asgi_scope(py, &req, mount_prefix),
    }) {
        Ok(s) => s,
        Err(e) => {
            return Err(actix_web::error::ErrorBadRequest(format!(
                "Invalid request: {}",
                e
            )));
        }
    };

    // The handler decides the handshake: accept sends the 101, close refuses it.
    let (handshake_tx, handshake_rx) = oneshot::channel::<Handshake>();
    let ws_state = Arc::new(WsConnectionState {
        from_actor_rx: tokio::sync::Mutex::new(to_python_rx),
        actor_addr: OnceLock::new(),
        handshake: Mutex::new(Some(handshake_tx)),
        subprotocols,
        connect_sent: AtomicBool::new(false),
    });

    spawn_handler(ws_state.clone(), scope, target);

    match handshake_rx.await {
        Ok(Handshake::Accept {
            subprotocol,
            headers,
            ready,
        }) => {
            let actor = WebSocketActor::new(to_python_tx, slot);
            // Set the header here: actix reads only the first header line of the client.
            let protocol = subprotocol
                .map(|selected| {
                    HeaderValue::from_str(&selected).map_err(|e| {
                        actix_web::error::ErrorInternalServerError(format!(
                            "WebSocket subprotocol: {}",
                            e
                        ))
                    })
                })
                .transpose()?;
            let (addr, mut resp) = ws::WsResponseBuilder::new(actor, &req, stream)
                .frame_size(config.max_message_size)
                .start_with_addr()
                .map_err(|e| {
                    actix_web::error::ErrorInternalServerError(format!("WebSocket error: {}", e))
                })?;
            if let Some(protocol) = protocol {
                resp.headers_mut().insert(SEC_WEBSOCKET_PROTOCOL, protocol);
            }
            append_headers(resp.headers_mut(), headers);
            let _ = ws_state.actor_addr.set(addr);
            let _ = ready.send(());
            Ok(resp)
        }
        Ok(Handshake::Refuse(status)) => Ok(refusal(status)),
        // The handler task ends with a decision, so a dropped sender is a bug.
        Err(_) => Ok(refusal(StatusCode::INTERNAL_SERVER_ERROR)),
    }
}

/// Add the `accept` headers of the handler to the 101 response.
fn append_headers(target: &mut HeaderMap, headers: Vec<(HeaderName, HeaderValue)>) {
    for (name, value) in headers {
        target.append(name, value);
    }
}

/// Build a refused handshake: a plain HTTP response with no upgrade headers.
fn refusal(status: StatusCode) -> HttpResponse {
    let detail = if status == StatusCode::FORBIDDEN {
        r#"{"detail":"WebSocket connection refused"}"#
    } else {
        r#"{"detail":"WebSocket handler failed"}"#
    };
    HttpResponse::build(status)
        .content_type("application/json")
        .body(detail)
}

/// Run the Python handler on this thread's WorkerLoop.
///
/// The handler decides the handshake. If it ends without a decision, the
/// upgrade gets 403 on return and 500 on error, as in uvicorn.
fn spawn_handler(ws_state: Arc<WsConnectionState>, scope: Py<PyAny>, target: WsTarget) {
    actix_web::rt::spawn(async move {
        // Wrap the entire handler execution in catch_unwind to handle panics
        let result = std::panic::AssertUnwindSafe(async {
            // Create WebSocket instance and get the coroutine
            let future_result = Python::attach(|py| -> PyResult<_> {
                // Create receive and send functions
                let receive_fn = create_receive_fn(py, ws_state.clone())?;
                let send_fn = create_send_fn(py, ws_state.clone())?;

                let coro = match &target {
                    // An ASGI app takes the raw triple, with no wrapper.
                    WsTarget::AsgiMount { app, .. } => {
                        app.call1(py, (scope, receive_fn, send_fn))?
                    }
                    WsTarget::Route {
                        handler, injector, ..
                    } => {
                        // Use cached WebSocket class (imports once, reuses)
                        let ws_class = get_ws_class(py)?;

                        // Create WebSocket instance
                        let websocket =
                            ws_class.call1(py, (scope.clone_ref(py), receive_fn, send_fn))?;

                        // Use cached build_websocket_request function (imports once, reuses)
                        let build_request = get_build_request_fn(py)?;
                        let request = build_request.call1(py, (scope,))?;

                        // Call handler with proper parameter injection
                        // Injector is pre-compiled at route registration and passed from router
                        if let Some(ref inj) = injector {
                            // Use pre-compiled injector to extract parameters
                            // Sync injector - call directly
                            // Note: WebSocket handlers don't support async injectors (dependencies)
                            // since the injector is called synchronously during connection setup
                            let result = inj.call1(py, (request,))?;
                            let (args, kwargs): (Py<PyAny>, Py<PyAny>) = result.extract(py)?;

                            // Prepend websocket to args (accept any iterable: tuple or list)
                            let new_args =
                                pyo3::types::PyList::new(py, std::iter::once(&websocket))?;
                            for item in args.bind(py).try_iter()? {
                                new_args.append(item?)?;
                            }
                            let args_tuple = PyTuple::new(py, new_args.iter())?;

                            // Call handler with websocket + extracted args
                            let kwargs_dict = kwargs.bind(py).cast::<PyDict>()?;
                            handler.call(py, args_tuple, Some(&kwargs_dict))?
                        } else {
                            // No injector (simple handler) - just pass websocket
                            handler.call1(py, (&websocket,))?
                        }
                    }
                };

                // Run the handler on this thread's WorkerLoop — the loop that
                // runs async HTTP dispatch here — so futures/queues/locks
                // shared between a WebSocket handler and an HTTP handler
                // stay same-loop. (Cross-loop, `set_result` queues a wakeup
                // that never rouses the foreign selector and the WebSocket
                // side hangs.) The receive/send futures created by
                // `future_into_py` follow the running loop, so they migrate
                // with the handler.
                let locals = bolt_loop::worker_task_locals(py)?;

                pyo3_async_runtimes::into_future_with_locals(&locals, coro.bind(py).clone())
            });

            let error = match future_result {
                Ok(future) => future.await.err(),
                Err(e) => Some(e),
            };
            match error {
                None => ws_state.refuse(StatusCode::FORBIDDEN),
                Some(e) => {
                    // A client that left is not an error of the handler.
                    if !is_client_gone(&e) {
                        eprintln!("[django-bolt] WebSocket handler error: {}", e);
                    }
                    ws_state.refuse(StatusCode::INTERNAL_SERVER_ERROR);
                    close_with_error(&ws_state, "Internal error").await;
                }
            }
        })
        .catch_unwind()
        .await;

        if result.is_err() {
            eprintln!("[django-bolt] WebSocket handler task panicked - closing connection");
            ws_state.refuse(StatusCode::INTERNAL_SERVER_ERROR);
            close_with_error(&ws_state, "Internal server error").await;
        }
    });
}

/// Close an accepted connection with 1011. The actor stop releases the slot.
async fn close_with_error(ws_state: &WsConnectionState, reason: &str) {
    if let Some(addr) = ws_state.actor_addr.get() {
        let _ = addr
            .send(SendToClient(WsMessage::Close {
                code: 1011,
                reason: reason.to_string(),
            }))
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::requested_subprotocols;

    #[test]
    fn requested_subprotocols_splits_trims_and_keeps_order() {
        let values = ["graphql-transport-ws , graphql-ws", " ,chat.v1,"];
        assert_eq!(
            requested_subprotocols(values.into_iter()),
            vec!["graphql-transport-ws", "graphql-ws", "chat.v1"]
        );
    }

    #[test]
    fn requested_subprotocols_is_empty_without_header() {
        assert!(requested_subprotocols(std::iter::empty()).is_empty());
    }
}
