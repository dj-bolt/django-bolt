//! WebSocket router for matching paths to handlers

use ahash::AHashMap;
use bolt_core::router;
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
    /// The path as written of each dynamic route, by its matchit path
    dynamic_paths: router::DynamicPaths,
    /// Each route path as it was written, for Actix registration
    paths: Vec<String>,
}

/// The method that a registration error gives for a WebSocket route.
const METHOD: &str = "WEBSOCKET";

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
            dynamic_paths: router::DynamicPaths::default(),
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

        // `find` gets the request path with its `%XX` escapes.
        let encoded = &*router::encode_route_path(path);

        if !encoded.contains('{') {
            if self.static_routes.contains_key(encoded) {
                return Err(router::already_registered(METHOD, path));
            }
            self.static_routes.insert(encoded.to_string(), route);
        } else {
            router::insert_dynamic(
                &mut self.dynamic_router,
                &mut self.dynamic_paths,
                METHOD,
                path,
                encoded,
                route,
            )?;
            self.has_dynamic_routes = true;
        }
        // Actix matches the decoded request path, so it gets the path as it
        // was written.
        self.paths.push(path.to_string());

        Ok(())
    }

    /// Find the route for `path` and its path parameters.
    ///
    /// The values are URL-decoded with the same code as for HTTP routes, so
    /// `/ws/rooms/hello%20world` gives `hello world`. The server and the test
    /// backend both use this lookup. `path` is the request path in the form of
    /// `normalize_request_path`.
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
    fn a_request_path_matches_for_each_form_of_an_escape() {
        let router = router_with(&["/ws/\u{fc}", "/ws/a:b/{room}"]);
        let found = |path: &str| {
            let path = bolt_core::router::normalize_request_path(path);
            router
                .find(&path)
                .map(|(route, params)| (route.handler_id, params.get("room").cloned()))
        };
        assert_eq!(found("/ws/%c3%bc"), Some((0, None)));
        assert_eq!(found("/ws/%C3%bC"), Some((0, None)));
        assert_eq!(
            found("/ws/%61%3ab/x%2fy"),
            Some((1, Some("x/y".to_string())))
        );
        // An escaped `/` is data of one segment.
        assert_eq!(found("/ws/a:b%2Fx"), None);
    }

    #[test]
    fn actix_gets_each_route_path_as_it_was_written() {
        let router = router_with(&["/ws/\u{fc}", "/ws/{room}/a b"]);
        assert_eq!(router.get_all_paths(), ["/ws/\u{fc}", "/ws/{room}/a b"]);
    }

    /// The error that registering `path` after the `earlier` routes gives.
    /// The earlier routes stay as they were.
    fn register_error(earlier: &[&str], path: &str) -> String {
        let mut router = router_with(earlier);
        let error = Python::attach(|py| {
            router
                .register(path, earlier.len(), py.None(), None)
                .unwrap_err()
                .to_string()
        });
        assert_eq!(router.get_all_paths(), earlier);
        for (handler_id, earlier_path) in earlier.iter().enumerate() {
            let request_path =
                bolt_core::router::encode_route_path(&earlier_path.replace("{room}", "r"))
                    .into_owned();
            let (route, _) = router.find(&request_path).unwrap();
            assert_eq!(route.handler_id, handler_id);
        }
        error
    }

    #[test]
    fn a_second_route_at_a_registered_path_does_not_replace_the_first() {
        for path in ["/live", "/live/{room}", "/\u{fc}"] {
            assert_eq!(
                register_error(&[path], path),
                format!(
                    "ValueError: Failed to register route: WEBSOCKET {path} is already registered"
                )
            );
        }
        // The same path after the escapes that a request path gets.
        assert_eq!(
            register_error(&["/a b"], "/a%20b"),
            "ValueError: Failed to register route: WEBSOCKET /a%20b is already registered"
        );
    }

    #[test]
    fn a_conflict_names_both_websocket_routes() {
        assert_eq!(
            register_error(&["/live/{room}"], "/live/{p:path}"),
            "ValueError: Failed to register route: WEBSOCKET /live/{p:path} conflicts with \
             WEBSOCKET /live/{room}. Keep one of the two routes, or add a fixed segment \
             before the parameter in one path."
        );
    }

    #[test]
    fn a_malformed_websocket_path_error_names_the_route() {
        assert_eq!(
            register_error(&[], "/w/{p:path}/x"),
            "ValueError: Failed to register route: WEBSOCKET /w/{p:path}/x has a {name:path} \
             parameter before the end of the path. Move it to the end."
        );
        assert_eq!(
            register_error(&[], "/w/{room"),
            "ValueError: Failed to register route: WEBSOCKET /w/{room has a parameter with no \
             name, or a brace with no pair. Write each parameter as {name} or {name:type}."
        );
    }
}
