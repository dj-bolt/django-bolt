//! In-process test server for the Python `TestClient`.
//!
//! Each request runs through `handler::handle_request`, the handler of `runbolt`.
//! The Actix app has the same middleware, CORS, compression and file scopes.
//! The request runs on a test worker thread, set up as an Actix worker of the
//! server. Each test app has its own routes, metadata and mounts, so tests do
//! not share state.

use actix_web::dev::Service;
use actix_web::http::header::HeaderValue;
use actix_web::middleware::{NormalizePath, TrailingSlash};
use actix_web::{test, web, App};
use ahash::AHashMap;
use bytes::Bytes;
use dashmap::DashMap;
use once_cell::sync::OnceCell;
use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use bolt_asgi::asgi_mounts::validate_and_sort_asgi_mounts;
use bolt_core::metadata::{RateLimitKey, RouteMetadata, RouteMetadataStore};
use bolt_core::middleware::compression::CompressionMiddleware;
use bolt_core::middleware::cors::CorsMiddleware;
use bolt_core::router::Router;
use bolt_core::state::{
    find_websocket_mount_in_slice, AppState, AsgiMount, ScopeConfig, ServeMode, TASK_LOCALS,
};
use bolt_websocket::handler::build_asgi_scope_from_parts;
use bolt_websocket::WebSocketRouter;

use crate::server::{configure_file_scopes, inject_global_cors, static_scope_prefix, ServerConfig};
use bolt_core::request_pipeline::{
    query_sequences, set_declared_item, set_param_item, set_query_sequences, EMPTY_TYPES,
};
use bolt_core::type_coercion::TypeHints;

static ASYNC_RUNTIME_INITIALIZED: std::sync::Once = std::sync::Once::new();

/// Initialize the tokio runtime, asyncio event loop, and TASK_LOCALS once
/// for the test environment (required for SSE/streaming and ASGI mounts).
fn ensure_task_locals_initialized() {
    use std::sync::mpsc;

    ASYNC_RUNTIME_INITIALIZED.call_once(|| {
        let mut runtime_builder = tokio::runtime::Builder::new_multi_thread();
        runtime_builder.enable_all();
        pyo3_async_runtimes::tokio::init(runtime_builder);

        let (tx, rx) = mpsc::channel();

        let loop_obj_opt: Option<Py<PyAny>> = Python::attach(|py| {
            let asyncio = match py.import("asyncio") {
                Ok(m) => m,
                Err(_) => return None,
            };

            let event_loop = match asyncio.call_method0("new_event_loop") {
                Ok(ev) => ev,
                Err(_) => return None,
            };

            match pyo3_async_runtimes::TaskLocals::new(event_loop.clone()).copy_context(py) {
                Ok(locals) => {
                    let _ = TASK_LOCALS.set(locals);
                    Some(event_loop.unbind())
                }
                Err(_) => None,
            }
        });

        if let Some(loop_obj) = loop_obj_opt {
            std::thread::spawn(move || {
                Python::attach(|py| {
                    let asyncio = match py.import("asyncio") {
                        Ok(m) => m,
                        Err(_) => {
                            let _ = tx.send(());
                            return;
                        }
                    };
                    let ev = loop_obj.bind(py);
                    let _ = asyncio.call_method1("set_event_loop", (ev.as_any(),));
                    let _ = tx.send(());
                    let _ = ev.call_method0("run_forever");
                });
            });

            // Release the GIL so the background thread can acquire it and
            // enter run_forever().
            Python::attach(|py| {
                py.detach(move || {
                    let _ = rx.recv_timeout(std::time::Duration::from_secs(5));
                    std::thread::sleep(std::time::Duration::from_millis(10));
                });
            });
        }
    });
}

/// A request for a test worker: it builds the request future on the worker thread.
type TestJob = Box<dyn FnOnce() -> std::pin::Pin<Box<dyn std::future::Future<Output = ()>>> + Send>;

