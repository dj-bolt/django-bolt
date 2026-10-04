use ahash::{AHashMap, AHashSet};
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
    #[allow(dead_code)]
    pub fn path_params(self) -> Option<AHashMap<String, String>> {
        match self {
            RouteMatch::Static(_) => None,
            RouteMatch::Dynamic(_, params) => Some(params),
        }
    }

    /// Decompose into the route reference (with the router's lifetime — 'static
    /// for the global router) and owned path params. Lets the request handler
    /// defer the handler `clone_ref` into the main GIL block instead of paying
    /// an extra `Python::attach` per request.
    #[inline]
    pub fn into_parts(self) -> (&'a Route, Option<AHashMap<String, String>>) {
        match self {
            RouteMatch::Static(r) => (r, None),
            RouteMatch::Dynamic(r, params) => (r, Some(params)),
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
    /// Per-route bound async dispatch: `partial(api._dispatch, handler, handler_id=id)`.
    /// The hot path calls it with just `(request,)` — no per-request 3-tuple
    /// of argument conversions and no handler clone_ref (PyO3 call-args
    /// machinery measured at ~12% of native samples on the sync path).
    pub dispatch: Py<PyAny>,
    /// Per-route bound sync dispatch (same shape as `dispatch`).
    pub dispatch_sync: Py<PyAny>,
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
    query: MethodRouter,
}

impl Default for Router {
    fn default() -> Self {
        Self::new()
    }
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
            query: MethodRouter::new(),
        }
    }

    pub fn register(
        &mut self,
        method: &str,
        path: &str,
        handler_id: usize,
        handler: Py<PyAny>,
        dispatch: Py<PyAny>,
        dispatch_sync: Py<PyAny>,
    ) -> PyResult<()> {
        let method_router = match method {
            "GET" => &mut self.get,
            "POST" => &mut self.post,
            "PUT" => &mut self.put,
            "PATCH" => &mut self.patch,
            "DELETE" => &mut self.delete,
            "HEAD" => &mut self.head,
            "OPTIONS" => &mut self.options,
            "QUERY" => &mut self.query,
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
                dispatch,
                dispatch_sync,
                handler_id,
            };
            method_router.static_routes.insert(path.to_string(), route);
        } else {
            // Dynamic route: convert path and store in radix tree
            let converted_path = convert_path(path);
            let route = Route {
                handler,
                dispatch,
                dispatch_sync,
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
            "QUERY" => &self.query,
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
            ("QUERY", &self.query),
        ];

        for (method_name, method_router) in method_routers.iter() {
            // Check static routes first (O(1)), then dynamic routes (radix tree).
            if method_router.static_routes.contains_key(path)
                || method_router.dynamic_router.at(path).is_ok()
            {
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

/// URL-decode path parameters in place (`/items/hello%20world` yields `hello world`).
/// A value that does not decode to UTF-8 stays as it is.
/// A `+` in a path is a literal `+`, so only a `%` starts a decode.
#[inline]
pub fn decode_path_params(params: &mut AHashMap<String, String>) {
    for v in params.values_mut() {
        if memchr::memchr(b'%', v.as_bytes()).is_some() {
            if let Ok(Cow::Owned(s)) = urlencoding::decode(v) {
                *v = s;
            }
        }
    }
}

/// Query keys and values, decoded as Django `QueryDict` decodes them.
/// A key or a value borrows from the query string when it needs no decode.
pub type QueryParams<'a> = AHashMap<Cow<'a, str>, Cow<'a, str>>;

/// Each sequence query key of a route, with its values in the order of the query.
pub type QuerySequences<'a> = Vec<(Cow<'a, str>, Vec<Cow<'a, str>>)>;

/// Decode one query key or value, as Django `QueryDict` does (`application/x-www-form-urlencoded`).
/// A `+` becomes a space and `%XX` becomes a byte, so `%2B` gives a literal `+`.
/// Bytes that are not UTF-8 become U+FFFD, as in Python `unquote`.
/// Do not use this for path params: a `+` in a path is a literal `+`.
#[inline]
pub fn decode_query_component(value: &str) -> Cow<'_, str> {
    // Keys and values are short. An inline scan costs less than a `memchr` call.
    if value.bytes().any(|byte| byte == b'%' || byte == b'+') {
        decode_escaped_query_component(value)
    } else {
        Cow::Borrowed(value)
    }
}

/// Decode a component that has a `%` or a `+`, in one pass.
/// It is out of line, so that the check in `decode_query_component` inlines.
#[inline(never)]
fn decode_escaped_query_component(value: &str) -> Cow<'_, str> {
    let bytes = value.as_bytes();
    if !bytes.contains(&b'%') {
        return Cow::Owned(value.replace('+', " "));
    }
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                decoded.push(b' ');
                i += 1;
            }
            b'%' => match (hex_digit(bytes.get(i + 1)), hex_digit(bytes.get(i + 2))) {
                (Some(high), Some(low)) => {
                    decoded.push((high << 4) | low);
                    i += 3;
                }
                // Not an escape: the `%` stays, as in Python `unquote`.
                _ => {
                    decoded.push(b'%');
                    i += 1;
                }
            },
            byte => {
                decoded.push(byte);
                i += 1;
            }
        }
    }
    match String::from_utf8(decoded) {
        Ok(text) => Cow::Owned(text),
        Err(error) => Cow::Owned(String::from_utf8_lossy(error.as_bytes()).into_owned()),
    }
}

