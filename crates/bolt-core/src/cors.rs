//! Shared CORS handling functions used by both production middleware and test_state.rs
//!
//! This module provides the core CORS functionality:
//! - `add_cors_headers_with_config` - Add CORS headers using CorsConfig (for middleware)
//! - `add_preflight_headers_with_config` - Add preflight headers using CorsConfig (for middleware)
//! - `add_preflight_vary` - Add the `Vary` names of a preflight (for middleware)

use actix_web::http::header::{
    ACCESS_CONTROL_ALLOW_CREDENTIALS, ACCESS_CONTROL_ALLOW_HEADERS, ACCESS_CONTROL_ALLOW_METHODS,
    ACCESS_CONTROL_ALLOW_ORIGIN, ACCESS_CONTROL_EXPOSE_HEADERS, ACCESS_CONTROL_MAX_AGE, HeaderMap,
    HeaderValue, VARY,
};

use crate::metadata::CorsConfig;

/// Add CORS headers to a HeaderMap using CorsConfig (supports regex patterns)
/// This is the core function used by CorsMiddleware
/// Returns true if the origin is allowed, false otherwise
///
/// `*` with no credentials is the same value for each request, so it needs no
/// `Vary: Origin`. Bolt sends it also when the request has no Origin. Each
/// other config gives an answer that depends on the Origin. Thus its responses
/// get `Vary: Origin`, also for a refused origin and for no Origin. A cache
/// then keeps one response for each origin (Fetch standard, "CORS protocol and
/// HTTP caches").
pub fn add_cors_headers_with_config(
    headers: &mut HeaderMap,
    request_origin: Option<&str>,
    cors_config: &CorsConfig,
    global_cors_config: Option<&CorsConfig>,
) -> bool {
    if cors_config.allow_all_origins && !cors_config.credentials {
        headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        if let Some(ref cached_val) = cors_config.expose_headers_header {
            headers.insert(ACCESS_CONTROL_EXPOSE_HEADERS, cached_val.clone());
        }
        return true;
    }

    add_vary(headers, "Origin");

    let Some(req_origin) = request_origin else {
        return false;
    };

    // A wildcard with credentials is not valid CORS, so Bolt reflects each
    // origin. A config with its own origins or patterns is the complete allow
    // list. A config with neither uses the origins and regexes of the global
    // config. Check the pattern strings: a pattern that does not compile must
    // not widen the route to the global list.
    if !cors_config.allow_all_origins {
        let allow_list =
            if !cors_config.origin_set.is_empty() || !cors_config.origin_regexes.is_empty() {
                cors_config
            } else if let Some(global_config) = global_cors_config {
                global_config
            } else {
                return false;
            };

        // O(1) exact match, then the compiled regexes
        let allowed = allow_list.origin_set.contains(req_origin)
            || allow_list
                .compiled_origin_regexes
                .iter()
                .any(|re| re.is_match(req_origin));
        if !allowed {
            return false;
        }
    }

    // Reflect the request origin
    if let Ok(val) = HeaderValue::from_str(req_origin) {
        headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, val);
    }

    if cors_config.credentials {
        headers.insert(
            ACCESS_CONTROL_ALLOW_CREDENTIALS,
            HeaderValue::from_static("true"),
        );
    }

    if let Some(ref cached_val) = cors_config.expose_headers_header {
        headers.insert(ACCESS_CONTROL_EXPOSE_HEADERS, cached_val.clone());
    }

    true
}

/// Add the `Vary` names of a preflight. The answer depends on the requested
/// method, because the route of that method gives the config. Add them also
/// when the origin is refused.
pub fn add_preflight_vary(headers: &mut HeaderMap) {
    add_vary(headers, "Access-Control-Request-Method");
    add_vary(headers, "Access-Control-Request-Headers");
}

/// Add CORS preflight headers to a HeaderMap using CorsConfig
/// This is the core function used by CorsMiddleware for OPTIONS requests
pub fn add_preflight_headers_with_config(headers: &mut HeaderMap, cors_config: &CorsConfig) {
    if let Some(ref cached_val) = cors_config.methods_header {
        headers.insert(ACCESS_CONTROL_ALLOW_METHODS, cached_val.clone());
    }

    if let Some(ref cached_val) = cors_config.headers_header {
        headers.insert(ACCESS_CONTROL_ALLOW_HEADERS, cached_val.clone());
    }

    if let Some(ref cached_val) = cors_config.max_age_header {
        headers.insert(ACCESS_CONTROL_MAX_AGE, cached_val.clone());
    }
}