static TEST_WORKERS: OnceCell<Vec<tokio::sync::mpsc::UnboundedSender<TestJob>>> = OnceCell::new();
static NEXT_TEST_WORKER: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// The worker threads of the test server, started on first use.
///
/// Each one is set up as an Actix worker of `runbolt`: a current-thread Tokio
/// runtime with a `LocalSet`, a pinned Python thread state, and its own bound
/// `WorkerLoop`. Thus a handler runs as on the server, and never on the thread
/// of the test: its context variables and thread-locals stay out of the test.
/// There are at least four, so concurrent test requests can run in parallel.
fn test_workers() -> &'static [tokio::sync::mpsc::UnboundedSender<TestJob>] {
    TEST_WORKERS.get_or_init(|| {
        let count = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4)
            .max(4);
        (0..count)
            .map(|index| {
                let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel::<TestJob>();
                std::thread::Builder::new()
                    .name(format!("bolt-test-worker-{index}"))
                    .spawn(move || {
                        bolt_core::state::pin_python_thread_state();
                        let runtime = tokio::runtime::Builder::new_current_thread()
                            .enable_all()
                            .build()
                            .expect("failed to build a test worker runtime");
                        let local = tokio::task::LocalSet::new();
                        local.block_on(&runtime, async move {
                            Python::attach(bolt_loop::bind_thread_loop).unwrap_or_else(|e| {
                                panic!("failed to create the test worker asyncio loop: {e}")
                            });
                            while let Some(job) = receiver.recv().await {
                                tokio::task::spawn_local(job());
                            }
                        });
                    })
                    .expect("failed to start a test worker thread");
                sender
            })
            .collect()
    })
}

thread_local! {
    /// The test worker of the calling thread, picked on its first request.
    static TEST_WORKER_INDEX: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
}

/// Send a request to the test worker of the calling thread.
///
/// A thread keeps its worker, as a keep-alive connection keeps its Actix
/// worker: consecutive requests of a test run on one worker thread. Requests
/// from other threads go to other workers, so they can run in parallel.
fn submit_test_job(job: TestJob) -> PyResult<()> {
    let workers = test_workers();
    let index = TEST_WORKER_INDEX.with(|slot| match slot.get() {
        Some(index) => index,
        None => {
            let index = NEXT_TEST_WORKER.fetch_add(1, Ordering::Relaxed) % workers.len();
            slot.set(Some(index));
            index
        }
    });
    workers[index]
        .send(job)
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("the test worker stopped"))
}

/// Close the Django connections of each test worker, and wait until they are
/// closed. A test client calls this at exit: a new `runbolt` process starts
/// with no connections, so a connection that a test broke must not stay.
#[pyfunction]
pub fn close_test_worker_connections(py: Python<'_>) -> PyResult<()> {
    let Some(workers) = TEST_WORKERS.get() else {
        return Ok(());
    };
    let (done_tx, done_rx) = std::sync::mpsc::channel::<()>();
    for worker in workers {
        let done_tx = done_tx.clone();
        let job: TestJob = Box::new(move || {
            Box::pin(async move {
                Python::attach(|py| {
                    let closed = py
                        .import("django_bolt.concurrency")
                        .and_then(|module| module.call_method0("close_lane_connections"));
                    if let Err(err) = closed {
                        err.print(py);
                    }
                });
                drop(done_tx);
            })
        });
        if worker.send(job).is_err() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "the test worker stopped",
            ));
        }
    }
    drop(done_tx);
    // The channel closes after the last worker dropped its sender.
    py.detach(move || while done_rx.recv().is_ok() {});
    Ok(())
}

/// The peer address of each test request: a client on the loopback interface.
const TEST_PEER_ADDR: std::net::SocketAddr =
    std::net::SocketAddr::new(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST), 50000);

/// Test application state stored per instance
pub struct TestAppState {
    pub router: Arc<Router>,
    pub websocket_router: Arc<WebSocketRouter>,
    pub asgi_mounts: Arc<Vec<AsgiMount>>,
    pub mcp_mounts: Arc<Vec<bolt_mcp::McpMount>>,
    pub route_metadata: Arc<RouteMetadataStore>,
    pub dispatch: Py<PyAny>,
    /// The Django settings, read by the same function as `runbolt`.
    pub config: ServerConfig,
    /// Global compression config (mirrors production server). Drives the
    /// streaming-compression codec selection in `handler.rs`.
    pub global_compression_config: Option<Arc<bolt_core::metadata::CompressionConfig>>,
}

