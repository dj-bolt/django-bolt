use crate::middleware::auth::{authenticate, AuthBackend, AuthContext};
use crate::permissions::{evaluate_guards, Guard, GuardResult};
/// Shared validation logic used by both production handler and test handler
/// All functions marked #[inline(always)] for zero-cost abstraction
use ahash::AHashMap;

/// Parse HTTP cookies from Cookie header
/// Returns HashMap of cookie name -> cookie value
///
/// # Performance
/// - Zero allocations if no cookies
/// - Pre-allocated capacity for 8 cookies (typical case)
/// - Inlined for zero-cost abstraction
#[inline(always)]
pub fn parse_cookies_inline(cookie_header: Option<&str>) -> AHashMap<String, String> {
    let mut cookies: AHashMap<String, String> = AHashMap::with_capacity(8);

    if let Some(raw_cookie) = cookie_header {
        for pair in raw_cookie.split(';') {
            let part = pair.trim();
            if let Some(eq) = part.find('=') {
                let (k, v) = part.split_at(eq);
                let v2 = &v[1..]; // Skip '=' character
                if !k.is_empty() {
                    cookies.insert(k.to_string(), v2.to_string());
                }
            }
        }
    }

    cookies
}

/// Result of authentication and guard evaluation
#[derive(Debug)]
pub enum AuthGuardResult {
    /// Authentication and guards passed
    Allow(Option<AuthContext>),
    /// Authentication required (401)
    Unauthorized,
    /// Permission denied (403)
    Forbidden,
}

/// Whether an HTTP method is "safe" (RFC 9110 §9.2.1) — read-only and thus
/// exempt from CSRF checks.
#[inline(always)]
fn is_safe_method(method: &str) -> bool {
    matches!(method, "GET" | "HEAD" | "OPTIONS" | "TRACE")
}

/// Cross-site request forgery check for cookie-authenticated routes.
///
/// Bolt endpoints bypass Django's `CsrfViewMiddleware`, so a JWT read from a
/// cookie has no CSRF protection unless the framework adds it. When any JWT
/// backend on the route sources its token from a cookie, an unsafe-method
/// request must prove it did not originate cross-site. This is pure header
/// inspection (no state, no body): the browser-set `Sec-Fetch-Site` is
/// authoritative when present, falling back to an `Origin`/`Referer` host
/// comparison against the request `Host` for older clients.
///
/// Returns `true` when the request is allowed to proceed.
#[inline]
pub fn passes_cookie_csrf(method: &str, headers: &AHashMap<String, String>) -> bool {
    if is_safe_method(method) {
        return true;
    }

    // Fetch metadata sends `Sec-Fetch-Site` on every request in modern
    // browsers; `same-origin`/`none` are trusted, everything else (cross-site,
    // same-site subdomain) is rejected for a state-changing cookie request.
    if let Some(site) = headers.get("sec-fetch-site") {
        return matches!(site.as_str(), "same-origin" | "none");
    }

    // Fallback: compare the Origin (or Referer) host to the request Host.
    let host = match headers.get("host") {
        Some(h) => h.as_str(),
        None => return false, // No Host to compare against — refuse.
    };
    if let Some(origin) = headers.get("origin") {
        return origin_host_matches(origin, host);
    }
    if let Some(referer) = headers.get("referer") {
        return origin_host_matches(referer, host);
    }
    // No Sec-Fetch-Site, no Origin, no Referer on an unsafe request: a
    // browser would have sent at least one, so this is not a same-origin
    // browser form post. Refuse.
    false
}

/// Compare the host authority of a URL (`https://example.com:443/path`)
/// against a request `Host` header value.
fn origin_host_matches(url: &str, host: &str) -> bool {
    // Strip scheme.
    let after_scheme = url.split("://").nth(1).unwrap_or(url);
    // Authority ends at the first '/'.
    let authority = after_scheme.split('/').next().unwrap_or(after_scheme);
    authority == host
}

/// Validate authentication and evaluate guards
/// This combines auth + guards into a single reusable flow
///
/// # Parameters
/// - `headers`: Request headers (lowercase keys)
/// - `auth_backends`: Configured auth backends for this route
/// - `guards`: Configured guards for this route
///
/// # Returns
/// - `Allow(auth_ctx)`: Authentication and guards passed
/// - `Unauthorized`: Authentication required (401)
/// - `Forbidden`: Permission denied (403)
///
/// # Performance
/// - Zero allocations if no auth configured
/// - Inlined for zero-cost abstraction
#[inline(always)]
pub fn validate_auth_and_guards(
    headers: &AHashMap<String, String>,
    auth_backends: &[AuthBackend],
    guards: &[Guard],
) -> AuthGuardResult {
    // Skip work if no auth or guards configured
    if auth_backends.is_empty() && guards.is_empty() {
        return AuthGuardResult::Allow(None);
    }

    // Authenticate if backends configured
    let auth_ctx = if !auth_backends.is_empty() {
        authenticate(headers, auth_backends)
    } else {
        None
    };

    // Evaluate guards if configured
    if !guards.is_empty() {
        match evaluate_guards(guards, auth_ctx.as_ref()) {
            GuardResult::Allow => {
                // Guards passed
            }
            GuardResult::Unauthorized => {
                return AuthGuardResult::Unauthorized;
            }
            GuardResult::Forbidden => {
                return AuthGuardResult::Forbidden;
            }
        }
    }

    AuthGuardResult::Allow(auth_ctx)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_cookies_empty() {
        let cookies = parse_cookies_inline(None);
        assert_eq!(cookies.len(), 0);
    }

    #[test]
    fn test_parse_cookies_single() {
        let cookies = parse_cookies_inline(Some("session=abc123"));
        assert_eq!(cookies.get("session"), Some(&"abc123".to_string()));
    }

    #[test]
    fn test_parse_cookies_multiple() {
        let cookies = parse_cookies_inline(Some("session=abc123; user=john; token=xyz"));
        assert_eq!(cookies.get("session"), Some(&"abc123".to_string()));
        assert_eq!(cookies.get("user"), Some(&"john".to_string()));
        assert_eq!(cookies.get("token"), Some(&"xyz".to_string()));
    }

    #[test]
    fn test_parse_cookies_with_spaces() {
        let cookies = parse_cookies_inline(Some("session=abc123;   user=john  ;token=xyz"));
        assert_eq!(cookies.get("session"), Some(&"abc123".to_string()));
        assert_eq!(cookies.get("user"), Some(&"john".to_string()));
        assert_eq!(cookies.get("token"), Some(&"xyz".to_string()));
    }

    #[test]
    fn test_validate_auth_no_config() {
        let headers = AHashMap::new();
        let result = validate_auth_and_guards(&headers, &[], &[]);
        matches!(result, AuthGuardResult::Allow(None));
    }

    fn headers_with(pairs: &[(&str, &str)]) -> AHashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn csrf_safe_methods_always_pass() {
        let headers = headers_with(&[("sec-fetch-site", "cross-site")]);
        assert!(passes_cookie_csrf("GET", &headers));
        assert!(passes_cookie_csrf("HEAD", &headers));
        assert!(passes_cookie_csrf("OPTIONS", &headers));
    }

    #[test]
    fn csrf_sec_fetch_site_is_authoritative() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "same-origin")])
        ));
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "none")])
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "cross-site")])
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "same-site")])
        ));
    }

    #[test]
    fn csrf_origin_host_fallback() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "https://example.com")])
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "https://evil.com")])
        ));
    }

    #[test]
    fn csrf_unsafe_request_without_any_origin_signal_is_refused() {
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com")])
        ));
    }
}
