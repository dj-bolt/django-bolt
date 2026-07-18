//! Cookie serialization using the `cookie` crate for RFC 6265 compliance.
//!
//! Uses the battle-tested `cookie` crate instead of manual string building
//! to ensure proper validation, escaping, and security.

use actix_web::cookie::{time, Cookie, SameSite};

use crate::response_meta::CookieData;

/// Format a CookieData into a Set-Cookie header value.
///
/// Uses the `cookie` crate for RFC 6265 compliant serialization:
/// - Validates cookie names (rejects invalid characters)
/// - Properly escapes cookie values
/// - Rejects control characters that could enable header injection
///
/// Returns None if the cookie name or value is invalid, with a warning logged.
#[inline]
pub fn format_cookie(c: &CookieData) -> Option<String> {
    // Validate cookie name - reject if it contains invalid characters
    // RFC 6265: cookie-name = token (no CTLs, separators, or whitespace)
    if !is_valid_cookie_name(&c.name) {
        eprintln!(
            "[django-bolt] WARNING: Invalid cookie name '{}' - contains invalid characters",
            c.name
        );
        return None;
    }

    // Validate cookie value - reject control characters that could enable injection
    if contains_control_chars(&c.value) {
        eprintln!(
            "[django-bolt] WARNING: Cookie '{}' value contains control characters - rejected for security",
            c.name
        );
        return None;
    }

    // Build cookie using the cookie crate
    let mut cookie = Cookie::build(&c.name, &c.value).path(&c.path);

    // Max-Age (validate non-negative)
    if let Some(max_age) = c.max_age {
        if max_age < 0 {
            eprintln!(
                "[django-bolt] WARNING: Cookie '{}' has negative max_age ({}) - using 0",
                c.name, max_age
            );
            cookie = cookie.max_age(time::Duration::ZERO);
        } else {
            cookie = cookie.max_age(time::Duration::seconds(max_age));
        }
    }

    // Expires is handled manually below (cookie crate expects OffsetDateTime,
    // but we receive a pre-formatted string from Python)

    // Domain
    if let Some(ref domain) = c.domain {
        cookie = cookie.domain(domain);
    }

    // Secure
    if c.secure {
        cookie = cookie.secure(true);
    }

    // HttpOnly
    if c.httponly {
        cookie = cookie.http_only(true);
    }

    // SameSite
    if let Some(ref samesite) = c.samesite {
        let ss = match samesite.to_lowercase().as_str() {
            "strict" => SameSite::Strict,
            "lax" => SameSite::Lax,
            "none" => SameSite::None,
            _ => SameSite::Lax, // Default to Lax for invalid values
        };
        cookie = cookie.same_site(ss);
    }

    let built = cookie.finish();
    let mut result = built.to_string();

    // Append Expires manually if provided (cookie crate requires OffsetDateTime)
    if let Some(ref expires) = c.expires {
        result.push_str("; Expires=");
        result.push_str(expires);
    }

    Some(result)
}

/// Validate cookie name per RFC 6265.
/// Cookie names must be tokens: no CTLs, separators, or whitespace.
#[inline]
fn is_valid_cookie_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }

    // RFC 6265 token characters (subset of ASCII)
    // Disallowed: CTLs (0-31, 127), separators: ()<>@,;:\"/[]?={} SP HT
    name.bytes().all(|b| {
        b > 32
            && b < 127
            && !matches!(
                b,
                b'(' | b')'
                    | b'<'
                    | b'>'
                    | b'@'
                    | b','
                    | b';'
                    | b':'
                    | b'\\'
                    | b'"'
                    | b'/'
                    | b'['
                    | b']'
                    | b'?'
                    | b'='
                    | b'{'
                    | b'}'
            )
    })
}

/// Check if a string contains control characters that could enable header injection.
#[inline]
fn contains_control_chars(value: &str) -> bool {
    value.bytes().any(|b| b < 32 || b == 127)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_simple_cookie() {
        let c = CookieData {
            name: "session".to_string(),
            value: "abc123".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("session=abc123"));
        assert!(result.contains("Path=/"));
    }

    #[test]
    fn test_full_cookie() {
        let c = CookieData {
            name: "auth".to_string(),
            value: "token".to_string(),
            path: "/app".to_string(),
            max_age: Some(3600),
            expires: Some("Thu, 01 Jan 2025 00:00:00 GMT".to_string()),
            domain: Some("example.com".to_string()),
            secure: true,
            httponly: true,
            samesite: Some("Strict".to_string()),
        };
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("auth=token"));
        assert!(result.contains("Path=/app"));
        assert!(result.contains("Max-Age=3600"));
        assert!(result.contains("Domain=example.com"));
        assert!(result.contains("Secure"));
        assert!(result.contains("HttpOnly"));
        assert!(result.contains("SameSite=Strict"));
        assert!(result.contains("Expires=Thu, 01 Jan 2025 00:00:00 GMT"));
    }

    #[test]
    fn test_value_with_spaces() {
        let c = CookieData {
            name: "data".to_string(),
            value: "hello world".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        // Cookie crate handles quoting/encoding
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("data="));
    }

    #[test]
    fn test_invalid_cookie_name_rejected() {
        let c = CookieData {
            name: "session; Path=/evil".to_string(), // Injection attempt
            value: "xyz".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        assert!(format_cookie(&c).is_none());
    }

    #[test]
    fn test_control_chars_rejected() {
        let c = CookieData {
            name: "session".to_string(),
            value: "value\r\nSet-Cookie: evil=1".to_string(), // Header injection attempt
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        assert!(format_cookie(&c).is_none());
    }

    #[test]
    fn test_empty_name_rejected() {
        let c = CookieData {
            name: "".to_string(),
            value: "value".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        assert!(format_cookie(&c).is_none());
    }

    #[test]
    fn test_negative_max_age_handled() {
        let c = CookieData {
            name: "session".to_string(),
            value: "abc".to_string(),
            path: "/".to_string(),
            max_age: Some(-100), // Invalid negative
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        let result = format_cookie(&c).unwrap();
        // Should use 0 instead of negative
        assert!(result.contains("Max-Age=0"));
    }

    #[test]
    fn test_samesite_values() {
        for (input, expected) in [("Strict", "Strict"), ("Lax", "Lax"), ("None", "None")] {
            let c = CookieData {
                name: "test".to_string(),
                value: "val".to_string(),
                path: "/".to_string(),
                max_age: None,
                expires: None,
                domain: None,
                secure: false,
                httponly: false,
                samesite: Some(input.to_string()),
            };
            let result = format_cookie(&c).unwrap();
            assert!(
                result.contains(&format!("SameSite={}", expected)),
                "Expected SameSite={} in {}",
                expected,
                result
            );
        }
    }
}