/// Registry for test app instances
static TEST_REGISTRY: OnceCell<DashMap<u64, Arc<RwLock<TestAppState>>>> = OnceCell::new();
static TEST_ID_GEN: AtomicU64 = AtomicU64::new(1);

fn registry() -> &'static DashMap<u64, Arc<RwLock<TestAppState>>> {
    TEST_REGISTRY.get_or_init(DashMap::new)
}

/// Create a test app instance and return its ID.
///
/// The app reads the Django settings with the function that `runbolt` uses.
/// With `read_django_settings=False`, it uses no global CORS, static or media
/// settings. `cors_allowed_origins` and `static_files_config` replace the
/// settings for one test.
#[pyfunction]
#[pyo3(signature = (dispatch, read_django_settings=true, cors_allowed_origins=None, static_files_config=None, compression_config=None))]
pub fn create_test_app(
    py: Python<'_>,
    dispatch: Py<PyAny>,
    read_django_settings: bool,
    cors_allowed_origins: Option<Vec<String>>,
    static_files_config: Option<&Bound<'_, PyDict>>,
    compression_config: Option<&Bound<'_, PyDict>>,
) -> PyResult<u64> {
    let mut config = ServerConfig::from_django_settings(py)?;
    if !read_django_settings {
        config.global_cors_config = None;
        config.static_files_config = None;
        config.media_files_config = None;
    }
    if let Some(origins) = cors_allowed_origins {
        config.set_cors_origins(origins);
    }
    if let Some(static_dict) = static_files_config {
        config.static_files_config = static_scope_from_dict(static_dict, config.debug)?;
    }

    let global_compression_config = match compression_config {
        Some(d) => Some(Arc::new(
            bolt_core::metadata::CompressionConfig::from_python_dict(d.as_any())?,
        )),
        None => None,
    };

    let app = TestAppState {
        router: Arc::new(Router::new()),
        websocket_router: Arc::new(WebSocketRouter::new()),
        asgi_mounts: Arc::new(Vec::new()),
        mcp_mounts: Arc::new(Vec::new()),
        route_metadata: Arc::new(RouteMetadataStore::default()),
        dispatch: dispatch.clone_ref(py),
        config,
        global_compression_config,
    };

    let id = TEST_ID_GEN.fetch_add(1, Ordering::Relaxed);
    registry().insert(id, Arc::new(RwLock::new(app)));
    Ok(id)
}

/// Build a static scope from an explicit `static_files_config` dict with the
/// keys `url_prefix`, `directories`, `csp_header` and `cache_control`.
fn static_scope_from_dict(
    static_dict: &Bound<'_, PyDict>,
    debug: bool,
) -> PyResult<Option<Arc<ScopeConfig>>> {
    let url_prefix: String = static_dict
        .get_item("url_prefix")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_else(|| "/static".to_string());
    // The same prefix rules as STATIC_URL in runbolt.
    let Some(url_prefix) = static_scope_prefix(&url_prefix) else {
        return Ok(None);
    };

    let directories: Vec<String> = static_dict
        .get_item("directories")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();

    // Mirror the production hot-path contract: store pre-canonicalized
    // absolute roots so `find_in_directories` never canonicalizes the dir.
    let directories: Vec<PathBuf> = directories
        .iter()
        .filter_map(|dir| Path::new(dir).canonicalize().ok())
        .filter(|p| p.is_dir())
        .collect();

    let csp_header: Option<HeaderValue> = static_dict
        .get_item("csp_header")?
        .and_then(|v| v.extract::<String>().ok())
        .and_then(|s| HeaderValue::from_str(&s).ok());

    let cache_control: Option<HeaderValue> = static_dict
        .get_item("cache_control")?
        .and_then(|v| v.extract::<String>().ok())
        .and_then(|s| HeaderValue::from_str(&s).ok());

    // Mirror production: register when we have real dirs OR in DEBUG (where
    // the staticfiles-finders fallback serves admin/app static).
    if directories.is_empty() && !debug {
        return Ok(None);
    }
    Ok(Some(Arc::new(ScopeConfig {
        url_prefix,
        directories,
        csp_header,
        cache_control,
        mode: ServeMode::Static,
        allow_django_finders: debug,
    })))
}

