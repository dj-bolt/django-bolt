use actix_http::KeepAlive;
use actix_web::{
    self as aw,
    middleware::{NormalizePath, TrailingSlash},
    web, App, HttpRequest, HttpResponse, HttpServer,
};
use ahash::AHashMap;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use socket2::{Domain, Protocol, Socket, Type};
use std::net::{IpAddr, SocketAddr};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use crate::asgi_mounts::validate_and_sort_asgi_mounts;
use crate::handler::handle_request;
use crate::metadata::{CompressionConfig, CorsConfig, RouteMetadata, RouteMetadataStore};
use crate::middleware::compression::CompressionMiddleware;
use crate::middleware::cors::CorsMiddleware;
use crate::router::Router;
use crate::state::{
    AppState, StaticFilesConfig, GLOBAL_ASGI_MOUNTS, GLOBAL_ROUTER, GLOBAL_WEBSOCKET_ROUTER,
    ROUTE_METADATA, ROUTE_METADATA_TEMP, TASK_LOCALS,
};
use crate::static_files::handle_static_file;
use crate::websocket::{
    handle_websocket_upgrade_with_handler, is_websocket_upgrade, WebSocketRouter,
};

#[pyfunction]
pub fn register_routes(
    _py: Python<'_>,
    routes: Vec<(String, String, usize, Py<PyAny>)>,
) -> PyResult<()> {
    let mut router = Router::new();
    for (method, path, handler_id, handler) in routes {
        router.register(&method, &path, handler_id, handler.into())?;
    }
    GLOBAL_ROUTER
        .set(Arc::new(router))
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Router already initialized"))?;
    Ok(())
}

#[pyfunction]
pub fn register_websocket_routes(
    _py: Python<'_>,
    routes: Vec<(String, usize, Py<PyAny>, Option<Py<PyAny>>)>,
) -> PyResult<()> {
    let mut router = WebSocketRouter::new();
    for (path, handler_id, handler, injector) in routes {
        router.register(&path, handler_id, handler.into(), injector)?;
    }
    GLOBAL_WEBSOCKET_ROUTER.set(Arc::new(router)).map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("WebSocket router already initialized")
    })?;
    Ok(())
}

#[pyfunction]
pub fn register_asgi_mounts(_py: Python<'_>, mounts: Vec<(String, Py<PyAny>)>) -> PyResult<()> {
    let asgi_mounts = validate_and_sort_asgi_mounts(mounts)?;

    GLOBAL_ASGI_MOUNTS.set(Arc::new(asgi_mounts)).map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("ASGI mounts already initialized")
    })?;

    Ok(())
}

#[pyfunction]
pub fn register_middleware_metadata(
    py: Python<'_>,
    metadata: Vec<(usize, Py<PyAny>)>,
) -> PyResult<()> {
    let mut parsed_metadata_map = AHashMap::new();

    for (handler_id, meta) in metadata {
        // Parse Python metadata into typed Rust metadata
        if let Ok(py_dict) = meta.bind(py).cast::<PyDict>() {
            match RouteMetadata::from_python(py_dict, py) {
                Ok(parsed) => {
                    parsed_metadata_map.insert(handler_id, parsed);
                }
                Err(e) => {
                    eprintln!(
                        "Warning: Failed to parse metadata for handler {}: {}",
                        handler_id, e
                    );
                }
            }
        }
    }

    ROUTE_METADATA_TEMP.set(parsed_metadata_map).map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Route metadata already initialized")
    })?;

    Ok(())
}