#[inline]
fn hex_digit(byte: Option<&u8>) -> Option<u8> {
    match *byte? {
        digit @ b'0'..=b'9' => Some(digit - b'0'),
        letter @ b'a'..=b'f' => Some(letter - b'a' + 10),
        letter @ b'A'..=b'F' => Some(letter - b'A' + 10),
        _ => None,
    }
}

/// Split a query string into decoded (key, value) pairs, in their order.
/// The rules are those of Python `parse_qsl` for Django `QueryDict`, and of the
/// WHATWG `application/x-www-form-urlencoded` parser. An empty pair is skipped.
/// A pair with no `=` has an empty value. An empty key stays.
#[inline]
pub fn query_pairs(query: &str) -> impl Iterator<Item = (Cow<'_, str>, Cow<'_, str>)> {
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            (decode_query_component(key), decode_query_component(value))
        })
}

/// Parse a query string into a map. A repeated key keeps its last value, as `QueryDict[key]` does.
#[inline]
pub fn parse_query_string(query: &str) -> QueryParams<'_> {
    if query.is_empty() {
        return QueryParams::new();
    }
    // One pair for each `&`, but at most 16. Thus a query of many `&` cannot make a large map.
    let pairs = query.bytes().filter(|&byte| byte == b'&').count() + 1;
    let mut params = QueryParams::with_capacity(pairs.min(16));
    params.extend(query_pairs(query));
    params
}

/// Collect each value of the query keys in `keys`, in the order of the query.
///
/// `parse_query_string` keeps the last value of a repeated key. A sequence
/// parameter (`list[int]`, `set[str]`) takes all of them, so the route lists
/// its sequence keys and this second pass reads only those. It splits and
/// decodes the pairs with `query_pairs`, so a value decodes as a scalar does.
/// A key that is not in the query is not in the result. A value that has no
/// escape borrows from the query, so it does not allocate.
pub fn collect_query_sequences<'a>(query: &'a str, keys: &AHashSet<String>) -> QuerySequences<'a> {
    let mut sequences: QuerySequences<'a> = Vec::new();
    for (key, value) in query_pairs(query) {
        if !keys.contains(key.as_ref()) {
            continue;
        }
        match sequences.iter_mut().find(|(name, _)| *name == key) {
            Some((_, values)) => values.push(value),
            None => sequences.push((key, vec![value])),
        }
    }
    sequences
}

#[cfg(test)]
mod tests {
    use super::*;

    fn owned(sequences: QuerySequences<'_>) -> Vec<(String, Vec<String>)> {
        sequences
            .into_iter()
            .map(|(name, values)| {
                (
                    name.into_owned(),
                    values.into_iter().map(Cow::into_owned).collect(),
                )
            })
            .collect()
    }

    #[test]
    fn collect_query_sequences_keeps_each_value_of_a_listed_key_in_order() {
        let keys: AHashSet<String> = ["tag".to_string(), "id".to_string()].into_iter().collect();
        let sequences = owned(collect_query_sequences(
            "tag=b&page=2&tag=a%20c&id=1&tag=b&empty",
            &keys,
        ));
        assert_eq!(
            sequences,
            vec![
                (
                    "tag".to_string(),
                    vec!["b".to_string(), "a c".to_string(), "b".to_string()]
                ),
                ("id".to_string(), vec!["1".to_string()]),
            ]
        );
    }

    #[test]
    fn collect_query_sequences_reads_a_key_with_no_value_as_empty() {
        let keys: AHashSet<String> = ["tag".to_string()].into_iter().collect();
        assert_eq!(
            owned(collect_query_sequences("tag&tag=", &keys)),
            vec![("tag".to_string(), vec![String::new(), String::new()])]
        );
        assert!(collect_query_sequences("other=1", &keys).is_empty());
    }