/// Destroy a test app instance
#[pyfunction]
pub fn destroy_test_app(app_id: u64) -> PyResult<()> {
    registry().remove(&app_id);
    Ok(())
}

/// Register HTTP routes for a test app
#[pyfunction]
#[expect(
    clippy::type_complexity,
    reason = "PyO3 wire format: Python passes each route as one tuple"
)]
pub fn register_test_routes(
    _py: Python<'_>,
    app_id: u64,
    routes: Vec<(String, String, usize, Py<PyAny>, Py<PyAny>, Py<PyAny>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();

    // Create a new router with the routes
    let mut router = Router::new();
    for (method, path, handler_id, handler, dispatch, dispatch_sync) in routes {
        router.register(&method, &path, handler_id, handler, dispatch, dispatch_sync)?;
    }
    app.router = Arc::new(router);
    Ok(())
}

/// Register WebSocket routes for a test app
#[pyfunction]
#[expect(
    clippy::type_complexity,
    reason = "PyO3 wire format: Python passes each WebSocket route as one tuple"
)]
pub fn register_test_websocket_routes(
    _py: Python<'_>,
    app_id: u64,
    routes: Vec<(String, usize, Py<PyAny>, Option<Py<PyAny>>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();

    let mut ws_router = WebSocketRouter::new();
    for (path, handler_id, handler, injector) in routes {
        ws_router.register(&path, handler_id, handler, injector)?;
    }
    app.websocket_router = Arc::new(ws_router);
    Ok(())
}

/// Register HTTP ASGI mounts for a test app.
#[pyfunction]
pub fn register_test_asgi_mounts(
    py: Python<'_>,
    app_id: u64,
    mounts: Vec<(String, Py<PyAny>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();
    let asgi_mounts = validate_and_sort_asgi_mounts(py, mounts)?;
    app.asgi_mounts = Arc::new(asgi_mounts);
    Ok(())
}

/// Register MCP mounts for a test app. Same mount-definition dicts as the
/// production `register_mcp_mounts`.
#[pyfunction]
pub fn register_test_mcp_mounts(
    py: Python<'_>,
    app_id: u64,
    mounts: Vec<Py<PyAny>>,
) -> PyResult<()> {
    let mut parsed = Vec::with_capacity(mounts.len());
    for mount in &mounts {
        let dict = mount.bind(py);
        let dict = dict.cast::<PyDict>().map_err(|_| {
            pyo3::exceptions::PyValueError::new_err("MCP mount definition must be a dict")
        })?;
        parsed.push(bolt_mcp::parse_mount(py, dict)?);
    }

    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;
    let mut app = entry.write();
    app.mcp_mounts = Arc::new(parsed);
    Ok(())
}

/// Register middleware metadata for a test app
#[pyfunction]
pub fn register_test_middleware_metadata(
    py: Python<'_>,
    app_id: u64,
    metadata: Vec<(usize, Py<PyAny>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();

    let mut parsed_metadata: AHashMap<usize, RouteMetadata> = AHashMap::new();

    for (handler_id, meta) in metadata {
        let py_dict = meta.bind(py).cast::<PyDict>().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Route metadata for handler {} must be a dict: {}",
                handler_id, e
            ))
        })?;
        // Propagate parse failures so tests fail loudly instead of the route
        // silently losing its auth/middleware config.
        let route_meta = RouteMetadata::from_python(py_dict, py).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Failed to parse route metadata for handler {}: {}",
                handler_id, e
            ))
        })?;
        parsed_metadata.insert(handler_id, route_meta);
    }
    inject_global_cors(&mut parsed_metadata, app.config.global_cors_config.as_ref());

    app.route_metadata = Arc::new(RouteMetadataStore::from_map(parsed_metadata));
    Ok(())
}

