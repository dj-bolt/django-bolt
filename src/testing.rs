//! Async testing infrastructure for django-bolt using Actix Web's official test utilities.
//!
//! This module provides testing capabilities that:
//! - Use Actix Web's native test framework (`actix_web::test`)
//! - Run asynchronously in Rust (native async, no blocking)
//! - Reuse production code paths (handle_request, middleware, CORS, etc.)
//! - Support per-instance test apps (no global state conflicts)
//!
//! The test infrastructure mirrors the production server configuration exactly,
//! ensuring tests validate the actual request pipeline.

use actix_web::dev::Service;
use actix_web::http::header::HeaderValue;
use actix_web::middleware::{NormalizePath, TrailingSlash};
use actix_web::{test, web, App, HttpRequest, HttpResponse};
use ahash::AHashMap;
use bytes::Bytes;
use dashmap::DashMap;
use once_cell::sync::OnceCell;
use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::asgi_http;
use crate::asgi_mounts::validate_and_sort_asgi_mounts;
use crate::form_parsing::{
    parse_multipart, parse_urlencoded, FormParseResult, DEFAULT_MAX_PARTS, DEFAULT_MEMORY_LIMIT,
};
use crate::metadata::{CorsConfig, RouteMetadata, RouteMetadataStore};
use crate::middleware::compression::CompressionMiddleware;
use crate::middleware::cors::CorsMiddleware;
use crate::router::Router;
use crate::state::{find_asgi_mount, AppState, AsgiMount, StaticFilesConfig, TASK_LOCALS};
use crate::websocket::WebSocketRouter;
use actix_multipart::Multipart;
use futures_util::StreamExt;
use std::collections::HashMap;

use crate::handler::{
    build_prebound_args_kwargs, coerced_value_to_py, form_result_to_py, response_from_wire_result,
};
use crate::request_pipeline::validate_and_cache_typed_params;
use crate::static_files::handle_static_file;
use crate::type_coercion::{coerce_param, params_to_py_dict, TYPE_STRING};

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

/// Test application state stored per instance
pub struct TestAppState {
    pub router: Arc<Router>,
    pub websocket_router: Arc<WebSocketRouter>,
    pub asgi_mounts: Arc<Vec<AsgiMount>>,
    pub route_metadata: Arc<RouteMetadataStore>,
    pub dispatch: Py<PyAny>,
    pub dispatch_sync: Py<PyAny>,
    pub global_cors_config: Option<CorsConfig>,
    pub debug: bool,
    pub max_payload_size: usize,
    pub asgi_mount_timeout: Duration,
    /// Trailing slash handling mode: "strip", "append", or "keep"
    pub trailing_slash: String,
    /// Static files configuration for testing static file serving
    pub static_files_config: Option<StaticFilesConfig>,
}

/// Registry for test app instances
static TEST_REGISTRY: OnceCell<DashMap<u64, Arc<RwLock<TestAppState>>>> = OnceCell::new();
static TEST_ID_GEN: AtomicU64 = AtomicU64::new(1);

fn registry() -> &'static DashMap<u64, Arc<RwLock<TestAppState>>> {
    TEST_REGISTRY.get_or_init(DashMap::new)
}

