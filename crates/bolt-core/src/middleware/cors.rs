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
    http::header::ORIGIN,
    http::Method,
    Error,
};
use futures_util::future::LocalBoxFuture;
use std::future::{ready, Ready};
use std::sync::Arc;

use crate::cors::{add_cors_headers_with_config, add_preflight_headers_with_config};
use crate::metadata::CorsConfig;
use crate::state::{AppState, GLOBAL_ROUTER, ROUTE_METADATA};

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
            let cors_config = find_cors_config(&method, &path, state_ref);

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

/// Find CORS config for a request
/// Returns route-level config if present, otherwise global config
#[inline]
fn find_cors_config<'a>(
    method: &Method,
    path: &str,
    state: &'a AppState,
) -> Option<CorsConfigRef<'a>> {
    // Check router exists - use AppState router (tests) or global router (production)
    let has_router = state.router.is_some() || GLOBAL_ROUTER.get().is_some();
    if !has_router {
        // No router available - fall back to global CORS config only
        return state.global_cors_config.as_ref().map(CorsConfigRef::Global);
    }

    // For OPTIONS, try multiple methods to find route config
    let methods_to_try: &[&str] = if method == &Method::OPTIONS {
        &["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "QUERY"]
    } else {
        // Use a slice pointing to the method string
        // This avoids allocation for the common case
        return find_cors_for_method(method.as_str(), path, state);
    };

    // OPTIONS: try each method to find route-level CORS
    for try_method in methods_to_try {
        if let Some(result) = find_cors_for_method(try_method, path, state) {
            return Some(result);
        }
    }

    // Fall back to global CORS
    state.global_cors_config.as_ref().map(CorsConfigRef::Global)
}

#[inline]
fn find_cors_for_method<'a>(
    method: &str,
    path: &str,
    state: &'a AppState,
) -> Option<CorsConfigRef<'a>> {
    // Try AppState router first (tests), then global router (production)
    let route_match = if let Some(ref router) = state.router {
        router.find(method, path)
    } else {
        GLOBAL_ROUTER
            .get()
            .and_then(|router| router.find(method, path))
    };

    if let Some(route_match) = route_match {
        let handler_id = route_match.handler_id();

        // Try AppState metadata first (tests), then global metadata (production)
        let meta = if let Some(ref meta_map) = state.route_metadata {
            meta_map.get(handler_id)
        } else {
            ROUTE_METADATA
                .get()
                .and_then(|meta_map| meta_map.get(handler_id))
        };

        if let Some(meta) = meta {
            // Check if CORS is skipped
            if meta.skip.contains("cors") {
                return Some(CorsConfigRef::Skipped);
            }

            // Return route-level CORS if present
            if let Some(ref cors_cfg) = meta.cors_config {
                return Some(CorsConfigRef::Route(cors_cfg));
            }
        }
    }

    // Fall back to global CORS
    state.global_cors_config.as_ref().map(CorsConfigRef::Global)
}