/// Handle a test request using Actix's native test infrastructure.
///
/// This function:
/// 1. Creates an Actix test service matching production configuration
/// 2. Executes the request using a local tokio runtime
/// 3. Returns the response as (status_code, headers, body)
///
/// The request flows through the exact same code path as production:
/// - NormalizePath middleware
/// - CorsMiddleware
/// - CompressionMiddleware
/// - handle_request handler
///
/// Note: This is a synchronous function because Actix test utilities are !Send
/// and cannot be used with pyo3_async_runtimes::future_into_py. We create
/// a local tokio runtime for each request instead.
#[pyfunction]
#[expect(
    clippy::type_complexity,
    reason = "PyO3 wire format: TestClient reads (status, headers, body) as one tuple"
)]
pub fn test_request(
    py: Python<'_>,
    app_id: u64,
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    query_string: Option<String>,
) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
    py.detach(move || {
        // Ensure TASK_LOCALS is initialized for SSE/streaming support
        ensure_task_locals_initialized();

        // Get test app state
        let entry = registry()
            .get(&app_id)
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

        let app_state = entry.clone();
        drop(entry); // Release DashMap lock

        // The request runs on a test worker thread, as a request of the server
        // runs on an Actix worker. The handler never runs on the thread of the test.
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let job: TestJob = Box::new(move || {
            Box::pin(async move {
                let result: PyResult<(u16, Vec<(String, String)>, Vec<u8>)> = async move {
                    // Read test app state
                    let (
                        router,
                        route_metadata,
                        asgi_mounts,
                        mcp_mounts,
                        dispatch,
                        config,
                        compression,
                    ) = {
                        let state = app_state.read();
                        (
                            state.router.clone(),
                            state.route_metadata.clone(),
                            state.asgi_mounts.clone(),
                            state.mcp_mounts.clone(),
                            Python::attach(|py| state.dispatch.clone_ref(py)),
                            state.config.clone(),
                            state.global_compression_config.clone(),
                        )
                    };
                    let max_payload_size = config.max_payload_size;

                    // Build AppState matching production
                    // Include router and route_metadata so CorsMiddleware can find route-level CORS config
                    let cors_origin_regexes = config.cors_origin_regexes();
                    let app_state_arc = Arc::new(AppState {
                        dispatch,
                        debug: config.debug,
                        max_header_size: config.max_header_size,
                        max_payload_size,
                        max_param_length: config.max_param_length,
                        asgi_mount_timeout: config.asgi_mount_timeout,
                        global_cors_config: config.global_cors_config,
                        cors_origin_regexes,
                        global_compression_config: compression,
                        trusted_proxies: config.trusted_proxies,
                        router: router.clone(),
                        route_metadata: route_metadata.clone(),
                        asgi_mounts: asgi_mounts.clone(),
                        extensions: {
                            let mut ext = http::Extensions::new();
                            ext.insert(mcp_mounts.clone());
                            ext
                        },
                        static_files_config: config.static_files_config,
                        media_files_config: config.media_files_config,
                        access_logger: None,
                    });

                    // Create Actix test service with production middleware stack
                    // Use MergeOnly for NormalizePath (only normalizes // -> /)
                    // Trailing slash handling is done via Starlette-style redirect in handler
                    let app = test::init_service(
                        App::new()
                            .app_data(web::Data::new(app_state_arc.clone()))
                            .app_data(web::PayloadConfig::new(max_payload_size))
                            .wrap(NormalizePath::new(TrailingSlash::MergeOnly))
                            .wrap(CorsMiddleware::new())
                            .wrap(CompressionMiddleware::new())
                            .configure(|cfg| configure_file_scopes(cfg, &app_state_arc))
                            // The production request pipeline, with this app's state.
                            .default_service(web::to(crate::handler::handle_request::<false>)),
                    )
                    .await;

                    // Build full URI
                    let uri = if let Some(qs) = query_string {
                        format!("{}?{}", path, qs)
                    } else {
                        path.clone()
                    };

                    // `TestRequest::with_uri` panics on an invalid URI. Check it first.
                    if let Err(e) = actix_web::http::Uri::try_from(uri.as_str()) {
                        return Err(pyo3::exceptions::PyValueError::new_err(format!(
                            "Invalid request URI {uri:?}: {e}"
                        )));
                    }
                    // A real client has a peer address. Without one, client-IP rate
                    // limits and BOLT_TRUSTED_PROXIES cannot work as in production.
                    let mut req = test::TestRequest::with_uri(&uri).peer_addr(TEST_PEER_ADDR);

                    // Set method
                    let method_upper = method.to_uppercase();
                    req = match method_upper.as_str() {
                        "GET" => req.method(actix_web::http::Method::GET),
                        "POST" => req.method(actix_web::http::Method::POST),
                        "PUT" => req.method(actix_web::http::Method::PUT),
                        "PATCH" => req.method(actix_web::http::Method::PATCH),
                        "DELETE" => req.method(actix_web::http::Method::DELETE),
                        "OPTIONS" => req.method(actix_web::http::Method::OPTIONS),
                        "HEAD" => req.method(actix_web::http::Method::HEAD),
                        // QUERY is a supported method but has no actix constant.
                        "QUERY" => req.method(
                            actix_web::http::Method::from_bytes(b"QUERY")
                                .expect("QUERY is a valid HTTP method token"),
                        ),
                        // Reject anything that isn't a supported method instead of
                        // silently defaulting to GET, which would hide typos in tests.
                        other => {
                            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                                "Unsupported HTTP method {other:?}"
                            )));
                        }
                    };

                    // Append, so a repeated header keeps each value as on the wire.
                    for (name, value) in headers {
                        req = req.append_header((name, value));
                    }

                    // Set body
                    if !body.is_empty() {
                        req = req.set_payload(Bytes::from(body));
                    }

                    // Execute request
                    let request = req.to_request();
                    let response = app.call(request).await.map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "Service call failed: {}",
                            e
                        ))
                    })?;

                    // Extract response
                    let status = response.status().as_u16();

                    let resp_headers: Vec<(String, String)> = response
                        .headers()
                        .iter()
                        .map(|(k, v)| {
                            (k.as_str().to_string(), v.to_str().unwrap_or("").to_string())
                        })
                        .collect();

                    // Use test::read_body which handles various body types including Encoder
                    let resp_body = test::read_body(response).await.to_vec();

                    Ok((status, resp_headers, resp_body))
                }
                .await;
                let _ = done_tx.send(result);
            })
        });
        submit_test_job(job)?;
        done_rx.recv().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("the test worker dropped the request")
        })?
    })
}

