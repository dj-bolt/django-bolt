//! The Origin check of a WebSocket handshake.

use actix_web::http::header::HeaderValue;
use bolt_core::metadata::CorsConfig;
use bolt_core::state::WebSocketOriginPolicy;

/// Check the Origin header of a WebSocket handshake against the global CORS config.
/// `runbolt` and the test client both call this function.
///
/// Security behavior:
/// - No Origin header: allow for non-browser clients; absence does not prove same-origin.
/// - An Origin value that is not visible ASCII: deny.
/// - No CORS config: deny all cross-origin requests (fail-secure).
/// - Else: allow only the origins that the CORS config allows.
pub fn validate_origin(origin: Option<&HeaderValue>, cors_config: Option<&CorsConfig>) -> bool {
    let origin = match origin {
        Some(v) => match v.to_str() {
            Ok(s) => s,
            Err(_) => {
                eprintln!("[django-bolt] WebSocket: Invalid Origin header encoding");
                return false;
            }
        },
        None => return true,
    };

    if let Some(cors_config) = cors_config {
        return is_origin_allowed(origin, cors_config);
    }

    // SECURITY: No CORS configured = deny all cross-origin requests
    // This is a fail-secure default (unlike the old allow-all default)
    eprintln!(
        "[django-bolt] WebSocket: Rejecting cross-origin request from '{}' - no CORS configured. \
        Set CORS_ALLOWED_ORIGINS in Django settings to allow WebSocket connections.",
        origin
    );
    false
}

/// Validate an explicitly selected ASGI-mount Origin policy.
///
/// The default delegates to Bolt's global policy; the dedicated allowlist
/// requires a header and exact match. Only an explicit delegate choice leaves
/// validation to the mounted ASGI app.
pub fn validate_mount_origin(
    origin: Option<&HeaderValue>,
    cors_config: Option<&CorsConfig>,
    policy: &WebSocketOriginPolicy,
) -> bool {
    match policy {
        WebSocketOriginPolicy::Inherit { allow_missing } => {
            if origin.is_none() && !*allow_missing {
                return false;
            }
            validate_origin(origin, cors_config)
        }
        WebSocketOriginPolicy::Delegate => true,
        WebSocketOriginPolicy::Allowlist {
            origins,
            allow_missing,
        } => match origin {
            None => *allow_missing,
            Some(value) => value.to_str().ok().is_some_and(|v| origins.contains(v)),
        },
        WebSocketOriginPolicy::Any { allow_missing } => match origin {
            None => *allow_missing,
            Some(value) => value.to_str().ok().is_some_and(valid_web_origin),
        },
        // The callback is evaluated separately before the HTTP 101.
        WebSocketOriginPolicy::Custom { allow_missing, .. } => match origin {
            None => *allow_missing,
            Some(value) => value.to_str().is_ok(),
        },
    }
}

/// Only a syntactically valid HTTP(S) web Origin is permitted by `any`.
/// This does not authenticate a client and deliberately excludes "null".
fn valid_web_origin(value: &str) -> bool {
    if value.is_empty() || value == "null" || value.bytes().any(|b| b.is_ascii_whitespace()) {
        return false;
    }
    let Ok(uri) = value.parse::<actix_web::http::Uri>() else {
        return false;
    };
    if !matches!(uri.scheme_str(), Some("http" | "https")) {
        return false;
    }
    let Some(authority) = uri.authority() else {
        return false;
    };
    let host = authority.host();
    if host.is_empty() || authority.as_str().contains('@') || authority.as_str().ends_with(':') {
        return false;
    }
    if host.starts_with('[') {
        if host
            .strip_prefix('[')
            .and_then(|s| s.strip_suffix(']'))
            .and_then(|s| s.parse::<std::net::Ipv6Addr>().ok())
            .is_none()
        {
            return false;
        }
    } else if !host
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
    {
        return false;
    }
    let suffix = &authority.as_str()[host.len()..];
    if !suffix.is_empty()
        && suffix
            .strip_prefix(':')
            .and_then(|s| s.parse::<u16>().ok())
            .is_none_or(|port| port == 0)
    {
        return false;
    }
    // Compare slices without allocating a formatted Origin on each handshake.
    value
        .strip_prefix("http://")
        .or_else(|| value.strip_prefix("https://"))
        == Some(authority.as_str())
}

/// Check if an origin is allowed by the CORS configuration.
/// The config holds the compiled `CORS_ALLOWED_ORIGIN_REGEXES` too.
fn is_origin_allowed(origin: &str, cors_config: &CorsConfig) -> bool {
    // Allow all origins if configured
    if cors_config.allow_all_origins {
        return true;
    }

    // O(1) exact match using HashSet
    if cors_config.origin_set.contains(origin) {
        return true;
    }

    cors_config
        .compiled_origin_regexes
        .iter()
        .any(|re| re.is_match(origin))
}

#[cfg(test)]
mod tests {
    use super::{is_origin_allowed, validate_mount_origin, validate_origin};
    use actix_web::http::header::HeaderValue;
    use bolt_core::metadata::CorsConfig;
    use bolt_core::state::WebSocketOriginPolicy;
    use std::collections::HashSet;

    fn cors(origins: &[&str], regexes: &[&str], allow_all: bool) -> CorsConfig {
        CorsConfig::from_django_settings(
            origins.iter().map(|o| o.to_string()).collect(),
            regexes.iter().map(|r| r.to_string()).collect(),
            allow_all,
            false,
            None,
            None,
            None,
            None,
        )
    }

    fn origin(value: &str) -> HeaderValue {
        HeaderValue::from_str(value).unwrap()
    }