/// Add `name` to `Vary`, unless a `Vary` value names it (in any letter case)
/// or is `*`. The values of the handler stay. The new value is static, so it
/// needs no allocation.
fn add_vary(headers: &mut HeaderMap, name: &'static str) {
    let named = headers.get_all(VARY).any(|value| {
        value.as_bytes().split(|&byte| byte == b',').any(|token| {
            let token = token.trim_ascii();
            token == b"*" || token.eq_ignore_ascii_case(name.as_bytes())
        })
    });
    if !named {
        headers.append(VARY, HeaderValue::from_static(name));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use actix_web::http::header::HeaderName;

    fn cors(origins: &[&str], regexes: &[&str], allow_all: bool, credentials: bool) -> CorsConfig {
        CorsConfig::from_django_settings(
            origins.iter().map(|o| o.to_string()).collect(),
            regexes.iter().map(|r| r.to_string()).collect(),
            allow_all,
            credentials,
            None,
            None,
            Some(vec!["X-Total".to_string()]),
            None,
        )
        .unwrap()
    }

    fn header<'a>(headers: &'a HeaderMap, name: &HeaderName) -> Option<&'a str> {
        headers.get(name).map(|v| v.to_str().unwrap())
    }

    /// Run the check of `config` with no global config.
    fn check(config: &CorsConfig, origin: Option<&str>) -> (bool, HeaderMap) {
        let mut headers = HeaderMap::new();
        let allowed = add_cors_headers_with_config(&mut headers, origin, config, None);
        (allowed, headers)
    }

    #[test]
    fn allowed_origin_is_reflected_with_vary_and_expose_headers() {
        let config = cors(&["https://a.example"], &[], false, false);
        let (allowed, headers) = check(&config, Some("https://a.example"));
        assert!(allowed);
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_ORIGIN),
            Some("https://a.example")
        );
        assert_eq!(header(&headers, &VARY), Some("Origin"));
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_EXPOSE_HEADERS),
            Some("X-Total")
        );
        assert!(headers.get(ACCESS_CONTROL_ALLOW_CREDENTIALS).is_none());
    }

    #[test]
    fn allowed_origin_gets_the_credentials_header_when_configured() {
        let config = cors(&["https://a.example"], &[], false, true);
        let (allowed, headers) = check(&config, Some("https://a.example"));
        assert!(allowed);
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_CREDENTIALS),
            Some("true")
        );
    }

    #[test]
    fn other_origin_or_no_origin_gets_only_vary_origin() {
        // The answer depends on the Origin, so a cache must key the response on it.
        let config = cors(
            &["https://a.example"],
            &[r"^https://r\.example$"],
            false,
            true,
        );
        for origin in [
            Some("https://b.example"),
            Some("https://a.example.evil"),
            None,
        ] {
            let (allowed, headers) = check(&config, origin);
            assert!(!allowed, "{origin:?}");
            assert_eq!(headers.len(), 1, "{origin:?}");
            assert_eq!(header(&headers, &VARY), Some("Origin"), "{origin:?}");
        }
    }

    #[test]
    fn allow_all_without_credentials_sends_a_wildcard() {
        let config = cors(&[], &[], true, false);
        for origin in [Some("https://any.example"), None] {
            let (allowed, headers) = check(&config, origin);
            assert!(allowed);
            assert_eq!(header(&headers, &ACCESS_CONTROL_ALLOW_ORIGIN), Some("*"));
            assert_eq!(
                header(&headers, &ACCESS_CONTROL_EXPOSE_HEADERS),
                Some("X-Total")
            );
            assert!(headers.get(ACCESS_CONTROL_ALLOW_CREDENTIALS).is_none());
            assert!(headers.get(VARY).is_none());
        }
    }

    #[test]
    fn allow_all_with_credentials_reflects_the_origin() {
        // A wildcard with credentials is not valid CORS.
        let config = cors(&[], &[], true, true);
        let (allowed, headers) = check(&config, Some("https://any.example"));
        assert!(allowed);
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_ORIGIN),
            Some("https://any.example")
        );
        assert_eq!(header(&headers, &VARY), Some("Origin"));
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_CREDENTIALS),
            Some("true")
        );
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_EXPOSE_HEADERS),
            Some("X-Total")
        );

        let (allowed, headers) = check(&config, None);
        assert!(!allowed);
        assert_eq!(headers.len(), 1);
        assert_eq!(header(&headers, &VARY), Some("Origin"));
    }

    #[test]
    fn route_with_no_origins_uses_the_global_origins() {
        let route = cors(&[], &[], false, false);
        let global = cors(&["https://g.example"], &[], false, false);
        let mut headers = HeaderMap::new();
        assert!(add_cors_headers_with_config(
            &mut headers,
            Some("https://g.example"),
            &route,
            Some(&global),
        ));
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_ORIGIN),
            Some("https://g.example")
        );

        // With no global config, a route with no origins allows nothing.
        let (allowed, headers) = check(&route, Some("https://g.example"));
        assert!(!allowed);
        assert!(headers.get(ACCESS_CONTROL_ALLOW_ORIGIN).is_none());
    }

    #[test]
    fn route_origins_replace_the_global_origins() {
        let route = cors(&["https://r.example"], &[], false, false);
        let global = cors(&["https://g.example"], &[], false, false);
        let mut headers = HeaderMap::new();
        assert!(!add_cors_headers_with_config(
            &mut headers,
            Some("https://g.example"),
            &route,
            Some(&global),
        ));
        assert!(headers.get(ACCESS_CONTROL_ALLOW_ORIGIN).is_none());
    }

    #[test]
    fn vary_origin_joins_the_vary_of_the_handler_once() {
        // The existing values stay, in any letter case and in one or more fields.
        let config = cors(&["https://a.example"], &[], false, false);
        for (existing, expected) in [
            (&["Cookie"][..], &["Cookie", "Origin"][..]),
            (&["accept-encoding, ORIGIN"], &["accept-encoding, ORIGIN"]),
            (
                &["Accept-Encoding", " origin "],
                &["Accept-Encoding", " origin "],
            ),
            (&["*"], &["*"]),
            (&["Cookie,*"], &["Cookie,*"]),
            (&["Origins"], &["Origins", "Origin"]),
        ] {
            for origin in [Some("https://a.example"), Some("https://b.example"), None] {
                let mut headers = HeaderMap::new();
                for value in existing {
                    headers.append(VARY, HeaderValue::from_static(value));
                }
                add_cors_headers_with_config(&mut headers, origin, &config, None);
                let vary: Vec<_> = headers.get_all(VARY).collect();
                assert_eq!(vary, expected, "{existing:?} {origin:?}");
            }
        }
    }

    /// Run the check of a route config against a global config.
    fn check_route(route: &CorsConfig, global: &CorsConfig, origin: &str) -> bool {
        add_cors_headers_with_config(&mut HeaderMap::new(), Some(origin), route, Some(global))
    }

    #[test]
    fn route_regexes_replace_the_global_regexes() {
        let global = cors(&[], &[r"^https://[a-z]+\.global\.example$"], false, false);
        let route = cors(
            &["https://r.example"],
            &[r"^https://[a-z]+\.route\.example$"],
            false,
            false,
        );
        assert!(check_route(&route, &global, "https://app.route.example"));
        assert!(!check_route(&route, &global, "https://app.global.example"));
    }

    #[test]
    fn route_origins_replace_the_global_regexes() {
        // A route with its own origins is the complete allow list.
        let global = cors(
            &["https://g.example"],
            &[r"^https://[a-z]+\.global\.example$"],
            false,
            false,
        );
        let route = cors(&["https://r.example"], &[], false, true);
        assert!(check_route(&route, &global, "https://r.example"));
        assert!(!check_route(&route, &global, "https://app.global.example"));
    }

    #[test]
    fn route_regexes_replace_the_global_origins() {
        let global = cors(&["https://g.example"], &[], false, false);
        let route = cors(&[], &[r"^https://[a-z]+\.route\.example$"], false, false);
        assert!(check_route(&route, &global, "https://app.route.example"));
        assert!(!check_route(&route, &global, "https://g.example"));
    }

    #[test]
    fn route_with_patterns_and_no_compiled_regex_does_not_take_the_global_list() {
        // The parsers refuse a pattern that does not compile. A config with
        // patterns and no compiled regex must still allow no origin.
        let route = CorsConfig {
            origin_regexes: vec![r"^https://(?!evil)[a-z]+\.route\.example$".to_string()],
            credentials: true,
            ..Default::default()
        };
        let global = cors(
            &["https://g.example"],
            &[r"^https://[a-z]+\.global\.example$"],
            false,
            false,
        );
        assert!(!check_route(&route, &global, "https://g.example"));
        assert!(!check_route(&route, &global, "https://app.global.example"));
    }

    #[test]
    fn route_with_no_origins_uses_the_global_regexes() {
        let route = cors(&[], &[], false, false);
        let global = cors(
            &["https://g.example"],
            &[r"^https://[a-z]+\.global\.example$"],
            false,
            false,
        );
        assert!(check_route(&route, &global, "https://app.global.example"));
        assert!(!check_route(&route, &global, "https://app.other.example"));
    }

    #[test]
    fn preflight_headers_come_from_the_config() {
        let config = CorsConfig::from_django_settings(
            vec!["https://a.example".to_string()],
            vec![],
            false,
            false,
            Some(vec!["GET".to_string(), "POST".to_string()]),
            Some(vec!["X-Token".to_string()]),
            None,
            Some(600),
        )
        .unwrap();
        let mut headers = HeaderMap::new();
        add_preflight_headers_with_config(&mut headers, &config);
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_METHODS),
            Some("GET, POST")
        );
        assert_eq!(
            header(&headers, &ACCESS_CONTROL_ALLOW_HEADERS),
            Some("X-Token")
        );
        assert_eq!(header(&headers, &ACCESS_CONTROL_MAX_AGE), Some("600"));
    }

    #[test]
    fn preflight_vary_is_added_once() {
        let mut headers = HeaderMap::new();
        headers.append(
            VARY,
            HeaderValue::from_static("access-control-request-method"),
        );
        add_preflight_vary(&mut headers);
        add_preflight_vary(&mut headers);
        let vary: Vec<_> = headers.get_all(VARY).collect();
        assert_eq!(
            vary,
            [
                "access-control-request-method",
                "Access-Control-Request-Headers"
            ]
        );
    }
}