/// Handle WebSocket test request - validates and routes WebSocket connections
///
/// Returns `(found, is_asgi_mount, handler_id, handler, path_params, scope)`.
/// When `is_asgi_mount` is true, `handler` is a raw ASGI application and the
/// caller must drive it with the scope, receive, and send triple.
#[pyfunction]
#[expect(
    clippy::type_complexity,
    reason = "PyO3 wire format: the Python TestClient unpacks this tuple"
)]
pub fn handle_test_websocket(
    py: Python<'_>,
    app_id: u64,
    path: String,
    headers: Vec<(String, String)>,
    query_string: Option<String>,
) -> PyResult<(bool, bool, usize, Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    use bolt_core::middleware::auth::authenticate;
    use bolt_core::permissions::{evaluate_guards, GuardResult};
    use bolt_core::router::parse_query_string;

    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let app = entry.read();

    // Convert headers to map
    let mut header_map: AHashMap<String, String> = AHashMap::with_capacity(headers.len());
    for (name, value) in headers.iter() {
        header_map.insert(name.to_lowercase(), value.clone());
    }

    // Origin validation for WebSocket
    let origin = header_map.get("origin");
    if let Some(origin_value) = origin {
        // The production check. No CORS config denies a cross-origin connection.
        let origin_allowed = match app.config.global_cors_config {
            Some(ref cors_config) => bolt_websocket::handler::is_origin_allowed(
                origin_value,
                cors_config,
                &app.config.cors_origin_regexes(),
            ),
            None => false,
        };

        if !origin_allowed {
            return Err(pyo3::exceptions::PyPermissionError::new_err(format!(
                "Origin not allowed: {}",
                origin_value
            )));
        }
    }

    // Normalize path
    let normalized_path = if path.len() > 1 && path.ends_with('/') {
        &path[..path.len() - 1]
    } else {
        &path
    };

    // Find WebSocket route
    let (route, path_params) = match app.websocket_router.find(normalized_path) {
        Some((route, params)) => (route, params),
        // No route matched: fall back to a mounted ASGI app, as the server does.
        None => {
            return match find_websocket_mount_in_slice(&app.asgi_mounts, normalized_path) {
                Some(mount) => {
                    // Give the app the request path as sent, as the server does.
                    let scope = build_asgi_scope_from_parts(
                        py,
                        &mount.prefix,
                        &path,
                        query_string.as_deref().unwrap_or_default().as_bytes(),
                        header_map
                            .iter()
                            .map(|(name, value)| (name.as_str(), value.as_bytes())),
                        false,
                        Some(("127.0.0.1".to_string(), 0)),
                    )?;
                    Ok((
                        true,
                        true,
                        0,
                        mount.app.clone_ref(py),
                        pyo3::types::PyDict::new(py).into(),
                        scope,
                    ))
                }
                None => Ok((false, false, 0, py.None(), py.None(), py.None())),
            };
        }
    };

    let handler_id = route.handler_id;
    let handler = route.handler.clone_ref(py);
    // Rate limiting for WebSocket: address and header keys before auth,
    // identity keys after it.
    let mut client_ip = None;
    if let Some(route_meta) = app.route_metadata.get(handler_id) {
        if let Some(ref rate_config) = route_meta.rate_limit_config {
            client_ip = (!matches!(rate_config.key, RateLimitKey::Header(_)))
                .then(|| {
                    bolt_core::middleware::client_ip::resolve(
                        header_map
                            .get("x-forwarded-for")
                            .into_iter()
                            .map(|value| Some(value.as_str())),
                        header_map
                            .get("x-real-ip")
                            .map(|value| Some(value.as_str())),
                        Some(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)),
                        &app.config.trusted_proxies,
                    )
                })
                .flatten();
            if bolt_core::middleware::rate_limit::check_before_auth(
                handler_id,
                &header_map,
                client_ip.as_ref(),
                rate_config,
                "GET",
                &path,
            )
            .is_some()
            {
                return Err(pyo3::exceptions::PyPermissionError::new_err(
                    "Rate limit exceeded",
                ));
            }
        }
    }

    // Auth and guards for WebSocket
    // The auth context for the revocation check, which the Python test client
    // awaits before it starts the handler (the server awaits it in the handshake).
    let mut revocation_auth: Option<Py<PyDict>> = None;
    if let Some(route_meta) = app.route_metadata.get(handler_id) {
        let auth_ctx = if !route_meta.auth_backends.is_empty() {
            authenticate(&header_map, &route_meta.auth_backends)
        } else {
            None
        };
        if let (Some(_), Some(ctx)) = (
            route_meta.websocket_revocation_check.as_ref(),
            auth_ctx.as_ref(),
        ) {
            let context = PyDict::new(py).unbind();
            bolt_core::middleware::auth::populate_auth_context(&context, ctx, py);
            revocation_auth = Some(context);
        }

        if !route_meta.guards.is_empty() {
            match evaluate_guards(&route_meta.guards, auth_ctx.as_ref()) {
                GuardResult::Allow => {}
                GuardResult::Unauthorized => {
                    return Err(pyo3::exceptions::PyPermissionError::new_err(
                        "Authentication required",
                    ));
                }
                GuardResult::Forbidden(denial) => {
                    return Err(pyo3::exceptions::PyPermissionError::new_err(
                        denial.map_or_else(
                            || "Permission denied".to_string(),
                            |denial| denial.text.clone(),
                        ),
                    ));
                }
            }
        }

        // After guards, mirroring the production upgrade path: a rejected
        // upgrade must not spend the bucket it would have been counted in.
        if let Some(rate_config) = route_meta.rate_limit_config.as_ref() {
            if bolt_core::middleware::rate_limit::check_after_auth(
                handler_id,
                &header_map,
                client_ip.as_ref(),
                auth_ctx.as_ref(),
                rate_config,
                "GET",
                &path,
            )
            .is_some()
            {
                return Err(pyo3::exceptions::PyPermissionError::new_err(
                    "Rate limit exceeded",
                ));
            }
        }
    }

    // Get type hints from route metadata for type coercion
    let ws_route_meta = app.route_metadata.get(handler_id);
    let empty_types: &TypeHints = &EMPTY_TYPES;
    let param_types = ws_route_meta.map_or(empty_types, |m| &m.param_types);
    let header_types = ws_route_meta.map_or(empty_types, |m| &m.header_types);
    let cookie_types = ws_route_meta.map_or(empty_types, |m| &m.cookie_types);

    // Build path_params dict with type coercion
    let max_param_length = app.config.max_param_length;
    let path_params_dict = pyo3::types::PyDict::new(py);
    // A value that is too long or a bad typed value rejects the upgrade, as in production.
    for (k, v) in path_params.iter() {
        set_param_item(
            py,
            &path_params_dict,
            k,
            v,
            param_types,
            max_param_length,
            "Path parameter",
        )?;
    }

    // Build scope dict
    let scope_dict = pyo3::types::PyDict::new(py);
    scope_dict.set_item("type", "websocket")?;
    scope_dict.set_item("path", &path)?;
    if let Some(context) = revocation_auth {
        scope_dict.set_item("_bolt_revocation_auth", context)?;
    }

    // Parse the query string with the HTTP parser, as production does.
    let query_dict = pyo3::types::PyDict::new(py);
    if let Some(ref qs) = query_string {
        for (key, value) in &parse_query_string(qs) {
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
    }
    // A sequence parameter takes each value of its repeated key, as in production.
    // Each value gets the length check, as the loop above checks only the last one.
    if let Some(ws_route_meta) = ws_route_meta {
        let sequences = query_sequences(
            query_string.as_deref(),
            &ws_route_meta.query_seq_fields,
            max_param_length,
        )
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
        set_query_sequences(py, &query_dict, &sequences)?;
    }
    scope_dict.set_item("query_params", query_dict)?;

    let qs_bytes = query_string.as_ref().map(|s| s.as_bytes()).unwrap_or(b"");
    scope_dict.set_item("query_string", pyo3::types::PyBytes::new(py, qs_bytes))?;

    // A typed header with a bad value rejects the upgrade, as in production.
    let headers_dict = pyo3::types::PyDict::new(py);
    for (k, v) in headers.iter() {
        set_declared_item(
            py,
            &headers_dict,
            &k.to_lowercase(),
            v,
            header_types,
            max_param_length,
            "Header",
        )?;
    }
    scope_dict.set_item("headers", headers_dict)?;
    scope_dict.set_item("path_params", &path_params_dict)?;
    let subprotocols = bolt_websocket::requested_subprotocols(
        headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case("sec-websocket-protocol"))
            .map(|(_, v)| v.as_str()),
    );
    scope_dict.set_item("subprotocols", subprotocols)?;

    // Parse cookies
    let cookies_dict = pyo3::types::PyDict::new(py);
    for (k, v) in headers.iter() {
        if k.to_lowercase() == "cookie" {
            for pair in v.split(';') {
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

    let client_tuple = pyo3::types::PyTuple::new(py, ["127.0.0.1", "12345"])?;
    scope_dict.set_item("client", client_tuple)?;

    // Add auth context if present
    if let Some(route_meta) = app.route_metadata.get(handler_id) {
        let auth_ctx = if !route_meta.auth_backends.is_empty() {
            authenticate(&header_map, &route_meta.auth_backends)
        } else {
            None
        };

        if let Some(ref auth) = auth_ctx {
            let ctx_dict = pyo3::types::PyDict::new(py);
            bolt_core::middleware::auth::populate_auth_context(
                &ctx_dict.clone().unbind(),
                auth,
                py,
            );
            scope_dict.set_item("auth_context", ctx_dict)?;
        }
    }

    Ok((
        true,
        false,
        handler_id,
        handler,
        path_params_dict.into(),
        scope_dict.into(),
    ))
}
