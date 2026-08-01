//! Shared CORS handling functions used by both production middleware and test_state.rs
//!
//! This module provides the core CORS functionality:
//! - `add_cors_headers_with_config` - Add CORS headers using CorsConfig (for middleware)
//! - `add_preflight_headers_with_config` - Add preflight headers using CorsConfig (for middleware)

use actix_web::http::header::{
    HeaderMap, HeaderValue, ACCESS_CONTROL_ALLOW_CREDENTIALS, ACCESS_CONTROL_ALLOW_HEADERS,
    ACCESS_CONTROL_ALLOW_METHODS, ACCESS_CONTROL_ALLOW_ORIGIN, ACCESS_CONTROL_EXPOSE_HEADERS,
    ACCESS_CONTROL_MAX_AGE, VARY,
};

use crate::metadata::CorsConfig;
use crate::state::AppState;

/// Whether an Origin header value denotes the same origin as the request's
/// Host header (browsers ALWAYS send Origin on WebSocket handshakes, even for
/// same-origin connections, so equality with Host means "not cross-origin").
///
/// Compares the origin's authority (scheme stripped) case-insensitively
/// against the Host value, tolerating an explicit default port on either side
/// (`:80` for http, `:443` for https).
pub fn origin_matches_host(origin: &str, host: &str) -> bool {
    let (authority, default_port) = if let Some(rest) = origin.strip_prefix("https://") {
        (rest, ":443")
    } else if let Some(rest) = origin.strip_prefix("http://") {
        (rest, ":80")
    } else {
        // Opaque origins ("null") or exotic schemes are never same-origin.
        return false;
    };
    if authority.is_empty() {
        return false;
    }
    let normalize = |value: &str| -> String {
        let trimmed = value.strip_suffix(default_port).unwrap_or(value);
        trimmed.to_ascii_lowercase()
    };
    normalize(authority) == normalize(host)
}

/// Add CORS headers to a HeaderMap using CorsConfig (supports regex patterns)
/// This is the core function used by CorsMiddleware
/// Returns true if CORS headers were added (origin was allowed), false otherwise
pub fn add_cors_headers_with_config(
    headers: &mut HeaderMap,
    request_origin: Option<&str>,
    cors_config: &CorsConfig,
    state: &AppState,
) -> bool {
    // Check if CORS_ALLOW_ALL_ORIGINS is True with credentials (invalid per spec)
    if cors_config.allow_all_origins && cors_config.credentials {
        // Per CORS spec, wildcard + credentials is invalid. Reflect the request origin instead.
        if let Some(req_origin) = request_origin {
            if let Ok(val) = HeaderValue::from_str(req_origin) {
                headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, val);
            }
            headers.append(VARY, HeaderValue::from_static("Origin"));
            headers.insert(
                ACCESS_CONTROL_ALLOW_CREDENTIALS,
                HeaderValue::from_static("true"),
            );

            if let Some(ref cached_val) = cors_config.expose_headers_header {
                headers.insert(ACCESS_CONTROL_EXPOSE_HEADERS, cached_val.clone());
            }
            return true;
        }
        return false;
    }

    // Handle allow_all_origins (wildcard) without credentials
    if cors_config.allow_all_origins {
        headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        if let Some(ref cached_val) = cors_config.expose_headers_header {
            headers.insert(ACCESS_CONTROL_EXPOSE_HEADERS, cached_val.clone());
        }
        return true;
    }

    // Skip work if no Origin header present
    let req_origin = match request_origin {
        Some(o) => o,
        None => return false,
    };

    // Use route-level origin_set first (O(1) lookup), then fall back to global
    let origin_set = if !cors_config.origin_set.is_empty() {
        &cors_config.origin_set
    } else if let Some(ref global_config) = state.global_cors_config {
        &global_config.origin_set
    } else {
        return false;
    };

    // Check exact match using O(1) hash set lookup
    let exact_match = origin_set.contains(req_origin);

    // Check regex match using route-level regexes, then global regexes
    let regex_match = if !cors_config.compiled_origin_regexes.is_empty() {
        cors_config
            .compiled_origin_regexes
            .iter()
            .any(|re| re.is_match(req_origin))
    } else {
        !state.cors_origin_regexes.is_empty()
            && state
                .cors_origin_regexes
                .iter()
                .any(|re| re.is_match(req_origin))
    };

    if !exact_match && !regex_match {
        return false;
    }

    // Reflect the request origin
    if let Ok(val) = HeaderValue::from_str(req_origin) {
        headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, val);
    }
    headers.append(VARY, HeaderValue::from_static("Origin"));

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

    // Add Vary headers for preflight requests (check for duplicates)
    let has_preflight_vary = headers
        .get(VARY)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.contains("Access-Control-Request-Method"))
        .unwrap_or(false);

    if !has_preflight_vary {
        headers.append(
            VARY,
            HeaderValue::from_static(
                "Access-Control-Request-Method, Access-Control-Request-Headers",
            ),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::origin_matches_host;

    #[test]
    fn same_origin_with_explicit_port() {
        assert!(origin_matches_host(
            "http://127.0.0.1:8000",
            "127.0.0.1:8000"
        ));
        assert!(origin_matches_host(
            "https://app.example.com:8443",
            "app.example.com:8443"
        ));
    }

    #[test]
    fn same_origin_with_default_ports() {
        assert!(origin_matches_host("http://example.com", "example.com"));
        assert!(origin_matches_host("http://example.com:80", "example.com"));
        assert!(origin_matches_host(
            "https://example.com",
            "example.com:443"
        ));
    }

    #[test]
    fn host_comparison_is_case_insensitive() {
        assert!(origin_matches_host(
            "http://Example.COM:8000",
            "example.com:8000"
        ));
    }

    #[test]
    fn cross_origin_is_rejected() {
        assert!(!origin_matches_host("https://evil.example", "example.com"));
        assert!(!origin_matches_host(
            "http://example.com:9000",
            "example.com:8000"
        ));
        // http vs https on the same host is a different origin (default ports differ)
        assert!(!origin_matches_host(
            "https://example.com",
            "example.com:80"
        ));
    }

    #[test]
    fn opaque_and_exotic_origins_are_rejected() {
        assert!(!origin_matches_host("null", "example.com"));
        assert!(!origin_matches_host("file://example.com", "example.com"));
        assert!(!origin_matches_host("http://", "example.com"));
    }
}