#[pyfunction]
pub fn start_server(
    py: Python<'_>,
    dispatch: Py<PyAny>,
    host: String,
    port: u16,
    compression_config: Option<Py<PyAny>>,
    dispatch_sync: Py<PyAny>,
) -> PyResult<()> {
    if GLOBAL_ROUTER.get().is_none() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "Routes not registered",
        ));
    }

    // Configure tokio runtime with adequate blocking thread pool for concurrent streaming
    // Default is 512, but with concurrent SSE clients doing blocking operations (time.sleep),
    // we need enough threads to handle simultaneous blocking tasks
    let blocking_threads = std::env::var("DJANGO_BOLT_BLOCKING_THREADS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(1024); // Increased default to 1024 for better concurrent streaming support

    let mut runtime_builder = tokio::runtime::Builder::new_multi_thread();
    runtime_builder.max_blocking_threads(blocking_threads);
    pyo3_async_runtimes::tokio::init(runtime_builder);

    let loop_obj: Py<PyAny> = {
        // Try to use uvloop if available (2-4x faster than asyncio)
        let ev = match py.import("uvloop") {
            Ok(uvloop) => {
                // uvloop available - use it for better performance
                uvloop.call_method0("new_event_loop")?
            }
            Err(_) => {
                // uvloop not available - fall back to standard asyncio
                let asyncio = py.import("asyncio")?;
                asyncio.call_method0("new_event_loop")?
            }
        };
        let locals = pyo3_async_runtimes::TaskLocals::new(ev.clone()).copy_context(py)?;
        let _ = TASK_LOCALS.set(locals);
        ev.unbind().into()
    };
    std::thread::spawn(move || {
        Python::attach(|py| {
            let asyncio = py.import("asyncio").expect("import asyncio");
            let ev = loop_obj.bind(py);
            let _ = asyncio.call_method1("set_event_loop", (ev.as_any(),));
            let _ = ev.call_method0("run_forever");
        });
    });

    // Get configuration from Django settings ONCE at startup (not per-request)
    let (
        debug,
        max_header_size,
        max_payload_size,
        asgi_mount_timeout,
        cors_config_data,
        static_files_data,
        csp_header,
        access_log_enabled,
        access_logger_obj,
    ) = Python::attach(|py| {
        let debug = (|| -> PyResult<bool> {
            let django_conf = py.import("django.conf")?;
            let settings = django_conf.getattr("settings")?;
            settings.getattr("DEBUG")?.extract::<bool>()
        })()
        .unwrap_or(false);

        let max_header_size = (|| -> PyResult<usize> {
            let django_conf = py.import("django.conf")?;
            let settings = django_conf.getattr("settings")?;
            settings.getattr("BOLT_MAX_HEADER_SIZE")?.extract::<usize>()
        })()
        .unwrap_or(8192); // Default 8KB

        let max_payload_size = (|| -> PyResult<usize> {
            let django_conf = py.import("django.conf")?;
            let settings = django_conf.getattr("settings")?;
            settings.getattr("BOLT_MAX_UPLOAD_SIZE")?.extract::<usize>()
        })()
        .unwrap_or(1 * 1024 * 1024); // Default 1MB

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

        // Read django-cors-headers compatible CORS settings
        let cors_data = (|| -> PyResult<(Vec<String>, Vec<String>, bool, bool, Option<Vec<String>>, Option<Vec<String>>, Option<Vec<String>>, Option<u32>)> {
            let django_conf = py.import("django.conf")?;
            let settings = django_conf.getattr("settings")?;

            let origins = settings.getattr("CORS_ALLOWED_ORIGINS")
                .and_then(|o| o.extract::<Vec<String>>())
                .unwrap_or_else(|_| vec![]);

            let origin_regexes = settings.getattr("CORS_ALLOWED_ORIGIN_REGEXES")
                .and_then(|r| r.extract::<Vec<String>>())
                .unwrap_or_else(|_| vec![]);

            let allow_all = settings.getattr("CORS_ALLOW_ALL_ORIGINS")
                .and_then(|a| a.extract::<bool>())
                .unwrap_or(false);

            let credentials = settings.getattr("CORS_ALLOW_CREDENTIALS")
                .and_then(|c| c.extract::<bool>())
                .unwrap_or(false);

            let methods = settings.getattr("CORS_ALLOW_METHODS")
                .and_then(|m| m.extract::<Vec<String>>())
                .ok();

            let headers = settings.getattr("CORS_ALLOW_HEADERS")
                .and_then(|h| h.extract::<Vec<String>>())
                .ok();

            let expose_headers = settings.getattr("CORS_EXPOSE_HEADERS")
                .and_then(|e| e.extract::<Vec<String>>())
                .ok();

            let max_age = settings.getattr("CORS_PREFLIGHT_MAX_AGE")
                .and_then(|a| a.extract::<u32>())
                .ok();

            Ok((origins, origin_regexes, allow_all, credentials, methods, headers, expose_headers, max_age))
        })().unwrap_or_else(|_| (vec![], vec![], false, false, None, None, None, None));

        // Read static files configuration from Django settings
        // STATIC_URL: URL prefix for static files (e.g., "/static/")
        // STATIC_ROOT: Directory where collectstatic gathers files
        // STATICFILES_DIRS: Additional directories to search for static files
        let static_data = (|| -> PyResult<Option<(String, Vec<String>)>> {
            let django_conf = py.import("django.conf")?;
            let settings = django_conf.getattr("settings")?;

            // Get STATIC_URL (required for static serving)
            let static_url = match settings.getattr("STATIC_URL") {
                Ok(url) => url.extract::<String>().ok(),
                Err(_) => None,
            };

            let static_url = match static_url {
                Some(url) => url,
                None => return Ok(None), // No static URL configured
            };

            // Normalize URL prefix (remove trailing slash for actix-files)
            let url_prefix = static_url.trim_end_matches('/').to_string();
            if url_prefix.is_empty() {
                return Ok(None); // Invalid static URL
            }

            let mut directories: Vec<String> = Vec::new();

            // Get STATIC_ROOT (primary location for collected static files)
            // STATIC_ROOT can be a Path object, so convert via str()
            // STATIC_ROOT defaults to None in Django when not configured --
            // we must skip None to avoid converting it to the string "None".
            if let Ok(static_root) = settings.getattr("STATIC_ROOT") {
                if !static_root.is_none() {
                    let root_str = static_root.extract::<String>().or_else(|_| {
                        static_root
                            .call_method0("__str__")
                            .and_then(|s| s.extract::<String>())
                    });
                    if let Ok(root_str) = root_str {
                        if !root_str.is_empty() {
                            directories.push(root_str);
                        }
                    }
                }
            }

            // Get STATICFILES_DIRS (additional directories)
            if let Ok(static_dirs) = settings.getattr("STATICFILES_DIRS") {
                if let Ok(dirs) = static_dirs.extract::<Vec<String>>() {
                    for dir in dirs {
                        if !dir.is_empty() && !directories.contains(&dir) {
                            directories.push(dir);
                        }
                    }
                }
            }

            if directories.is_empty() {
                return Ok(None); // No static directories configured
            }

            Ok(Some((url_prefix, directories)))
        })()
        .unwrap_or(None);

        // Read CSP configuration from Django settings (Django 6.0+ SECURE_CSP)
        // CSP header is built once at startup for static files (no nonce support for static files)
        // See: https://docs.djangoproject.com/en/6.0/ref/csp/
        let csp_header: Option<String> = (|| -> Option<String> {
            use std::collections::HashMap;

            let django_conf = py.import("django.conf").ok()?;
            let settings = django_conf.getattr("settings").ok()?;

            let csp = settings.getattr("SECURE_CSP").ok()?;
            if csp.is_none() {
                return None;
            }
            let csp_directives: HashMap<String, Vec<String>> = csp.extract().ok()?;

            // Build CSP header string from directives
            let mut csp_parts: Vec<String> = Vec::new();

            for (directive, sources) in csp_directives {
                // Filter out CSP.NONCE sentinel values (can't inject nonces for static files)
                let filtered_sources: Vec<String> = sources
                    .into_iter()
                    .filter(|s| !s.contains("CSP_NONCE_SENTINEL"))
                    .collect();

                if !filtered_sources.is_empty() {
                    csp_parts.push(format!("{} {}", directive, filtered_sources.join(" ")));
                } else if directive == "upgrade-insecure-requests"
                    || directive == "block-all-mixed-content"
                {
                    // Boolean directives (no sources needed)
                    csp_parts.push(directive);
                }
            }

            if csp_parts.is_empty() {
                None
            } else {
                Some(csp_parts.join("; "))
            }
        })();

        // Check Django's logging configuration to determine if access logging is enabled.
        // Uses the standard django.server logger — no extra settings needed.
        // Decision is made once at startup (Granian pattern: zero cost when off).
        let (access_log_enabled, access_logger_obj) = (|| -> (bool, Option<Py<PyAny>>) {
            let logging = match py.import("logging") {
                Ok(m) => m,
                Err(_) => return (false, None),
            };
            let info_level: i32 = match logging.getattr("INFO").and_then(|v| v.extract()) {
                Ok(v) => v,
                Err(_) => return (false, None),
            };
            let logger = match logging.call_method1("getLogger", ("django.server",)) {
                Ok(l) => l,
                Err(_) => return (false, None),
            };
            let enabled = logger
                .call_method1("isEnabledFor", (info_level,))
                .and_then(|v| v.extract::<bool>())
                .unwrap_or(false);
            if enabled {
                (true, Some(logger.unbind()))
            } else {
                (false, None)
            }
        })();

        (
            debug,
            max_header_size,
            max_payload_size,
            asgi_mount_timeout,
            cors_data,
            static_data,
            csp_header,
            access_log_enabled,
            access_logger_obj,
        )
    });

    // Unpack CORS configuration data
    let (
        origins,
        origin_regex_patterns,
        allow_all,
        credentials,
        methods,
        headers,
        expose_headers,
        max_age,
    ) = cors_config_data;

    // Validate CORS configuration: wildcard + credentials is invalid per spec
    if allow_all && credentials {
        eprintln!("[django-bolt] Warning: CORS_ALLOW_ALL_ORIGINS=True with CORS_ALLOW_CREDENTIALS=True is invalid.");
        eprintln!(
            "[django-bolt] Per CORS spec, wildcard origin (*) cannot be used with credentials."
        );
        eprintln!("[django-bolt] CORS will reflect the request origin instead of using wildcard.");
    }

    // Build global CORS config if any CORS settings are configured
    let global_cors_config =
        if !origins.is_empty() || !origin_regex_patterns.is_empty() || allow_all {
            let mut cors_origins = origins.clone();

            // If CORS_ALLOW_ALL_ORIGINS = True, use wildcard
            if allow_all {
                cors_origins = vec!["*".to_string()];
            }

            Some(CorsConfig::from_django_settings(
                cors_origins,
                origin_regex_patterns.clone(),
                allow_all,
                credentials,
                methods,
                headers,
                expose_headers,
                max_age,
            ))
        } else {
            None
        };

    // Compile origin regex patterns at startup (zero runtime overhead)
    let cors_origin_regexes: Vec<regex::Regex> = origin_regex_patterns
        .iter()
        .filter_map(|pattern| {
            regex::Regex::new(pattern).ok().or_else(|| {
                eprintln!(
                    "[django-bolt] Warning: Invalid CORS origin regex pattern: {}",
                    pattern
                );
                None
            })
        })
        .collect();

    // Inject global CORS config into routes that don't have explicit config
    if let (Some(ref global_config), Some(metadata_temp)) =
        (&global_cors_config, ROUTE_METADATA_TEMP.get())
    {
        // Clone the metadata HashMap to make it mutable
        let mut updated_metadata = metadata_temp.clone();

        for (_handler_id, route_meta) in updated_metadata.iter_mut() {
            // Inject CORS if:
            // 1. Route doesn't have explicit cors_config
            // 2. CORS not skipped via @skip_middleware("cors")
            let should_inject =
                route_meta.cors_config.is_none() && !route_meta.skip.contains("cors");

            if should_inject {
                route_meta.cors_config = Some(global_config.clone());
            }
        }

        // Set the final ROUTE_METADATA with updated version (only set once)
        let _ = ROUTE_METADATA.set(Arc::new(RouteMetadataStore::from_map(updated_metadata)));
    } else if let Some(metadata_temp) = ROUTE_METADATA_TEMP.get() {
        // No global CORS config, just use the metadata as-is
        let _ = ROUTE_METADATA.set(Arc::new(RouteMetadataStore::from_map(
            metadata_temp.clone(),
        )));
    }

    // Parse compression configuration from Python
    let global_compression_config = compression_config.and_then(|config_py| {
        Python::attach(|py| {
            let config_obj = config_py.bind(py);

            // Extract compression settings from Python dict
            let backend = config_obj
                .get_item("backend")
                .ok()?
                .extract::<String>()
                .ok()?;
            let minimum_size = config_obj
                .get_item("minimum_size")
                .ok()?
                .extract::<usize>()
                .ok()?;
            let gzip_fallback = config_obj
                .get_item("gzip_fallback")
                .ok()?
                .extract::<bool>()
                .ok()?;

            Some(CompressionConfig {
                backend,
                minimum_size,
                gzip_fallback,
            })
        })
    });

    // Build static files configuration
    let static_files_config = static_files_data.and_then(|(url_prefix, directories)| {
        // Filter to only existing directories
        let valid_dirs: Vec<String> = directories
            .into_iter()
            .filter(|dir| Path::new(dir).is_dir())
            .collect();

        if valid_dirs.is_empty() {
            eprintln!(
                "[django-bolt] Warning: Static files: No valid directories found for {}",
                url_prefix
            );
            None
        } else {
            Some(StaticFilesConfig {
                url_prefix,
                directories: valid_dirs,
                csp_header: csp_header.clone(),
            })
        }
    });

    let app_state = Arc::new(AppState {
        dispatch: dispatch.into(),
        dispatch_sync: dispatch_sync.into(),
        debug,
        max_header_size,
        max_payload_size,
        asgi_mount_timeout,
        global_cors_config,
        cors_origin_regexes,
        global_compression_config: global_compression_config.clone(),
        router: None,         // Production uses GLOBAL_ROUTER
        route_metadata: None, // Production uses ROUTE_METADATA
        asgi_mounts: None,    // Production uses GLOBAL_ASGI_MOUNTS
        static_files_config: static_files_config.clone(),
        access_logger: access_logger_obj,
    });

    py.detach(|| {
        aw::rt::System::new()
            .block_on(async move {
                let workers: usize = std::env::var("DJANGO_BOLT_WORKERS")
                    .ok()
                    .and_then(|s| s.parse::<usize>().ok())
                    .filter(|&w| w >= 1)
                    .unwrap_or(1);

                // Read HTTP keep-alive configuration from environment
                let keep_alive = std::env::var("DJANGO_BOLT_KEEP_ALIVE")
                    .ok()
                    .and_then(|s| s.parse::<u64>().ok())
                    .map(|seconds| KeepAlive::Timeout(std::time::Duration::from_secs(seconds)))
                    .unwrap_or(KeepAlive::Os);

                {
                    let server = HttpServer::new(move || {
                        let mut app = App::new()
                            .app_data(web::Data::new(app_state.clone()))
                            .app_data(web::PayloadConfig::new(max_payload_size)) // Configure max request body size from BOLT_MAX_UPLOAD_SIZE
                            // MergeOnly: only normalize // -> / (not trailing slashes)
                            // Trailing slash redirects are handled in handler.rs on 404
                            .wrap(NormalizePath::new(TrailingSlash::MergeOnly))
                            .wrap(CorsMiddleware::new()) // Add CORS headers to all responses
                            .wrap(CompressionMiddleware::new()); // Respects Content-Encoding: identity from skip_compression

                        // Register WebSocket routes BEFORE the catch-all handler
                        // We iterate through all registered WebSocket paths and add explicit routes
                        if let Some(ws_router) = GLOBAL_WEBSOCKET_ROUTER.get() {
                            for path in ws_router.get_all_paths() {
                                app = app.route(&path, web::get().to(websocket_upgrade_handler));
                            }
                        }

                        // Register catch-all WebSocket 404 handler
                        // This matches all GET requests with WebSocket upgrade headers that didn't match
                        // registered WebSocket routes, and properly closes them with code 1000
                        app = app.route(
                            "/{path:.*}",
                            web::get()
                                .guard(actix_web::guard::fn_guard(is_websocket_upgrade_guard))
                                .to(websocket_not_found_handler),
                        );

                        // Register static files handler (if configured via Django settings)
                        // Uses a custom handler that:
                        // 1. Searches configured directories in order (fast path)
                        // 2. Falls back to Django's staticfiles finders (for app static files like admin)
                        // 3. Applies CSP headers from Django settings (if configured)
                        if let Some(ref config) = app_state.static_files_config {
                            let static_dirs = web::Data::new(config.directories.clone());
                            let static_csp = web::Data::new(config.csp_header.clone());
                            let static_route = format!("{}{{path:.*}}", config.url_prefix);
                            app = app
                                .app_data(static_dirs)
                                .app_data(static_csp)
                                .route(&static_route, web::get().to(handle_static_file));
                        }

                        // Default service handles all unmatched HTTP requests.
                        // Granian pattern: select handler variant at registration time.
                        // handle_request::<false> has zero access-logging instructions (compiler eliminated).
                        if access_log_enabled {
                            app.default_service(web::to(handle_request::<true>))
                        } else {
                            app.default_service(web::to(handle_request::<false>))
                        }
                    })
                    .keep_alive(keep_alive)
                    .client_request_timeout(std::time::Duration::from_secs(0))
                    .workers(workers);

                    let use_reuse_port = std::env::var("DJANGO_BOLT_REUSE_PORT")
                        .ok()
                        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                        .unwrap_or(false);

                    let backlog = std::env::var("DJANGO_BOLT_BACKLOG")
                        .ok()
                        .and_then(|s| s.parse::<i32>().ok())
                        .unwrap_or(1024);

                    // Always use socket2 for consistent backlog control
                    let ip: IpAddr = host.parse().unwrap_or(IpAddr::from([0, 0, 0, 0]));
                    let domain = match ip {
                        IpAddr::V4(_) => Domain::IPV4,
                        IpAddr::V6(_) => Domain::IPV6,
                    };
                    let socket = Socket::new(domain, Type::STREAM, Some(Protocol::TCP))
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    socket
                        .set_reuse_address(true)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

                    // Only set SO_REUSEPORT when explicitly requested (multi-process mode)
                    #[cfg(not(target_os = "windows"))]
                    if use_reuse_port {
                        socket
                            .set_reuse_port(true)
                            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    }

                    let addr = SocketAddr::new(ip, port);
                    socket
                        .bind(&addr.into())
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    socket
                        .listen(backlog)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    let listener: std::net::TcpListener = socket.into();
                    listener
                        .set_nonblocking(true)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    server
                        .listen(listener)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?
                        .run()
                        .await
                }
            })
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, format!("{:?}", e)))
    })
    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Server error: {}", e)))?;

    Ok(())
}

