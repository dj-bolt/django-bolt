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
    dev::{forward_ready, Service, ServiceRequest, ServiceResponse, Transform},
    http::header::{ACCESS_CONTROL_REQUEST_METHOD, ORIGIN},
    http::Method,
    Error,
};
use futures_util::future::LocalBoxFuture;
use std::future::{ready, Ready};
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
                        state_ref,
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
                        state_ref,
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