    #[test]
    fn collect_query_sequences_decodes_plus_as_a_space_as_the_scalar_parser_does() {
        let keys: AHashSet<String> = ["tag".to_string()].into_iter().collect();
        assert_eq!(
            owned(collect_query_sequences("tag=a+b&t%61g=c%2Bd", &keys)),
            vec![(
                "tag".to_string(),
                vec!["a b".to_string(), "c+d".to_string()]
            )]
        );
    }

    #[test]
    fn test_decode_path_params() {
        let mut params = AHashMap::new();
        params.insert("space".to_string(), "hello%20world".to_string());
        params.insert("slash".to_string(), "a%2Fb".to_string());
        params.insert("newline".to_string(), "%0A".to_string());
        params.insert("plain".to_string(), "abc".to_string());
        params.insert("plus".to_string(), "a+b".to_string());
        // Not valid UTF-8 after decoding: the value stays as it is.
        params.insert("bad".to_string(), "%FF".to_string());

        decode_path_params(&mut params);

        assert_eq!(params["space"], "hello world");
        assert_eq!(params["slash"], "a/b");
        assert_eq!(params["newline"], "\n");
        assert_eq!(params["plain"], "abc");
        assert_eq!(params["plus"], "a+b");
        assert_eq!(params["bad"], "%FF");
    }

    #[test]
    fn test_parse_query_string_decodes_plus_as_space() {
        let params =
            parse_query_string("q=hello+world&tag=a%2Bb&mixed=a+b%20c&my+key=1&flag+on&plain=x");

        assert_eq!(params["q"], "hello world");
        assert_eq!(params["tag"], "a+b");
        assert_eq!(params["mixed"], "a b c");
        assert_eq!(params["my key"], "1");
        assert_eq!(params["flag on"], "");
        assert_eq!(params["plain"], "x");
    }

    #[test]
    fn test_parse_query_string_replaces_bytes_that_are_not_utf8() {
        // Django `QueryDict` decodes with errors="replace".
        let params = parse_query_string("bad=a+%FF&cut=%E2%82&ok=%C3%A9&%FF=key");

        assert_eq!(params["bad"], "a \u{FFFD}");
        assert_eq!(params["cut"], "\u{FFFD}");
        assert_eq!(params["ok"], "é");
        assert_eq!(params["\u{FFFD}"], "key");
    }

    #[test]
    fn test_parse_query_string_keeps_empty_keys_and_skips_empty_pairs() {
        let params = parse_query_string("=v&&flag&a=1&a=2&%zz=%&");

        assert_eq!(params[""], "v");
        assert_eq!(params["flag"], "");
        assert_eq!(params["a"], "2");
        assert_eq!(params["%zz"], "%");
        assert_eq!(params.len(), 4);
    }

    #[test]
    fn test_parse_query_string_borrows_what_needs_no_decode() {
        let params = parse_query_string("page=2&q=a+b");

        let (key, value) = params.get_key_value("page").unwrap();
        assert!(matches!(key, Cow::Borrowed(_)));
        assert!(matches!(value, Cow::Borrowed(_)));
        assert!(matches!(params["q"], Cow::Owned(_)));
    }

    #[test]
    fn test_query_pairs_keep_their_order() {
        let pairs: Vec<(String, String)> = query_pairs("b=2&a=1&flag&b=3")
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();

        let expected = [("b", "2"), ("a", "1"), ("flag", ""), ("b", "3")];
        assert_eq!(pairs, expected.map(|(k, v)| (k.to_string(), v.to_string())));
    }

    /// The parser gives the pairs of the WHATWG `application/x-www-form-urlencoded`
    /// parser (in `serde_urlencoded`), which gives what Django `QueryDict` gives.
    #[test]
    fn test_parse_query_string_matches_the_whatwg_parser() {
        let parts: Vec<&str> =
            "a Z 0 é + % %2 %2B %20 %3D %26 %C3%A9 %FF %E2%82 %ED%A0%80 %zz = & &&"
                .split(' ')
                .collect();
        let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _ in 0..20_000 {
            let count = next() % 12;
            let query: String = (0..count)
                .map(|_| parts[(next() % parts.len() as u64) as usize])
                .collect();

            let expected: AHashMap<String, String> =
                serde_urlencoded::from_str::<Vec<(String, String)>>(&query)
                    .unwrap()
                    .into_iter()
                    .collect();
            let actual: AHashMap<String, String> = parse_query_string(&query)
                .into_iter()
                .map(|(key, value)| (key.to_string(), value.to_string()))
                .collect();
            assert_eq!(actual, expected, "query {query:?}");
        }
    }
}