/// Guard function to detect WebSocket upgrade requests
/// Used for catch-all WebSocket 404 route
/// OPTIMIZATION: Use case-insensitive comparison without allocation
fn is_websocket_upgrade_guard(ctx: &actix_web::guard::GuardContext) -> bool {
    let headers = ctx.head().headers();

    // Check for Connection: upgrade header (can be comma-separated list)
    // OPTIMIZATION: Use eq_ignore_ascii_case instead of to_lowercase().contains()
    let has_upgrade_connection = headers
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

    // Check for Upgrade: websocket header
    headers
        .get("upgrade")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false)
}

/// Handler for WebSocket upgrade requests
/// This is registered as a service and checks against the WebSocket router
pub async fn websocket_upgrade_handler(
    req: HttpRequest,
    stream: web::Payload,
    state: web::Data<Arc<AppState>>,
) -> actix_web::Result<HttpResponse> {
    // Check if this is a WebSocket upgrade request
    if !is_websocket_upgrade(&req) {
        return Ok(HttpResponse::BadRequest().body("Not a WebSocket upgrade request"));
    }

    let path = req.path();

    // Normalize trailing slash for consistent matching
    // WebSocket clients typically don't follow redirects, so we normalize server-side
    let normalized_path = if path.len() > 1 && path.ends_with('/') {
        &path[..path.len() - 1]
    } else {
        path
    };

    // Look up in global WebSocket router
    if let Some(ws_router) = GLOBAL_WEBSOCKET_ROUTER.get() {
        if let Some((route, path_params)) = ws_router.find(normalized_path) {
            let (handler, injector) = Python::attach(|py| {
                (
                    route.handler.clone_ref(py),
                    route.injector.as_ref().map(|i| i.clone_ref(py)),
                )
            });
            // Pass AppState to WebSocket handler for CORS validation and connection tracking
            return handle_websocket_upgrade_with_handler(
                req,
                stream,
                handler,
                route.handler_id,
                path_params,
                state.get_ref().clone(),
                injector,
            )
            .await;
        }
    }

    Ok(HttpResponse::NotFound().body("WebSocket endpoint not found"))
}