/// Parse CORS config from a Python dict (matches production server parsing)
fn parse_cors_config_from_dict(dict: &Bound<'_, PyDict>) -> PyResult<CorsConfig> {
    use ahash::AHashSet;

    let origins: Vec<String> = dict
        .get_item("origins")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();

    let origin_set: AHashSet<String> = origins.iter().cloned().collect();
    let allow_all_origins = origins.iter().any(|o| o == "*");

    let credentials: bool = dict
        .get_item("credentials")?
        .map(|v| v.extract().unwrap_or(false))
        .unwrap_or(false);

    let methods: Vec<String> = dict
        .get_item("methods")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_else(|| {
            vec![
                "GET".to_string(),
                "POST".to_string(),
                "PUT".to_string(),
                "PATCH".to_string(),
                "DELETE".to_string(),
                "OPTIONS".to_string(),
            ]
        });

    let headers: Vec<String> = dict
        .get_item("headers")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_else(|| {
            vec![
                "accept".to_string(),
                "accept-encoding".to_string(),
                "authorization".to_string(),
                "content-type".to_string(),
                "dnt".to_string(),
                "origin".to_string(),
                "user-agent".to_string(),
                "x-csrftoken".to_string(),
                "x-requested-with".to_string(),
            ]
        });

    let expose_headers: Vec<String> = dict
        .get_item("expose_headers")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();

    let max_age: u32 = dict
        .get_item("max_age")?
        .map(|v| v.extract().unwrap_or(86400))
        .unwrap_or(86400);

    // Build pre-computed strings and cached HeaderValues
    let methods_str = methods.join(", ");
    let headers_str = headers.join(", ");
    let expose_headers_str = expose_headers.join(", ");
    let max_age_str = max_age.to_string();

    let methods_header = HeaderValue::from_str(&methods_str).ok();
    let headers_header = HeaderValue::from_str(&headers_str).ok();
    let expose_headers_header = if !expose_headers_str.is_empty() {
        HeaderValue::from_str(&expose_headers_str).ok()
    } else {
        None
    };
    let max_age_header = HeaderValue::from_str(&max_age_str).ok();

    Ok(CorsConfig {
        origins,
        origin_regexes: vec![],
        compiled_origin_regexes: vec![],
        origin_set,
        allow_all_origins,
        credentials,
        methods,
        headers,
        expose_headers,
        max_age,
        methods_str,
        headers_str,
        expose_headers_str,
        max_age_str,
        methods_header,
        headers_header,
        expose_headers_header,
        max_age_header,
    })
}

/// Create a test app instance and return its ID
#[pyfunction]
#[pyo3(signature = (dispatch, debug, cors_config=None, trailing_slash=None, static_files_config=None, dispatch_sync=None))]
pub fn create_test_app(
    py: Python<'_>,
    dispatch: Py<PyAny>,
    debug: bool,
    cors_config: Option<&Bound<'_, PyDict>>,
    trailing_slash: Option<String>,
    static_files_config: Option<&Bound<'_, PyDict>>,
    dispatch_sync: Option<Py<PyAny>>,
) -> PyResult<u64> {
    let global_cors_config = if let Some(cors_dict) = cors_config {
        Some(parse_cors_config_from_dict(cors_dict)?)
    } else {
        None
    };

    // Parse static files config from Python dict
    let static_config = if let Some(static_dict) = static_files_config {
        let url_prefix: String = static_dict
            .get_item("url_prefix")?
            .map(|v| v.extract().unwrap_or_default())
            .unwrap_or_else(|| "/static".to_string());

        let directories: Vec<String> = static_dict
            .get_item("directories")?
            .map(|v| v.extract().unwrap_or_default())
            .unwrap_or_default();

        let csp_header: Option<String> = static_dict
            .get_item("csp_header")?
            .and_then(|v| v.extract().ok());

        if !directories.is_empty() {
            Some(StaticFilesConfig {
                url_prefix,
                directories,
                csp_header,
            })
        } else {
            None
        }
    } else {
        None
    };

    // Read max payload size from Django settings (same as production server)
    // Default to 10MB for tests to handle large file uploads
    let max_payload_size: usize = (|| -> PyResult<usize> {
        let django_conf = py.import("django.conf")?;
        let settings = django_conf.getattr("settings")?;
        settings.getattr("BOLT_MAX_UPLOAD_SIZE")?.extract::<usize>()
    })()
    .unwrap_or(10 * 1024 * 1024); // Default to 10MB for tests

    let asgi_mount_timeout = (|| -> PyResult<f64> {
        let django_conf = py.import("django.conf")?;
        let settings = django_conf.getattr("settings")?;
        settings
            .getattr("BOLT_ASGI_MOUNT_TIMEOUT")?
            .extract::<f64>()
    })()
    .ok()
    .filter(|value| value.is_finite() && *value > 0.0)
    .map(Duration::from_secs_f64)
    .unwrap_or_else(|| Duration::from_secs(30)); // Default 30s

    let app = TestAppState {
        router: Arc::new(Router::new()),
        websocket_router: Arc::new(WebSocketRouter::new()),
        asgi_mounts: Arc::new(Vec::new()),
        route_metadata: Arc::new(RouteMetadataStore::default()),
        dispatch: dispatch.clone_ref(py),
        dispatch_sync: dispatch_sync
            .map(|ds| ds.clone_ref(py))
            .unwrap_or_else(|| dispatch.clone_ref(py)),
        global_cors_config,
        debug,
        max_payload_size,
        asgi_mount_timeout,
        trailing_slash: trailing_slash.unwrap_or_else(|| "strip".to_string()),
        static_files_config: static_config,
    };

    let id = TEST_ID_GEN.fetch_add(1, Ordering::Relaxed);
    registry().insert(id, Arc::new(RwLock::new(app)));
    Ok(id)
}

