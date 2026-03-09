//! WebSocket upgrade handler with full Python integration

use actix::Addr;
use actix_web::{web, HttpRequest, HttpResponse};
use actix_web_actors::ws;
use ahash::AHashMap;
use futures_util::FutureExt;
use once_cell::sync::OnceCell;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::sync::mpsc;

use std::collections::HashMap;

use crate::handler::coerced_value_to_py;
use crate::metadata::CorsConfig;
use crate::middleware::rate_limit::check_rate_limit;
use crate::state::{AppState, ROUTE_METADATA, TASK_LOCALS};
use crate::type_coercion::{coerce_param, TYPE_STRING};
use crate::validation::{validate_auth_and_guards, AuthGuardResult};

use super::actor::WebSocketActor;
use super::config::WS_CONFIG;
use super::messages::{SendToClient, WsMessage};
use super::ACTIVE_WS_CONNECTIONS;

/// Cached Python imports - loaded once at first WebSocket connection
static WS_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();
static BUILD_REQUEST_FN: OnceCell<Py<PyAny>> = OnceCell::new();

/// Get cached WebSocket class (imports once, reuses)
fn get_ws_class(py: Python<'_>) -> PyResult<&Py<PyAny>> {
    WS_CLASS.get_or_try_init(|| {
        let ws_module = py.import("django_bolt.websocket")?;
        let ws_class = ws_module.getattr("WebSocket")?;
        Ok(ws_class.unbind())
    })
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

/// Build scope dict for Python WebSocket handler
///
/// Parses and coerces query and path parameters to typed Python objects
/// using the same type coercion as HTTP handlers.
fn build_scope(
    py: Python<'_>,
    req: &HttpRequest,
    path_params: &AHashMap<String, String>,
    param_types: &HashMap<String, u8>,
) -> PyResult<Py<PyAny>> {
    let scope_dict = PyDict::new(py);
    scope_dict.set_item("type", "websocket")?;
    scope_dict.set_item("path", req.path())?;

    // Parse and coerce query parameters
    let query_dict = PyDict::new(py);
    let query_string = req.query_string();
    if !query_string.is_empty() {
        for pair in query_string.split('&') {
            if let Some((key, value)) = pair.split_once('=') {
                let decoded_key = urlencoding::decode(key).unwrap_or_default();
                let decoded_value = urlencoding::decode(value).unwrap_or_default();

                // Get type hint and coerce
                let type_hint = param_types
                    .get(decoded_key.as_ref())
                    .copied()
                    .unwrap_or(TYPE_STRING);

                match coerce_param(&decoded_value, type_hint) {
                    Ok(coerced) => {
                        let py_value = coerced_value_to_py(py, &coerced);
                        query_dict.set_item(decoded_key.as_ref(), py_value)?;
                    }
                    Err(_) => {
                        // Fall back to string on coercion error
                        query_dict.set_item(decoded_key.as_ref(), decoded_value.as_ref())?;
                    }
                }
            }
        }
    }
    scope_dict.set_item("query_params", query_dict)?;

    // Keep raw query_string for compatibility
    scope_dict.set_item("query_string", req.query_string().as_bytes())?;

    // Add headers as dict (FastAPI style)
    // OPTIMIZATION: HeaderName::as_str() already returns lowercase (http crate canonical form)
    let headers_dict = PyDict::new(py);
    for (key, value) in req.headers().iter() {
        if let Ok(v) = value.to_str() {
            headers_dict.set_item(key.as_str(), v)?;
        }
    }
    scope_dict.set_item("headers", headers_dict)?;

    // Coerce path params using type hints
    let params_dict = PyDict::new(py);
    for (k, v) in path_params.iter() {
        let type_hint = param_types.get(k).copied().unwrap_or(TYPE_STRING);
        match coerce_param(v, type_hint) {
            Ok(coerced) => {
                let py_value = coerced_value_to_py(py, &coerced);
                params_dict.set_item(k.as_str(), py_value)?;
            }
            Err(_) => {
                // Fall back to string on coercion error
                params_dict.set_item(k.as_str(), v.as_str())?;
            }
        }
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
                    cookies_dict.set_item(key, value)?;
                }
            }
        }
    }
    scope_dict.set_item("cookies", cookies_dict)?;

    // Add client info
    if let Some(peer) = req.peer_addr() {
        let client = PyTuple::new(py, &[peer.ip().to_string(), peer.port().to_string()])?;
        scope_dict.set_item("client", client)?;
    }

    Ok(scope_dict.into())
}

/// Shared state for WebSocket connection - passed to Python receive/send functions
struct WsConnectionState {
    /// Channel to receive messages from Actix actor
    from_actor_rx: tokio::sync::Mutex<mpsc::Receiver<WsMessage>>,
    /// Actor address to send messages to client
    actor_addr: Addr<WebSocketActor>,
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
            let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
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