/// Minimal WebSocket actor for 404 - accepts then immediately closes
struct WebSocketNotFoundActor;

impl actix::Actor for WebSocketNotFoundActor {
    type Context = actix_web_actors::ws::WebsocketContext<Self>;

    fn started(&mut self, ctx: &mut Self::Context) {
        use actix::ActorContext;
        // Immediately close with code 1000 (normal closure) - like Starlette
        ctx.close(Some(actix_web_actors::ws::CloseReason {
            code: actix_web_actors::ws::CloseCode::Normal,
            description: Some("Not Found".to_string()),
        }));
        ctx.stop();
    }
}

impl
    actix::StreamHandler<Result<actix_web_actors::ws::Message, actix_web_actors::ws::ProtocolError>>
    for WebSocketNotFoundActor
{
    fn handle(
        &mut self,
        _msg: Result<actix_web_actors::ws::Message, actix_web_actors::ws::ProtocolError>,
        _ctx: &mut Self::Context,
    ) {
        // Ignore all messages - we're closing immediately
    }
}

/// Handler for WebSocket upgrade requests to non-existent paths
/// Properly upgrades then closes with code 1000, avoiding client hangs
async fn websocket_not_found_handler(
    req: HttpRequest,
    stream: web::Payload,
) -> actix_web::Result<HttpResponse> {
    actix_web_actors::ws::start(WebSocketNotFoundActor, &req, stream)
}