/// Destroy a test app instance
#[pyfunction]
pub fn destroy_test_app(app_id: u64) -> PyResult<()> {
    registry().remove(&app_id);
    Ok(())
}

/// Register HTTP routes for a test app
#[pyfunction]
pub fn register_test_routes(
    _py: Python<'_>,
    app_id: u64,
    routes: Vec<(String, String, usize, Py<PyAny>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();

    // Create a new router with the routes
    let mut router = Router::new();
    for (method, path, handler_id, handler) in routes {
        router.register(&method, &path, handler_id, handler)?;
    }
    app.router = Arc::new(router);
    Ok(())
}

/// Register WebSocket routes for a test app
#[pyfunction]
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
    _py: Python<'_>,
    app_id: u64,
    mounts: Vec<(String, Py<PyAny>)>,
) -> PyResult<()> {
    let entry = registry()
        .get(&app_id)
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("Invalid test app id"))?;

    let mut app = entry.write();
    let asgi_mounts = validate_and_sort_asgi_mounts(mounts)?;
    app.asgi_mounts = Arc::new(asgi_mounts);
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
        if let Ok(py_dict) = meta.bind(py).cast::<PyDict>() {
            if let Ok(parsed) = RouteMetadata::from_python(py_dict, py) {
                // Inject global CORS config if route doesn't have explicit config
                let mut route_meta = parsed;
                if route_meta.cors_config.is_none() && !route_meta.plan.skip_cors() {
                    route_meta.cors_config = app.global_cors_config.clone();
                }
                parsed_metadata.insert(handler_id, route_meta);
            }
        }
    }

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

        // Use the global runtime (initialized by pyo3_async_runtimes::tokio::init())
        // This ensures handler execution and streaming use the same runtime context
        let runtime_handle = pyo3_async_runtimes::tokio::get_runtime();

        runtime_handle.block_on(async {
            // Read test app state
            let (
                router,
                route_metadata,
                asgi_mounts,
                dispatch,
                dispatch_sync,
                global_cors_config,
                debug,
                max_payload_size,
                asgi_mount_timeout,
                _trailing_slash,
                static_files_config,
            ) = {
                let state = app_state.read();
                (
                    state.router.clone(),
                    state.route_metadata.clone(),
                    state.asgi_mounts.clone(),
                    Python::attach(|py| state.dispatch.clone_ref(py)),
                    Python::attach(|py| state.dispatch_sync.clone_ref(py)),
                    state.global_cors_config.clone(),
                    state.debug,
                    state.max_payload_size,
                    state.asgi_mount_timeout,
                    state.trailing_slash.clone(),
                    state.static_files_config.clone(),
                )
            };

            // Build AppState matching production
            // Include router and route_metadata so CorsMiddleware can find route-level CORS config
            let app_state_arc = Arc::new(AppState {
                dispatch,
                dispatch_sync,
                debug,
                max_header_size: 8192,
                max_payload_size,
                asgi_mount_timeout,
                global_cors_config: global_cors_config.clone(),
                cors_origin_regexes: vec![],
                global_compression_config: None,
                router: Some(router.clone()),
                route_metadata: Some(route_metadata.clone()),
                asgi_mounts: Some(asgi_mounts.clone()),
                static_files_config: static_files_config.clone(),
                access_logger: None,
            });

            // Clone the Arc values for the handler closure
            let router_for_handler = router.clone();
            let metadata_for_handler = route_metadata.clone();

            // Create the test handler that uses per-instance state
            // Use web::Payload to support multipart form parsing (which needs the stream)
            let handler = move |req: HttpRequest, payload: web::Payload| {
                let router = router_for_handler.clone();
                let metadata = metadata_for_handler.clone();

                async move { handle_test_request_internal(req, payload, router, metadata).await }
            };

            // Create Actix test service with production middleware stack
            // Use MergeOnly for NormalizePath (only normalizes // -> /)
            // Trailing slash handling is done via Starlette-style redirect in handler
            let app = if let Some(ref config) = static_files_config {
                // With static files: register static file handler before default service
                let static_dirs = web::Data::new(config.directories.clone());
                let static_csp = web::Data::new(config.csp_header.clone());
                let static_route = format!("{}{{path:.*}}", config.url_prefix);

                test::init_service(
                    App::new()
                        .app_data(web::Data::new(app_state_arc.clone()))
                        .app_data(web::PayloadConfig::new(max_payload_size))
                        .app_data(static_dirs)
                        .app_data(static_csp)
                        .wrap(NormalizePath::new(TrailingSlash::MergeOnly))
                        .wrap(CorsMiddleware::new())
                        .wrap(CompressionMiddleware::new())
                        .route(&static_route, web::get().to(handle_static_file))
                        .default_service(web::to(handler)),
                )
                .await
            } else {
                // Without static files: just the default handler
                test::init_service(
                    App::new()
                        .app_data(web::Data::new(app_state_arc.clone()))
                        .app_data(web::PayloadConfig::new(max_payload_size))
                        .wrap(NormalizePath::new(TrailingSlash::MergeOnly))
                        .wrap(CorsMiddleware::new())
                        .wrap(CompressionMiddleware::new())
                        .default_service(web::to(handler)),
                )
                .await
            };

            // Build full URI
            let uri = if let Some(qs) = query_string {
                format!("{}?{}", path, qs)
            } else {
                path.clone()
            };

            // Create test request
            let mut req = test::TestRequest::with_uri(&uri);

            // Set method
            req = match method.to_uppercase().as_str() {
                "GET" => req.method(actix_web::http::Method::GET),
                "POST" => req.method(actix_web::http::Method::POST),
                "PUT" => req.method(actix_web::http::Method::PUT),
                "PATCH" => req.method(actix_web::http::Method::PATCH),
                "DELETE" => req.method(actix_web::http::Method::DELETE),
                "OPTIONS" => req.method(actix_web::http::Method::OPTIONS),
                "HEAD" => req.method(actix_web::http::Method::HEAD),
                _ => req.method(actix_web::http::Method::GET),
            };

            // Set headers
            for (name, value) in headers {
                req = req.insert_header((name, value));
            }

            // Set body
            if !body.is_empty() {
                req = req.set_payload(Bytes::from(body));
            }

            // Execute request
            let request = req.to_request();
            let response = app.call(request).await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Service call failed: {}", e))
            })?;

            // Extract response
            let status = response.status().as_u16();

            let resp_headers: Vec<(String, String)> = response
                .headers()
                .iter()
                .map(|(k, v)| (k.as_str().to_string(), v.to_str().unwrap_or("").to_string()))
                .collect();

            // Use test::read_body which handles various body types including Encoder
            let resp_body = test::read_body(response).await.to_vec();

            Ok((status, resp_headers, resp_body))
        })
    })
}