            let state = self.state.clone();

            match msg_type.as_str() {
                "websocket.accept" => {
                    let subprotocol: Option<String> = message
                        .get_item("subprotocol")?
                        .map(|v| v.extract())
                        .transpose()?;
                    let state = state.clone();
                    let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                        state
                            .actor_addr
                            .send(SendToClient(WsMessage::Accept { subprotocol }))
                            .await
                            .map_err(|e| {
                                pyo3::exceptions::PyRuntimeError::new_err(format!(
                                    "Failed to send accept: {}",
                                    e
                                ))
                            })?;
                        Ok(Python::attach(|py| {
                            py.None().into_pyobject(py).unwrap().unbind()
                        }))
                    })?;
                    Ok(future.into())
                }
                "websocket.send" => {
                    if let Some(text) = message.get_item("text")? {
                        let text: String = text.extract()?;
                        let state = state.clone();
                        let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                            state
                                .actor_addr
                                .send(SendToClient(WsMessage::SendText(text)))
                                .await
                                .map_err(|e| {
                                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                                        "Failed to send text: {}",
                                        e
                                    ))
                                })?;
                            Ok(Python::attach(|py| {
                                py.None().into_pyobject(py).unwrap().unbind()
                            }))
                        })?;
                        Ok(future.into())
                    } else if let Some(bytes) = message.get_item("bytes")? {
                        let data: Vec<u8> = bytes.extract()?;
                        let state = state.clone();
                        let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                            state
                                .actor_addr
                                .send(SendToClient(WsMessage::SendBinary(data)))
                                .await
                                .map_err(|e| {
                                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                                        "Failed to send binary: {}",
                                        e
                                    ))
                                })?;
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
                    let state = state.clone();
                    let future = pyo3_async_runtimes::tokio::future_into_py(py, async move {
                        state
                            .actor_addr
                            .send(SendToClient(WsMessage::Close { code, reason }))
                            .await
                            .map_err(|e| {
                                pyo3::exceptions::PyRuntimeError::new_err(format!(
                                    "Failed to send close: {}",
                                    e
                                ))
                            })?;
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

/// HTTP handler for WebSocket upgrade with full Python integration
///
/// Handles:
/// - Connection limit checking
/// - Rate limiting (reuses HTTP rate limit infrastructure)
/// - Origin validation (CORS-like protection)
/// - Authentication and guards
/// - WebSocket upgrade and actor setup
pub async fn handle_websocket_upgrade_with_handler(
    req: HttpRequest,
    stream: web::Payload,
    handler: Py<PyAny>,
    handler_id: usize,
    path_params: AHashMap<String, String>,
    state: Arc<AppState>,
    injector: Option<Py<PyAny>>,
) -> actix_web::Result<HttpResponse> {
    // Use cached config - no Python/GIL access
    let config = &*WS_CONFIG;

    // Validate request is actually a WebSocket upgrade
    if !is_websocket_upgrade(&req) {
        return Ok(HttpResponse::BadRequest().body("Expected WebSocket upgrade request"));
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

    // Get peer address for rate limiting
    let peer_addr = req.peer_addr().map(|addr| addr.ip().to_string());

    // Check rate limiting BEFORE origin validation (reuse HTTP rate limit)
    if let Some(route_metadata) = ROUTE_METADATA.get() {
        if let Some(route_meta) = route_metadata.get(handler_id) {
            if let Some(ref rate_config) = route_meta.rate_limit_config {
                if let Some(response) = check_rate_limit(
                    handler_id,
                    &headers,
                    peer_addr.as_deref(),
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
    if let Some(route_metadata) = ROUTE_METADATA.get() {
        if let Some(route_meta) = route_metadata.get(handler_id) {
            match validate_auth_and_guards(&headers, &route_meta.auth_backends, &route_meta.guards)
            {
                AuthGuardResult::Allow(_ctx) => {
                    // Guards passed, continue with WebSocket upgrade
                }
                AuthGuardResult::Unauthorized => {
                    return Ok(HttpResponse::Unauthorized()
                        .content_type("application/json")
                        .body(r#"{"detail":"Authentication required"}"#));
                }
                AuthGuardResult::Forbidden => {
                    return Ok(HttpResponse::Forbidden()
                        .content_type("application/json")
                        .body(r#"{"detail":"Permission denied"}"#));
                }
            }
        }
    }

    // Increment connection counter BEFORE any fallible operations
    // This ensures we always decrement if we fail after this point
    ACTIVE_WS_CONNECTIONS.fetch_add(1, Ordering::Relaxed);

    // Create channels for bidirectional communication (configurable size)
    let (to_python_tx, to_python_rx) = mpsc::channel::<WsMessage>(config.channel_buffer_size);

    // Get param_types from route metadata for type coercion
    let param_types = ROUTE_METADATA
        .get()
        .and_then(|m| m.get(handler_id))
        .map(|m| m.param_types.clone())
        .unwrap_or_default();

    // Build scope for Python - if this fails, decrement counter
    let scope = match Python::attach(|py| build_scope(py, &req, &path_params, &param_types)) {
        Ok(s) => s,
        Err(e) => {
            // CRITICAL: Decrement counter on error to prevent resource leak
            ACTIVE_WS_CONNECTIONS.fetch_sub(1, Ordering::Relaxed);
            return Err(actix_web::error::ErrorBadRequest(format!(
                "Invalid request: {}",
                e
            )));
        }
    };

    // Start the WebSocket actor
    let actor = WebSocketActor::new(to_python_tx);

    // Use WsResponseBuilder to start actor and get address - if this fails, decrement counter
    let (addr, resp) = match ws::WsResponseBuilder::new(actor, &req, stream)
        .frame_size(config.max_message_size)
        .start_with_addr()
    {
        Ok(result) => result,
        Err(e) => {
            // CRITICAL: Decrement counter on error to prevent resource leak
            ACTIVE_WS_CONNECTIONS.fetch_sub(1, Ordering::Relaxed);
            return Err(actix_web::error::ErrorInternalServerError(format!(
                "WebSocket error: {}",
                e
            )));
        }
    };

    // Create shared state for Python functions
    let ws_state = Arc::new(WsConnectionState {
        from_actor_rx: tokio::sync::Mutex::new(to_python_rx),
        actor_addr: addr.clone(),
    });

    // Spawn task to run Python handler using proper async integration
    // We use catch_unwind to ensure cleanup happens even on panic
    let ws_state_clone = ws_state.clone();
    actix_web::rt::spawn(async move {
        // Wrap the entire handler execution in catch_unwind to handle panics
        let result = std::panic::AssertUnwindSafe(async {
            // Create WebSocket instance and get the coroutine
            let future_result = Python::attach(|py| -> PyResult<_> {
                // Use cached WebSocket class (imports once, reuses)
                let ws_class = get_ws_class(py)?;

                // Create receive and send functions
                let receive_fn = create_receive_fn(py, ws_state_clone.clone())?;
                let send_fn = create_send_fn(py, ws_state_clone.clone())?;

                // Create WebSocket instance
                let websocket = ws_class.call1(py, (scope.clone_ref(py), receive_fn, send_fn))?;

                // Use cached build_websocket_request function (imports once, reuses)
                let build_request = get_build_request_fn(py)?;
                let request = build_request.call1(py, (scope,))?;

                // Call handler with proper parameter injection
                // Injector is pre-compiled at route registration and passed from router
                let coro = if let Some(ref inj) = injector {
                    // Use pre-compiled injector to extract parameters
                    // Sync injector - call directly
                    // Note: WebSocket handlers don't support async injectors (dependencies)
                    // since the injector is called synchronously during connection setup
                    let result = inj.call1(py, (request,))?;
                    let (args, kwargs): (Py<PyAny>, Py<PyAny>) = result.extract(py)?;

                    // Prepend websocket to args (accept any iterable: tuple or list)
                    let new_args = pyo3::types::PyList::new(py, std::iter::once(&websocket))?;
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
                };

                // Reuse the global event loop locals initialized at server startup (same as HTTP handlers)
                let locals = TASK_LOCALS.get().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized")
                })?;

                // Convert Python coroutine to Rust future using the shared event loop
                pyo3_async_runtimes::into_future_with_locals(locals, coro.bind(py).clone())
            });

            match future_result {
                Ok(future) => {
                    if let Err(e) = future.await {
                        eprintln!("[django-bolt] WebSocket handler error: {}", e);
                        // Close the connection on error - this triggers actor stopped() which decrements counter
                        let _ = addr
                            .send(SendToClient(WsMessage::Close {
                                code: 1011,
                                reason: "Internal error".to_string(),
                            }))
                            .await;
                    }
                    // Normal completion - actor will be stopped when handler returns and Python closes
                }
                Err(e) => {
                    eprintln!("[django-bolt] WebSocket handler setup error: {}", e);
                    // Close the connection on setup error - this triggers actor stopped() which decrements counter
                    let _ = addr
                        .send(SendToClient(WsMessage::Close {
                            code: 1011,
                            reason: "Handler setup failed".to_string(),
                        }))
                        .await;
                }
            }
        })
        .catch_unwind()
        .await;

        // If the task panicked, ensure we close the actor to trigger cleanup
        if result.is_err() {
            eprintln!("[django-bolt] WebSocket handler task panicked - closing connection");
            // Send close message to trigger actor stopped() which decrements counter
            let _ = addr
                .send(SendToClient(WsMessage::Close {
                    code: 1011,
                    reason: "Internal server error".to_string(),
                }))
                .await;
        }
    });

    Ok(resp)
}