    #[test]
    fn validate_origin_allows_a_handshake_with_no_origin_header() {
        assert!(validate_origin(None, None));
        assert!(validate_origin(
            None,
            Some(&cors(&["https://a.example"], &[], false))
        ));
    }

    #[test]
    fn validate_origin_denies_every_origin_with_no_cors_config() {
        assert!(!validate_origin(Some(&origin("https://a.example")), None));
    }

    #[test]
    fn validate_origin_checks_the_origin_against_the_cors_config() {
        let config = cors(&["https://a.example"], &[], false);
        assert!(validate_origin(
            Some(&origin("https://a.example")),
            Some(&config)
        ));
        assert!(!validate_origin(
            Some(&origin("https://b.example")),
            Some(&config)
        ));
    }

    #[test]
    fn validate_origin_denies_an_origin_that_is_not_visible_ascii() {
        // "https://\u{e9}.example" in UTF-8. It is a valid header value, but not a str.
        let value = HeaderValue::from_bytes(b"https://\xc3\xa9.example").unwrap();
        assert!(!validate_origin(Some(&value), Some(&cors(&[], &[], true))));
    }

    #[test]
    fn is_origin_allowed_takes_exact_origins_only() {
        let config = cors(&["https://a.example"], &[], false);
        assert!(is_origin_allowed("https://a.example", &config));
        assert!(!is_origin_allowed("https://a.example.evil", &config));
        assert!(!is_origin_allowed("http://a.example", &config));
    }

    #[test]
    fn is_origin_allowed_allows_every_origin_with_allow_all() {
        let config = cors(&[], &[], true);
        assert!(is_origin_allowed("https://any.example", &config));
    }

    #[test]
    fn mount_origin_inherit_keeps_the_global_security_policy() {
        let configured = cors(&["https://allowed.example"], &[], false);
        let policy = WebSocketOriginPolicy::Inherit {
            allow_missing: true,
        };
        assert!(validate_mount_origin(
            Some(&origin("https://allowed.example")),
            Some(&configured),
            &policy
        ));
        assert!(!validate_mount_origin(
            Some(&origin("https://evil.example")),
            Some(&configured),
            &policy
        ));
        assert!(validate_mount_origin(None, Some(&configured), &policy));
        assert!(!validate_mount_origin(
            Some(&origin("https://allowed.example")),
            None,
            &policy
        ));
    }

    #[test]
    fn mount_origin_explicit_allowlist_requires_exact_origin() {
        let origins = HashSet::from(["https://mount.example".to_string()]);
        let policy = WebSocketOriginPolicy::Allowlist {
            origins,
            allow_missing: false,
        };
        let global = cors(&["https://other.example"], &[], false);
        assert!(validate_mount_origin(
            Some(&origin("https://mount.example")),
            Some(&global),
            &policy
        ));
        for rejected in [
            "http://mount.example",
            "https://other.example",
            "https://mount.example.evil",
            "null",
        ] {
            assert!(!validate_mount_origin(
                Some(&origin(rejected)),
                Some(&global),
                &policy
            ));
        }
        assert!(!validate_mount_origin(None, Some(&global), &policy));
        // An explicit mount allowlist still wins over globally permissive CORS.
        let globally_open = cors(&[], &[], true);
        assert!(!validate_mount_origin(
            Some(&origin("https://evil.example")),
            Some(&globally_open),
            &policy
        ));
    }

    #[test]
    fn any_origin_is_explicit_and_rejects_null_or_missing() {
        let policy = WebSocketOriginPolicy::Any {
            allow_missing: false,
        };
        assert!(validate_mount_origin(
            Some(&origin("https://other.example")),
            None,
            &policy
        ));
        assert!(!validate_mount_origin(Some(&origin("null")), None, &policy));
        assert!(!validate_mount_origin(
            Some(&origin("https://other.example/path")),
            None,
            &policy
        ));
        assert!(!validate_mount_origin(None, None, &policy));
    }

    #[test]
    fn any_rejects_malformed_authorities() {
        for value in [
            "https://example.com:0",
            "https://example.com:65536",
            "https://example.com:",
            "https://[not-ipv6]",
            "https://user@example.com",
            "https://example.com?",
            "https://example.com#",
            "https://example.com,https://evil.example",
        ] {
            assert!(!super::valid_web_origin(value), "accepted {value}");
        }
    }

    #[test]
    fn any_accepts_serialized_web_origins() {
        for value in [
            "http://localhost",
            "https://EXAMPLE.com",
            "https://example.com:443",
            "http://127.0.0.1:8000",
            "https://[::1]",
            "http://[2001:db8::1]:65535",
        ] {
            assert!(super::valid_web_origin(value), "rejected {value}");
        }
    }

    proptest::proptest! {
        #[test]
        fn any_checks_port_range(port in 0u32..100_000) {
            let value = format!("https://example.com:{port}");
            proptest::prop_assert_eq!(super::valid_web_origin(&value), (1..=65535).contains(&port));
        }
    }

    #[test]
    fn mount_origin_delegate_is_an_explicit_opt_out() {
        let policy = WebSocketOriginPolicy::Delegate;
        assert!(validate_mount_origin(
            Some(&origin("https://arbitrary.example")),
            None,
            &policy
        ));
        assert!(validate_mount_origin(None, None, &policy));
    }

    #[test]
    fn is_origin_allowed_matches_the_config_regexes() {
        let config = cors(&[], &[r"^https://[a-z]+\.trusted\.example$"], false);
        assert!(is_origin_allowed("https://app.trusted.example", &config));
        assert!(!is_origin_allowed("https://app.other.example", &config));
    }
}