/// Internal handler for test requests that uses per-instance state.
/// This mirrors the production `handle_request` but uses the provided router and metadata.
async fn handle_test_request_internal(
    req: HttpRequest,
    mut payload: web::Payload,
    router: Arc<Router>,
    route_metadata: Arc<RouteMetadataStore>,
) -> HttpResponse {
    use crate::handler::{extract_headers, handle_python_error};
    use crate::middleware;
    use crate::middleware::auth::populate_auth_context;
    use crate::request::PyRequest;
    use crate::responses;
    use crate::router::parse_query_string;
    use crate::validation::{parse_cookies_inline, validate_auth_and_guards, AuthGuardResult};

    let method = req.method().as_str();
    let path = req.path();

    // Get state from app data
    let state = match req.app_data::<web::Data<Arc<AppState>>>() {
        Some(s) => s.get_ref().clone(),
        None => {
            return HttpResponse::InternalServerError().body("App state not found");
        }
    };

    // Find route
    let (route_handler, path_params, handler_id) = {
        if let Some(route_match) = router.find(method, path) {
            let handler_id = route_match.handler_id();
            let handler = Python::attach(|py| route_match.route().handler.clone_ref(py));
            let path_params = route_match.path_params();
            (handler, path_params, handler_id)
        } else {
            // No route found - check for trailing slash redirect FIRST
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

            // Automatic OPTIONS handling
            if method == "OPTIONS" {
                let available_methods = router.find_all_methods(path);
                if !available_methods.is_empty() {
                    let allow_header = available_methods.join(", ");
                    return HttpResponse::NoContent()
                        .insert_header(("Allow", allow_header))
                        .insert_header(("Content-Type", "application/json"))
                        .finish();
                }
            }

            // HTTP ASGI mount fallback:
            // - only after Bolt route miss
            // - only after trailing-slash/API-method near-miss checks above
            if let Some(asgi_mount) = find_asgi_mount(&state, path) {
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

            if method == "OPTIONS" {
                // Handle OPTIONS preflight for non-existent routes
                if state.global_cors_config.is_some() {
                    return HttpResponse::NoContent().finish();
                }
            }

            return responses::error_404();
        }
    };

    // Get route metadata
    let route_meta = route_metadata.get(handler_id).cloned();

    // Parse query string
    let needs_query = route_meta
        .as_ref()
        .map(|m| m.plan.needs_query())
        .unwrap_or(true);
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

    // Validate typed parameters before GIL acquisition and cache non-string coerced values.
    let (path_coerced, query_coerced) = if let Some(ref meta) = route_meta {
        match validate_and_cache_typed_params(
            path_params.as_ref(),
            query_params.as_ref(),
            &meta.param_types,
        ) {
            Ok(cached) => cached,
            Err(response) => return response,
        }
    } else {
        (None, None)
    };

    // Extract headers
    let needs_headers = route_meta
        .as_ref()
        .map(|m| m.plan.needs_headers())
        .unwrap_or(true);
    let skip_cors = route_meta
        .as_ref()
        .map(|m| m.plan.skip_cors())
        .unwrap_or(false);
    let skip_compression = route_meta
        .as_ref()
        .map(|m| m.plan.skip_compression())
        .unwrap_or(false);

    let headers = match extract_headers(&req, state.max_header_size) {
        Ok(h) => h,
        Err(response) => return response,
    };

    let peer_addr = req.peer_addr().map(|addr| addr.ip().to_string());

    // Get connection info from Actix - handles proxies, IPv6, etc. correctly
    let conn_info = req.connection_info();
    let conn_host = conn_info.host().to_owned();
    let conn_scheme = conn_info.scheme().to_owned();
    let conn_remote_addr = conn_info
        .realip_remote_addr()
        .unwrap_or("127.0.0.1")
        .to_owned();

    // Rate limiting
    if let Some(ref meta) = route_meta {
        if let Some(ref rate_config) = meta.rate_limit_config {
            if let Some(response) = middleware::rate_limit::check_rate_limit(
                handler_id,
                &headers,
                peer_addr.as_deref(),
                rate_config,
                method,
                path,
            ) {
                return response;
            }
        }
    }

    // Auth and guards
    let auth_ctx = if let Some(ref meta) = route_meta {
        match validate_auth_and_guards(&headers, &meta.auth_backends, &meta.guards) {
            AuthGuardResult::Allow(ctx) => ctx,
            AuthGuardResult::Unauthorized => return responses::error_401(),
            AuthGuardResult::Forbidden => return responses::error_403(),
        }
    } else {
        None
    };

    // Cookies
    let needs_cookies = route_meta
        .as_ref()
        .map(|m| m.plan.needs_cookies())
        .unwrap_or(true);
    let cookies = if needs_cookies {
        parse_cookies_inline(headers.get("cookie").map(|s| s.as_str()))
    } else {
        AHashMap::new()
    };

    // Form parsing (URL-encoded and multipart)
    let needs_form_parsing = route_meta
        .as_ref()
        .map(|m| m.plan.needs_form_parsing())
        .unwrap_or(false);

    let content_type = headers
        .get("content-type")
        .map(|s| s.as_str())
        .unwrap_or("");

    let is_multipart = content_type.starts_with("multipart/form-data");
    let is_urlencoded = content_type.starts_with("application/x-www-form-urlencoded");

    // Read body from payload (before form parsing consumes it for multipart)
    let (body, form_result): (Vec<u8>, Option<FormParseResult>) =
        if needs_form_parsing && is_multipart {
            // Multipart form parsing - uses the payload stream directly
            let form_type_hints = route_meta
                .as_ref()
                .map(|m| &m.form_type_hints)
                .cloned()
                .unwrap_or_default();
            let file_constraints = route_meta
                .as_ref()
                .map(|m| &m.file_constraints)
                .cloned()
                .unwrap_or_default();
            let max_upload_size = route_meta
                .as_ref()
                .map(|m| m.max_upload_size)
                .unwrap_or(1024 * 1024);
            let memory_spool_threshold = route_meta
                .as_ref()
                .map(|m| m.memory_spool_threshold)
                .unwrap_or(DEFAULT_MEMORY_LIMIT);

            // Create Multipart from the payload
            let multipart = Multipart::new(req.headers(), payload);

            match parse_multipart(
                multipart,
                &form_type_hints,
                &file_constraints,
                max_upload_size,
                memory_spool_threshold,
                DEFAULT_MAX_PARTS,
            )
            .await
            {
                Ok(result) => (Vec::new(), Some(result)),
                Err(validation_error) => {
                    // Return HTTP 422 for validation errors
                    let body = serde_json::json!({
                        "detail": [validation_error.to_json()]
                    });
                    return HttpResponse::UnprocessableEntity()
                        .content_type("application/json")
                        .body(body.to_string());
                }
            }
        } else {
            // Read payload as bytes (for non-multipart requests)
            let mut body_bytes = web::BytesMut::new();
            while let Some(chunk) = payload.next().await {
                match chunk {
                    Ok(data) => body_bytes.extend_from_slice(&data),
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
            let body = body_bytes.freeze();

            // URL-encoded form parsing
            if needs_form_parsing && is_urlencoded {
                let form_type_hints = route_meta
                    .as_ref()
                    .map(|m| &m.form_type_hints)
                    .cloned()
                    .unwrap_or_default();

                match parse_urlencoded(&body, &form_type_hints) {
                    Ok(form_map) => {
                        let result = FormParseResult {
                            form_map,
                            files_map: HashMap::new(),
                        };
                        (body.to_vec(), Some(result))
                    }
                    Err(validation_error) => {
                        // Return HTTP 422 for validation errors
                        let body = serde_json::json!({
                            "detail": [validation_error.to_json()]
                        });
                        return HttpResponse::UnprocessableEntity()
                            .content_type("application/json")
                            .body(body.to_string());
                    }
                }
            } else {
                (body.to_vec(), None)
            }
        };

    let is_head_request = method == "HEAD";

    // Execute handler using run_coroutine_threadsafe to submit to background event loop
    // This reuses the global event loop instead of creating one per request via asyncio.run()
    let result_obj = match Python::attach(|py| -> PyResult<Py<PyAny>> {
        let dispatch = state.dispatch.clone_ref(py);
        let handler = route_handler.clone_ref(py);

        let context = if let Some(ref auth) = auth_ctx {
            let ctx_dict = PyDict::new(py);
            let ctx_py = ctx_dict.unbind();
            populate_auth_context(&ctx_py, auth, py);
            Some(ctx_py)
        } else {
            None
        };

        let headers_for_python = if needs_headers {
            headers.clone()
        } else {
            AHashMap::new()
        };

        // Get param_types from route metadata for typed conversion
        let param_types = route_meta
            .as_ref()
            .map(|m| &m.param_types)
            .cloned()
            .unwrap_or_default();

        // Create typed dicts - reuse pre-coerced path/query values from validation phase.
        let path_params_dict = if let Some(path_params) = path_params.as_ref() {
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

        let query_params_dict = if let Some(query_params) = query_params.as_ref() {
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

        let headers_dict = params_to_py_dict(py, &headers_for_python, &param_types)?;
        let cookies_dict = params_to_py_dict(py, &cookies, &param_types)?;

        // Only create state dict when Rust-side prebound args exist (matches production).
        let state_lock = std::sync::OnceLock::new();
        if let Some(bindings) = route_meta
            .as_ref()
            .and_then(|m| m.rust_arg_bindings.as_deref())
        {
            let empty_dict = PyDict::new(py);
            let pp_ref = match &path_params_dict {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            let qp_ref = match &query_params_dict {
                Some(d) => d.bind(py),
                None => &empty_dict,
            };
            if let Some((pre_args, pre_kwargs)) = build_prebound_args_kwargs(
                py,
                bindings,
                pp_ref,
                qp_ref,
                &headers_dict,
                &cookies_dict,
            ) {
                let state_dict = PyDict::new(py);
                state_dict.set_item("_bolt_prebound_args", pre_args)?;
                state_dict.set_item("_bolt_prebound_kwargs", pre_kwargs)?;
                let _ = state_lock.set(state_dict.unbind());
            }
        }

        // Only create form/files dicts when form data is present (matches production).
        let (form_map_opt, files_map_opt) = if let Some(ref result) = form_result {
            let (fm, fi) = form_result_to_py(py, result)
                .unwrap_or_else(|_| (PyDict::new(py).unbind(), PyDict::new(py).unbind()));
            (Some(fm), Some(fi))
        } else {
            (None, None)
        };

        let request = PyRequest {
            method: method.to_string(),
            path: path.to_string(),
            body: body.to_vec(),
            path_params: path_params_dict,
            query_params: query_params_dict,
            headers: Some(headers_dict.unbind()),
            cookies: Some(cookies_dict.unbind()),
            context,
            user: None,
            state: state_lock,
            form_map: form_map_opt,
            files_map: files_map_opt,
            meta_cache: std::sync::OnceLock::new(),
            conn_host: conn_host.clone(),
            conn_scheme: conn_scheme.clone(),
            conn_remote_addr: conn_remote_addr.clone(),
        };
        let request_obj = Py::new(py, request)?;

        // Get the event loop from TASK_LOCALS (initialized by ensure_task_locals_initialized)
        let locals = TASK_LOCALS.get().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized")
        })?;
        let event_loop = locals.event_loop(py);

        // Call dispatch to get a coroutine
        let coroutine = dispatch.call1(py, (handler, request_obj, handler_id))?;

        // Submit coroutine to background event loop using run_coroutine_threadsafe
        // This returns a concurrent.futures.Future that we can wait on
        let asyncio = py.import("asyncio")?;
        let future = asyncio.call_method1("run_coroutine_threadsafe", (coroutine, event_loop))?;

        // Wait for the result (releases GIL while waiting)
        let result = future.call_method0("result")?;
        Ok(result.unbind())
    }) {
        Ok(r) => r,
        Err(e) => {
            return Python::attach(|py| handle_python_error(py, e, path, method, state.debug));
        }
    };

    match response_from_wire_result(result_obj, skip_compression, skip_cors, is_head_request).await
    {
        Ok(response) => response,
        Err(e) => Python::attach(|py| {
            crate::error::build_error_response(
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

/// Handle WebSocket test request - validates and routes WebSocket connections
#[pyfunction]
pub fn handle_test_websocket(
    py: Python<'_>,
    app_id: u64,
    path: String,
    headers: Vec<(String, String)>,
    query_string: Option<String>,
) -> PyResult<(bool, usize, Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    use crate::middleware::auth::authenticate;
    use crate::permissions::{evaluate_guards, GuardResult};

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
        let origin_allowed = if let Some(ref cors_config) = app.global_cors_config {
            if cors_config.allow_all_origins {
                true
            } else {
                cors_config.origin_set.contains(origin_value)
                    || cors_config
                        .compiled_origin_regexes
                        .iter()
                        .any(|re| re.is_match(origin_value))
            }
        } else {
            false // No CORS = deny cross-origin
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
        None => return Ok((false, 0, py.None(), py.None(), py.None())),
    };

    let handler_id = route.handler_id;
    let handler = route.handler.clone_ref(py);

    // Rate limiting for WebSocket
    if let Some(route_meta) = app.route_metadata.get(handler_id) {
        if let Some(ref rate_config) = route_meta.rate_limit_config {
            if crate::middleware::rate_limit::check_rate_limit(
                handler_id,
                &header_map,
                Some("127.0.0.1"),
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
    if let Some(route_meta) = app.route_metadata.get(handler_id) {
        let auth_ctx = if !route_meta.auth_backends.is_empty() {
            authenticate(&header_map, &route_meta.auth_backends)
        } else {
            None
        };

        if !route_meta.guards.is_empty() {
            match evaluate_guards(&route_meta.guards, auth_ctx.as_ref()) {
                GuardResult::Allow => {}
                GuardResult::Unauthorized => {
                    return Err(pyo3::exceptions::PyPermissionError::new_err(
                        "Authentication required",
                    ));
                }
                GuardResult::Forbidden => {
                    return Err(pyo3::exceptions::PyPermissionError::new_err(
                        "Permission denied",
                    ));
                }
            }
        }
    }

    // Get param_types from route metadata for type coercion
    let param_types = app
        .route_metadata
        .get(handler_id)
        .map(|m| &m.param_types)
        .cloned()
        .unwrap_or_default();

    // Build path_params dict with type coercion
    let path_params_dict = pyo3::types::PyDict::new(py);
    for (k, v) in path_params.iter() {
        let type_hint = param_types.get(k).copied().unwrap_or(TYPE_STRING);
        match coerce_param(v, type_hint) {
            Ok(coerced) => {
                let py_value = coerced_value_to_py(py, &coerced);
                path_params_dict.set_item(k, py_value)?;
            }
            Err(_) => {
                path_params_dict.set_item(k, v)?;
            }
        }
    }

    // Build scope dict
    let scope_dict = pyo3::types::PyDict::new(py);
    scope_dict.set_item("type", "websocket")?;
    scope_dict.set_item("path", &path)?;

    // Parse and coerce query parameters
    let query_dict = pyo3::types::PyDict::new(py);
    if let Some(ref qs) = query_string {
        if !qs.is_empty() {
            for pair in qs.split('&') {
                if let Some((key, value)) = pair.split_once('=') {
                    let decoded_key = urlencoding::decode(key).unwrap_or_default();
                    let decoded_value = urlencoding::decode(value).unwrap_or_default();

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
                            query_dict.set_item(decoded_key.as_ref(), decoded_value.as_ref())?;
                        }
                    }
                }
            }
        }
    }
    scope_dict.set_item("query_params", query_dict)?;

    let qs_bytes = query_string.as_ref().map(|s| s.as_bytes()).unwrap_or(b"");
    scope_dict.set_item("query_string", pyo3::types::PyBytes::new(py, qs_bytes))?;

    let headers_dict = pyo3::types::PyDict::new(py);
    for (k, v) in headers.iter() {
        headers_dict.set_item(k.to_lowercase(), v)?;
    }
    scope_dict.set_item("headers", headers_dict)?;
    scope_dict.set_item("path_params", &path_params_dict)?;

    // Parse cookies
    let cookies_dict = pyo3::types::PyDict::new(py);
    for (k, v) in headers.iter() {
        if k.to_lowercase() == "cookie" {
            for pair in v.split(';') {
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

    let client_tuple = pyo3::types::PyTuple::new(py, &["127.0.0.1", "12345"])?;
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
            crate::middleware::auth::populate_auth_context(&ctx_dict.clone().unbind(), auth, py);
            scope_dict.set_item("auth_context", ctx_dict)?;
        }
    }

    Ok((
        true,
        handler_id,
        handler,
        path_params_dict.into(),
        scope_dict.into(),
    ))
}
