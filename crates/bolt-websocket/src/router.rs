//! WebSocket router for matching paths to handlers

use ahash::AHashMap;
use pyo3::prelude::*;

/// WebSocket route definition
pub struct WebSocketRoute {
    #[allow(dead_code)] // Stored for debugging/introspection
    pub path: String,
    pub handler_id: usize,
    pub handler: Py<PyAny>,
    /// Pre-compiled parameter injector (passed from Python at registration time)
    pub injector: Option<Py<PyAny>>,
}

/// WebSocket router for matching paths to handlers
pub struct WebSocketRouter {
    /// Static routes (no path params) - O(1) lookup
    static_routes: AHashMap<String, WebSocketRoute>,
    /// Dynamic routes (with path params) - radix tree
    dynamic_router: matchit::Router<WebSocketRoute>,
    /// Track if we have any dynamic routes
    has_dynamic_routes: bool,
    /// Each route path as it was written, for Actix registration
    paths: Vec<String>,
}

impl Default for WebSocketRouter {
    fn default() -> Self {
        Self::new()
    }
}

impl WebSocketRouter {
    pub fn new() -> Self {
        WebSocketRouter {
            static_routes: AHashMap::new(),
            dynamic_router: matchit::Router::new(),
            has_dynamic_routes: false,
            paths: Vec::new(),
        }
    }

    pub fn register(
        &mut self,
        path: &str,
        handler_id: usize,
        handler: Py<PyAny>,
        injector: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        let route = WebSocketRoute {
            path: path.to_string(),
            handler_id,
            handler,
            injector,
        };

        // Actix matches the decoded request path, so it gets the path as it
        // was written.
        if !self.paths.iter().any(|known| known == path) {
            self.paths.push(path.to_string());
        }
        // `find` gets the request path with its `%XX` escapes.
        let path = &*bolt_core::router::encode_route_path(path);

        if !path.contains('{') {
            self.static_routes.insert(path.to_string(), route);
        } else {
            let converted = bolt_core::router::convert_path(path);
            self.dynamic_router.insert(&converted, route).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "Failed to register WebSocket route: {}",
                    e
                ))
            })?;
            self.has_dynamic_routes = true;
        }

        Ok(())
    }

    /// Find the route for `path` and its path parameters.
    ///
    /// The values are URL-decoded with the same code as for HTTP routes, so
    /// `/ws/rooms/hello%20world` gives `hello world`. The server and the test
    /// backend both use this lookup.
    pub fn find(&self, path: &str) -> Option<(&WebSocketRoute, AHashMap<String, String>)> {
        // O(1) static route lookup first
        if let Some(route) = self.static_routes.get(path) {
            return Some((route, AHashMap::new()));
        }

        // Radix tree lookup only if we have dynamic routes
        if self.has_dynamic_routes
            && let Ok(matched) = self.dynamic_router.at(path)
        {
            let mut params = AHashMap::new();
            for (key, value) in matched.params.iter() {
                params.insert(key.to_string(), value.to_string());
            }
            bolt_core::router::decode_path_params(&mut params);
            return Some((matched.value, params));
        }

        None
    }

    #[allow(dead_code)]
    pub fn is_empty(&self) -> bool {
        self.static_routes.is_empty() && !self.has_dynamic_routes
    }

    /// Get all registered WebSocket paths for Actix route registration.
    /// Each path is as it was written, with no escape added.
    pub fn get_all_paths(&self) -> Vec<String> {
        self.paths.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A router with the handler id of each route at its index.
    fn router_with(paths: &[&str]) -> WebSocketRouter {
        Python::initialize();
        Python::attach(|py| {
            let mut router = WebSocketRouter::new();
            for (handler_id, path) in paths.iter().enumerate() {
                router.register(path, handler_id, py.None(), None).unwrap();
            }
            router
        })
    }

    #[test]
    fn a_route_path_matches_the_escaped_request_path() {
        let router = router_with(&["/ws/\u{fc} b", "/ws/\u{fc} b/{room}", "/ws/plain"]);
        let found = |path: &str| {
            router.find(path).map(|(route, params)| {
                let mut params: Vec<(String, String)> = params.into_iter().collect();
                params.sort();
                (route.handler_id, route.path.clone(), params)
            })
        };
        assert_eq!(
            found("/ws/%C3%BC%20b"),
            Some((0, "/ws/\u{fc} b".to_string(), vec![]))
        );
        assert_eq!(
            found("/ws/%C3%BC%20b/x%20y"),
            Some((
                1,
                "/ws/\u{fc} b/{room}".to_string(),
                vec![("room".to_string(), "x y".to_string())]
            ))
        );
        assert_eq!(
            found("/ws/plain"),
            Some((2, "/ws/plain".to_string(), vec![]))
        );
        assert_eq!(found("/ws/\u{fc} b"), None);
    }

    #[test]
    fn actix_gets_each_route_path_one_time_as_it_was_written() {
        let router = router_with(&["/ws/\u{fc}", "/ws/{room}/a b", "/ws/\u{fc}"]);
        assert_eq!(router.get_all_paths(), ["/ws/\u{fc}", "/ws/{room}/a b"]);
    }
}
