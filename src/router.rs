use ahash::AHashMap;
use matchit::{Match, Router as MatchRouter};
use pyo3::prelude::*;
use std::borrow::Cow;

/// Lookup result type that indicates whether path params exist
/// For static routes, params is always None (avoiding allocation)
/// For dynamic routes, params contains the extracted path parameters
pub enum RouteMatch<'a> {
    Static(&'a Route),
    Dynamic(&'a Route, AHashMap<String, String>),
}

impl<'a> RouteMatch<'a> {
    /// Get the route handler
    #[inline]
    pub fn route(&self) -> &Route {
        match self {
            RouteMatch::Static(r) => r,
            RouteMatch::Dynamic(r, _) => r,
        }
    }

    /// Get owned path params when present (None for static routes)
    #[inline]
    pub fn path_params(self) -> Option<AHashMap<String, String>> {
        match self {
            RouteMatch::Static(_) => None,
            RouteMatch::Dynamic(_, params) => Some(params),
        }
    }

    /// Check if this is a static route (no path params)
    #[inline]
    #[allow(dead_code)]
    pub fn is_static(&self) -> bool {
        matches!(self, RouteMatch::Static(_))
    }

    /// Get handler_id
    #[inline]
    pub fn handler_id(&self) -> usize {
        self.route().handler_id
    }
}

/// Route handler with metadata
/// Used for both static and dynamic routes
#[repr(C)]
pub struct Route {
    pub handler: Py<PyAny>,
    pub handler_id: usize, // Store handler_id for middleware metadata lookup
}

/// Check if a path contains any path parameters (dynamic segments)
/// Returns true if path contains {param} patterns
/// OPTIMIZATION: #[inline(always)] - very small function called during registration
#[inline(always)]
fn is_static_path(path: &str) -> bool {
    !path.contains('{')
}

/// Convert FastAPI-style paths like /items/{id} and /files/{path:path}
/// Matchit uses the same {param} syntax as FastAPI, but uses *path for catch-all
pub fn convert_path(path: &str) -> String {
    let mut result = String::with_capacity(path.len());
    let mut chars = path.chars().peekable();

    while let Some(ch) = chars.next() {
        if ch == '{' {
            result.push(ch);
            let mut param = String::new();

            // Collect parameter name and optional type
            while let Some(&next_ch) = chars.peek() {
                if next_ch == '}' {
                    chars.next(); // consume '}'
                    break;
                }
                param.push(chars.next().unwrap());
            }

            // Check if it has :path suffix
            if let Some(colon_pos) = param.find(':') {
                let name = &param[..colon_pos];
                let type_ = &param[colon_pos + 1..];

                if type_ == "path" {
                    // Convert {name:path} to {*name} (catch-all)
                    // matchit requires catch-all to be inside braces: {*param}
                    result.push('*');
                    result.push_str(name);
                    result.push('}');
                    continue;
                }
            }

            // Regular parameter: just keep the name
            if let Some(colon_pos) = param.find(':') {
                result.push_str(&param[..colon_pos]);
            } else {
                result.push_str(&param);
            }
            result.push('}');
        } else {
            result.push(ch);
        }
    }

    result
}

/// Per-method router combining static (O(1) HashMap) and dynamic (radix tree) routing
/// Inspired by Elysia's router optimization that separates static from dynamic routes
struct MethodRouter {
    /// O(1) lookup for static routes (e.g., /users, /health, /api/items)
    /// These routes have no path parameters and can use exact string matching
    static_routes: AHashMap<String, Route>,

    /// Radix tree for dynamic routes (e.g., /users/{id}, /posts/{id}/comments)
    /// Only used when static lookup fails
    dynamic_router: MatchRouter<Route>,
}

impl MethodRouter {
    fn new() -> Self {
        MethodRouter {
            static_routes: AHashMap::new(),
            dynamic_router: MatchRouter::new(),
        }
    }
}

pub struct Router {
    get: MethodRouter,
    post: MethodRouter,
    put: MethodRouter,
    patch: MethodRouter,
    delete: MethodRouter,
    head: MethodRouter,
    options: MethodRouter,
}

impl Router {
    pub fn new() -> Self {
        Router {
            get: MethodRouter::new(),
            post: MethodRouter::new(),
            put: MethodRouter::new(),
            patch: MethodRouter::new(),
            delete: MethodRouter::new(),
            head: MethodRouter::new(),
            options: MethodRouter::new(),
        }
    }

    pub fn register(
        &mut self,
        method: &str,
        path: &str,
        handler_id: usize,
        handler: Py<PyAny>,
    ) -> PyResult<()> {
        let method_router = match method {
            "GET" => &mut self.get,
            "POST" => &mut self.post,
            "PUT" => &mut self.put,
            "PATCH" => &mut self.patch,
            "DELETE" => &mut self.delete,
            "HEAD" => &mut self.head,
            "OPTIONS" => &mut self.options,
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Unsupported method: {}",
                    method
                )))
            }
        };

        // Elysia-style optimization: Separate static routes from dynamic routes
        // Static routes use O(1) HashMap lookup, dynamic routes use radix tree
        if is_static_path(path) {
            // Static route: store in HashMap for O(1) lookup
            let route = Route {
                handler,
                handler_id,
            };
            method_router.static_routes.insert(path.to_string(), route);
        } else {
            // Dynamic route: convert path and store in radix tree
            let converted_path = convert_path(path);
            let route = Route {
                handler,
                handler_id,
            };
            method_router
                .dynamic_router
                .insert(&converted_path, route)
                .map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!(
                        "Failed to register route: {}",
                        e
                    ))
                })?;
        }

        Ok(())
    }

    /// Find a route handler for the given method and path.
    ///
    /// Uses Elysia-style two-phase lookup:
    /// 1. O(1) HashMap lookup for static routes (no path params)
    /// 2. Radix tree lookup for dynamic routes (with path params)
    ///
    /// Returns RouteMatch enum that distinguishes between static and dynamic routes,
    /// allowing the handler to skip path param processing for static routes.
    ///
    /// This optimization significantly improves performance for APIs
    /// where most routes are static (e.g., /users, /health, /api/items).
    ///
    /// OPTIMIZATION: #[inline] on hot path - called on every request
    #[inline]
    pub fn find(&self, method: &str, path: &str) -> Option<RouteMatch<'_>> {
        let method_router = match method {
            "GET" => &self.get,
            "POST" => &self.post,
            "PUT" => &self.put,
            "PATCH" => &self.patch,
            "DELETE" => &self.delete,
            "HEAD" => &self.head,
            "OPTIONS" => &self.options,
            _ => return None,
        };

        // Phase 1: O(1) HashMap lookup for static routes
        // This is the fast path - most API routes are static
        if let Some(route) = method_router.static_routes.get(path) {
            // Static routes have no path parameters - no HashMap allocation needed
            return Some(RouteMatch::Static(route));
        }

        // Phase 2: Radix tree lookup for dynamic routes
        // Only reached for paths with parameters like /users/{id}
        match method_router.dynamic_router.at(path) {
            Ok(Match { value, params }) => {
                let mut path_params = AHashMap::new();
                for (key, value) in params.iter() {
                    path_params.insert(key.to_string(), value.to_string());
                }
                Some(RouteMatch::Dynamic(value, path_params))
            }
            Err(_) => None,
        }
    }

    /// Find all HTTP methods that have handlers registered for the given path.
    /// Used for automatic OPTIONS handling to return the Allow header.
    ///
    /// Returns a vector of method names (e.g., ["GET", "POST", "PUT"]).
    /// Always includes "OPTIONS" if any methods are found (for automatic OPTIONS support).
    pub fn find_all_methods(&self, path: &str) -> Vec<String> {
        let mut methods = Vec::new();

        // Check each method router to see if it has a handler for this path
        // Need to check both static routes (HashMap) and dynamic routes (radix tree)
        let method_routers = [
            ("GET", &self.get),
            ("POST", &self.post),
            ("PUT", &self.put),
            ("PATCH", &self.patch),
            ("DELETE", &self.delete),
            ("HEAD", &self.head),
            ("OPTIONS", &self.options),
        ];

        for (method_name, method_router) in method_routers.iter() {
            // Check static routes first (O(1))
            if method_router.static_routes.contains_key(path) {
                methods.push(method_name.to_string());
            }
            // Then check dynamic routes (radix tree)
            else if method_router.dynamic_router.at(path).is_ok() {
                methods.push(method_name.to_string());
            }
        }

        // If we found any methods and OPTIONS is not explicitly registered, add it
        // (automatic OPTIONS support for all routes)
        if !methods.is_empty() && !methods.contains(&"OPTIONS".to_string()) {
            methods.push("OPTIONS".to_string());
        }

        methods
    }
}

/// Parse query string into key-value pairs
/// OPTIMIZATION: #[inline] on hot path - called on requests with query strings
#[inline]
pub fn parse_query_string(query: &str) -> AHashMap<String, String> {
    let mut params = AHashMap::new();
    if query.is_empty() {
        return params;
    }

    #[inline(always)]
    fn decode_component(value: &str) -> Cow<'_, str> {
        if value.as_bytes().iter().any(|&b| b == b'%' || b == b'+') {
            urlencoding::decode(value).unwrap_or_else(|_| Cow::Borrowed(value))
        } else {
            Cow::Borrowed(value)
        }
    }

    for pair in query.split('&') {
        if let Some(eq_pos) = pair.find('=') {
            let key = &pair[..eq_pos];
            let value = &pair[eq_pos + 1..];
            if !key.is_empty() {
                params.insert(
                    decode_component(key).into_owned(),
                    decode_component(value).into_owned(),
                );
            }
        } else if !pair.is_empty() {
            params.insert(decode_component(pair).into_owned(), String::new());
        }
    }

    params
}
