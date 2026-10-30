/// CORS middleware that adds CORS headers to all responses automatically.
///
/// This middleware handles:
/// - Adding CORS headers to all responses (including error responses)
/// - OPTIONS preflight requests
/// - Route-level CORS config override via @cors() decorator
/// - Skipping CORS via @skip_middleware("cors")
///
/// The middleware runs AFTER the handler, so it catches all responses including
/// errors from authentication, rate limiting, and Python exceptions.
use actix_web::{
    Error,
    dev::{Service, ServiceRequest, ServiceResponse, Transform, forward_ready},
    http::Method,
    http::header::{ACCESS_CONTROL_REQUEST_METHOD, ORIGIN},
};
use futures_util::future::LocalBoxFuture;
use std::future::{Ready, ready};
use std::sync::Arc;

use crate::cors::{add_cors_headers_with_config, add_preflight_headers_with_config};
use crate::metadata::CorsConfig;
use crate::state::AppState;

/// CORS middleware factory
pub struct CorsMiddleware;

impl CorsMiddleware {
    pub fn new() -> Self {
        Self
    }
}

impl Default for CorsMiddleware {
    fn default() -> Self {
        Self::new()
    }
}

impl<S, B> Transform<S, ServiceRequest> for CorsMiddleware
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type InitError = ();
    type Transform = CorsMiddlewareService<S>;
    type Future = Ready<Result<Self::Transform, Self::InitError>>;

    fn new_transform(&self, service: S) -> Self::Future {
        ready(Ok(CorsMiddlewareService { service }))
    }
}

pub struct CorsMiddlewareService<S> {
    service: S,
}

impl<S, B> Service<ServiceRequest> for CorsMiddlewareService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type Future = LocalBoxFuture<'static, Result<Self::Response, Self::Error>>;

    forward_ready!(service);

    fn call(&self, req: ServiceRequest) -> Self::Future {
        // Extract Origin header - no allocation if missing (common case for same-origin)
        let origin = req
            .headers()
            .get(ORIGIN)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());

        // Early exit: no Origin header means no CORS needed
        // This is the fast path for same-origin requests
        if origin.is_none() {
            let fut = self.service.call(req);
            return Box::pin(async move {
                let mut res = fut.await?;
                // Still need to check skip marker
                if res.headers().get("x-bolt-skip-cors").is_some() {
                    res.headers_mut().remove("x-bolt-skip-cors");
                }
                Ok(res)
            });
        }

        let method = req.method().clone();
        let path = req.path().to_string();
        // A preflight names the method of the real request.
        let requested_method = if method == Method::OPTIONS {
            req.headers()
                .get(ACCESS_CONTROL_REQUEST_METHOD)
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned)
        } else {
            None
        };

        // Get app state for CORS config
        let app_state = req
            .app_data::<actix_web::web::Data<Arc<AppState>>>()
            .cloned();

        let fut = self.service.call(req);

        Box::pin(async move {
            let mut res = fut.await?;

            // Fast path: check skip marker first
            if res.headers().get("x-bolt-skip-cors").is_some() {
                res.headers_mut().remove("x-bolt-skip-cors");
                return Ok(res);
            }

            let state = match app_state {
                Some(s) => s,
                None => return Ok(res),
            };
            let state_ref = state.get_ref();

            // Find CORS config: route-level first, then global
            let cors_config =
                find_cors_config(&method, requested_method.as_deref(), &path, state_ref);

            // Apply CORS headers
            match cors_config {
                Some(CorsConfigRef::Route(cors_cfg)) => {
                    let origin_allowed = add_cors_headers_with_config(
                        res.headers_mut(),
                        origin.as_deref(),
                        cors_cfg,
                        state_ref.global_cors_config.as_ref(),
                    );
                    if method == Method::OPTIONS && origin_allowed {
                        add_preflight_headers_with_config(res.headers_mut(), cors_cfg);
                    }
                }
                Some(CorsConfigRef::Global(cors_cfg)) => {
                    let origin_allowed = add_cors_headers_with_config(
                        res.headers_mut(),
                        origin.as_deref(),
                        cors_cfg,
                        state_ref.global_cors_config.as_ref(),
                    );
                    if method == Method::OPTIONS && origin_allowed {
                        add_preflight_headers_with_config(res.headers_mut(), cors_cfg);
                    }
                }
                Some(CorsConfigRef::Skipped) | None => {
                    // No CORS headers needed
                }
            }

            Ok(res)
        })
    }
}

/// Reference to CORS config - avoids cloning
enum CorsConfigRef<'a> {
    Route(&'a CorsConfig),
    Global(&'a CorsConfig),
    Skipped,
}

/// The methods that an OPTIONS request with no requested method tries, in order.
const PREFLIGHT_METHODS: [&str; 7] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "QUERY"];

/// Find the CORS config of a request: the config of its route, else the global config.
///
/// A preflight uses only the route of the method in `Access-Control-Request-Method`.
/// The config of another method grants nothing to that method. An OPTIONS
/// request with no requested method uses the first route on the path with its
/// own CORS decision.
#[inline]
fn find_cors_config<'a>(
    method: &Method,
    requested_method: Option<&str>,
    path: &str,
    state: &'a AppState,
) -> Option<CorsConfigRef<'a>> {
    let route_config = if method == Method::OPTIONS {
        match requested_method {
            Some(requested) => route_cors_config(requested, path, state),
            None => PREFLIGHT_METHODS
                .iter()
                .find_map(|m| route_cors_config(m, path, state)),
        }
    } else {
        route_cors_config(method.as_str(), path, state)
    };
    route_config.or_else(|| state.global_cors_config.as_ref().map(CorsConfigRef::Global))
}

/// The CORS decision of the route for `method` on `path`, or None when no
/// route matches or the route has no CORS config of its own.
#[inline]
fn route_cors_config<'a>(
    method: &str,
    path: &str,
    state: &'a AppState,
) -> Option<CorsConfigRef<'a>> {
    let route_match = state.router.find(method, path)?;
    let meta = state.route_metadata.get(route_match.handler_id())?;
    if meta.skip.contains("cors") {
        return Some(CorsConfigRef::Skipped);
    }
    meta.cors_config.as_ref().map(CorsConfigRef::Route)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metadata::{RouteMetadata, RouteMetadataStore};
    use crate::middleware::client_ip::TrustedProxies;
    use crate::middleware::rate_limit::RateLimiters;
    use crate::router::Router;
    use crate::streaming::StreamConfig;
    use actix_web::http::header::{ACCESS_CONTROL_ALLOW_METHODS, ACCESS_CONTROL_ALLOW_ORIGIN};
    use actix_web::test::{self, TestRequest};
    use actix_web::{App, HttpRequest, HttpResponse, web};
    use ahash::AHashMap;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    use std::time::Duration;

    const GLOBAL: &str = "https://global.example";
    const ROUTE: &str = "https://route.example";
    const OTHER: &str = "https://other.example";

    /// The CORS decision of a route.
    enum RouteCors {
        /// `@cors(origins=[ROUTE])`.
        Own,
        /// `@skip_middleware("cors")`.
        Skip,
        /// No decision of its own: the global config applies.
        Inherit,
    }

    /// An app state with `routes` and a global config that allows `GLOBAL`.
    fn state(routes: &[(&str, &str, RouteCors)]) -> Arc<AppState> {
        Python::initialize();
        Python::attach(|py| {
            let mut router = Router::new();
            let mut metadata = AHashMap::new();
            for (handler_id, (method, path, cors)) in routes.iter().enumerate() {
                router
                    .register(method, path, handler_id, py.None(), py.None(), py.None())
                    .unwrap();
                let meta = PyDict::new(py);
                for key in [
                    "needs_body",
                    "needs_query",
                    "needs_headers",
                    "needs_cookies",
                    "needs_path_params",
                    "needs_form_parsing",
                ] {
                    meta.set_item(key, false).unwrap();
                }
                meta.set_item("is_static_route", true).unwrap();
                meta.set_item("default_status_code", 200).unwrap();
                match cors {
                    RouteCors::Own => {
                        let middleware = PyDict::new(py);
                        middleware.set_item("type", "cors").unwrap();
                        middleware.set_item("origins", vec![ROUTE]).unwrap();
                        meta.set_item("middleware", vec![middleware]).unwrap();
                    }
                    RouteCors::Skip => meta.set_item("skip", vec!["cors"]).unwrap(),
                    RouteCors::Inherit => {}
                }
                metadata.insert(handler_id, RouteMetadata::from_python(&meta, py).unwrap());
            }
            Arc::new(AppState {
                dispatch: py.None(),
                debug: false,
                max_header_size: 8192,
                max_payload_size: 1024 * 1024,
                max_param_length: 4096,
                asgi_mount_timeout: Duration::from_secs(30),
                stream_config: StreamConfig {
                    sync_batch_size: 5,
                    channel_capacity: 32,
                    max_sync_threads: 1000,
                },
                global_cors_config: Some(CorsConfig::from_django_settings(
                    vec![GLOBAL.to_string()],
                    Vec::new(),
                    false,
                    false,
                    None,
                    None,
                    None,
                    None,
                )),
                global_compression_config: None,
                trusted_proxies: Arc::new(TrustedProxies::default()),
                rate_limiters: Arc::new(RateLimiters::default()),
                router: Arc::new(router),
                route_metadata: Arc::new(RouteMetadataStore::from_map(metadata)),
                asgi_mounts: Arc::new(Vec::new()),
                extensions: http::Extensions::new(),
                static_files_config: None,
                media_files_config: None,
                access_log_levels: 0,
                access_logger: None,
                error_renderer: None,
            })
        })
    }

    /// The CORS headers of a response: the allowed origin, and whether it has
    /// the preflight `Access-Control-Allow-Methods`.
    #[derive(Debug, PartialEq)]
    struct Cors {
        origin: Option<String>,
        preflight: bool,
    }

    const NONE: Cors = Cors {
        origin: None,
        preflight: false,
    };

    fn allowed(origin: &str) -> Cors {
        Cors {
            origin: Some(origin.to_string()),
            preflight: false,
        }
    }

    fn preflight(origin: &str) -> Cors {
        Cors {
            origin: Some(origin.to_string()),
            preflight: true,
        }
    }

    /// Send `request` through the middleware to a handler that answers 200.
    /// The handler of `/marker` sets the skip marker. Also check that the
    /// marker never reaches the client.
    async fn call(state: &Arc<AppState>, request: TestRequest) -> Cors {
        let app = test::init_service(
            App::new()
                .app_data(web::Data::new(state.clone()))
                .wrap(CorsMiddleware::new())
                .default_service(web::to(|req: HttpRequest| async move {
                    let mut response = HttpResponse::Ok();
                    if req.path() == "/marker" {
                        response.insert_header(("x-bolt-skip-cors", "1"));
                    }
                    response.finish()
                })),
        )
        .await;
        let response = test::call_service(&app, request.to_request()).await;
        let headers = response.headers();
        assert!(headers.get("x-bolt-skip-cors").is_none());
        Cors {
            origin: headers
                .get(ACCESS_CONTROL_ALLOW_ORIGIN)
                .map(|value| value.to_str().unwrap().to_string()),
            preflight: headers.contains_key(ACCESS_CONTROL_ALLOW_METHODS),
        }
    }

    fn get(path: &str, origin: &str) -> TestRequest {
        TestRequest::get().uri(path).insert_header((ORIGIN, origin))
    }

    fn options(path: &str, origin: &str, requested: Option<&str>) -> TestRequest {
        let request = TestRequest::default()
            .method(Method::OPTIONS)
            .uri(path)
            .insert_header((ORIGIN, origin));
        match requested {
            Some(method) => request.insert_header((ACCESS_CONTROL_REQUEST_METHOD, method)),
            None => request,
        }
    }

    #[actix_web::test]
    async fn a_request_with_no_origin_gets_no_cors_headers() {
        let state = state(&[]);
        for path in ["/items", "/marker"] {
            let request = TestRequest::get().uri(path);
            assert_eq!(call(&state, request).await, NONE);
        }
    }

    #[actix_web::test]
    async fn the_global_config_adds_preflight_headers_only_to_an_allowed_preflight() {
        let state = state(&[]);
        assert_eq!(call(&state, get("/items", GLOBAL)).await, allowed(GLOBAL));
        assert_eq!(call(&state, get("/items", OTHER)).await, NONE);
        assert_eq!(
            call(&state, options("/items", GLOBAL, None)).await,
            preflight(GLOBAL)
        );
        assert_eq!(call(&state, options("/items", OTHER, None)).await, NONE);
    }

    #[actix_web::test]
    async fn a_route_config_replaces_the_global_config() {
        let state = state(&[("GET", "/route", RouteCors::Own)]);
        assert_eq!(call(&state, get("/route", ROUTE)).await, allowed(ROUTE));
        assert_eq!(call(&state, get("/route", GLOBAL)).await, NONE);
        assert_eq!(call(&state, get("/other", GLOBAL)).await, allowed(GLOBAL));
    }

    #[actix_web::test]
    async fn a_preflight_uses_the_route_of_the_requested_method() {
        let state = state(&[
            ("GET", "/items", RouteCors::Inherit),
            ("POST", "/items", RouteCors::Own),
        ]);
        assert_eq!(
            call(&state, options("/items", ROUTE, Some("POST"))).await,
            preflight(ROUTE)
        );
        assert_eq!(
            call(&state, options("/items", GLOBAL, Some("POST"))).await,
            NONE
        );
        assert_eq!(
            call(&state, options("/items", GLOBAL, Some("GET"))).await,
            preflight(GLOBAL)
        );
        // The config of the POST route grants nothing to a GET request.
        assert_eq!(call(&state, get("/items", ROUTE)).await, NONE);
        assert_eq!(call(&state, get("/items", GLOBAL)).await, allowed(GLOBAL));
    }

    #[actix_web::test]
    async fn a_preflight_with_no_requested_method_uses_the_first_route_with_a_decision() {
        let state = state(&[
            ("GET", "/items", RouteCors::Inherit),
            ("PUT", "/items", RouteCors::Own),
        ]);
        assert_eq!(
            call(&state, options("/items", ROUTE, None)).await,
            preflight(ROUTE)
        );
        assert_eq!(call(&state, options("/items", GLOBAL, None)).await, NONE);
    }

    #[actix_web::test]
    async fn a_skipped_route_or_the_skip_marker_gets_no_cors_headers() {
        let state = state(&[("GET", "/skip", RouteCors::Skip)]);
        assert_eq!(call(&state, get("/skip", GLOBAL)).await, NONE);
        assert_eq!(call(&state, get("/marker", GLOBAL)).await, NONE);
    }
}
