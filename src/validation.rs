use crate::middleware::auth::{authenticate, AuthBackend, AuthContext};
use crate::permissions::{evaluate_guards, Guard, GuardResult};
use actix_web::http::uri::Authority;
/// Shared validation logic used by both production handler and test handler
/// All functions marked #[inline(always)] for zero-cost abstraction
use ahash::AHashMap;
use std::str::FromStr;

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

/// Whether an unsafe request must be rejected by the cookie-CSRF gate.
///
/// `credential_requires_csrf` comes from the authentication result, so the
/// gate follows the credential that actually succeeded. A stale incidental
/// cookie does not block a request authenticated through another backend.
///
/// Returns `true` when the request must be rejected (403).
#[inline]
pub fn cookie_csrf_blocks(
    method: &str,
    headers: &AHashMap<String, String>,
    credential_requires_csrf: bool,
    scheme: &str,
) -> bool {
    if !credential_requires_csrf || is_safe_method(method) {
        return false;
    }
    !passes_cookie_csrf(method, headers, scheme)
}

/// Cross-site request forgery check for cookie-authenticated routes.
///
/// Bolt endpoints bypass Django's `CsrfViewMiddleware`, so a JWT read from a
/// cookie has no CSRF protection unless the framework adds it. When a
/// request carries a CSRF-enforced auth cookie (see `cookie_csrf_blocks`),
/// an unsafe-method request must prove it did not originate cross-site.
/// This is pure header inspection (no state, no body): the browser-set
/// `Sec-Fetch-Site` is authoritative when present, falling back to a full
/// `Origin`/`Referer` origin comparison (scheme + authority) against the
/// request's effective scheme and `Host` for older clients.
///
/// `scheme` is the request's effective scheme (`http`/`https`, honoring
/// `X-Forwarded-Proto` behind a proxy) — HTTP and HTTPS are different
/// origins, so `Origin: http://example.com` must not authorize a
/// state-changing HTTPS request to `example.com`.
///
/// Returns `true` when the request is allowed to proceed.
#[inline]
pub fn passes_cookie_csrf(method: &str, headers: &AHashMap<String, String>, scheme: &str) -> bool {
    if is_safe_method(method) {
        return true;
    }

    // Fetch metadata sends `Sec-Fetch-Site` on every request in modern
    // browsers; `same-origin`/`none` are trusted, everything else (cross-site,
    // same-site subdomain) is rejected for a state-changing cookie request.
    if let Some(site) = headers.get("sec-fetch-site") {
        return matches!(site.as_str(), "same-origin" | "none");
    }

    // Fallback: compare the Origin (or Referer) origin to the request's
    // effective scheme + Host.
    let host = match headers.get("host") {
        Some(h) => h.as_str(),
        None => return false, // No Host to compare against — refuse.
    };
    if let Some(origin) = headers.get("origin") {
        return origin_matches(origin, scheme, host);
    }
    if let Some(referer) = headers.get("referer") {
        return origin_matches(referer, scheme, host);
    }
    // No Sec-Fetch-Site, no Origin, no Referer on an unsafe request: a
    // browser would have sent at least one, so this is not a same-origin
    // browser form post. Refuse.
    false
}

/// Compare a URL's origin (`https://example.com:443/path`) against the
/// request's effective scheme and `Host` header value.
fn origin_matches(url: &str, scheme: &str, host: &str) -> bool {
    // `Origin: null` (sandboxed frames, some redirects) and schemeless
    // values carry no provable origin — refuse.
    let (url_scheme, rest) = match url.split_once("://") {
        Some(parts) => parts,
        None => return false,
    };
    if !url_scheme.eq_ignore_ascii_case(scheme)
        || !matches!(url_scheme.to_ascii_lowercase().as_str(), "http" | "https")
    {
        return false;
    }

    // Origin has no path; Referer does. Query/fragment delimiters also end
    // the authority for defensive parsing of unusual clients.
    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let url_authority = &rest[..authority_end];
    if url_authority.contains('@') {
        return false;
    }

    let url_authority = match Authority::from_str(url_authority) {
        Ok(authority) => authority,
        Err(_) => return false,
    };
    let request_authority = match Authority::from_str(host) {
        Ok(authority) => authority,
        Err(_) => return false,
    };

    let default_port = if url_scheme.eq_ignore_ascii_case("https") {
        443
    } else {
        80
    };
    let url_port = url_authority.port_u16().unwrap_or(default_port);
    let request_port = request_authority.port_u16().unwrap_or(default_port);

    url_authority
        .host()
        .eq_ignore_ascii_case(request_authority.host())
        && url_port == request_port
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
        assert!(passes_cookie_csrf("GET", &headers, "https"));
        assert!(passes_cookie_csrf("HEAD", &headers, "https"));
        assert!(passes_cookie_csrf("OPTIONS", &headers, "https"));
    }

    #[test]
    fn csrf_sec_fetch_site_is_authoritative() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "same-origin")]),
            "https"
        ));
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "none")]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "cross-site")]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("sec-fetch-site", "same-site")]),
            "https"
        ));
    }

    #[test]
    fn csrf_origin_fallback() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "https://example.com")]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "https://evil.com")]),
            "https"
        ));
    }

    #[test]
    fn csrf_origin_scheme_must_match_request_scheme() {
        // HTTP and HTTPS are different origins: an insecure origin must not
        // authorize a state-changing HTTPS request to the same host.
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "http://example.com")]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[
                ("host", "example.com"),
                ("referer", "http://example.com/form")
            ]),
            "https"
        ));
        // Matching scheme still passes on plain HTTP.
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "http://example.com")]),
            "http"
        ));
    }

    #[test]
    fn csrf_opaque_or_schemeless_origin_is_refused() {
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "null")]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com"), ("origin", "example.com")]),
            "https"
        ));
    }

    #[test]
    fn csrf_unsafe_request_without_any_origin_signal_is_refused() {
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "example.com")]),
            "https"
        ));
    }

    #[test]
    fn csrf_origin_comparison_is_case_insensitive() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[("host", "Example.com"), ("origin", "HTTPS://example.COM")]),
            "https"
        ));
    }

    #[test]
    fn csrf_origin_normalizes_default_ports() {
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[
                ("host", "example.com:443"),
                ("origin", "https://example.com")
            ]),
            "https"
        ));
        assert!(passes_cookie_csrf(
            "POST",
            &headers_with(&[
                ("host", "example.com"),
                ("origin", "https://example.com:443")
            ]),
            "https"
        ));
        assert!(!passes_cookie_csrf(
            "POST",
            &headers_with(&[
                ("host", "example.com"),
                ("origin", "https://example.com:444")
            ]),
            "https"
        ));
    }

    #[test]
    fn csrf_gate_applies_only_when_the_accepted_credential_requires_it() {
        // A cookie-authenticated cross-site request is blocked.
        assert!(cookie_csrf_blocks(
            "POST",
            &headers_with(&[
                ("cookie", "access_token=tok"),
                ("sec-fetch-site", "cross-site")
            ]),
            true,
            "https"
        ));
        // A credential accepted from another source is exempt even if a
        // stale auth cookie happens to be attached.
        assert!(!cookie_csrf_blocks(
            "POST",
            &headers_with(&[
                ("cookie", "access_token=stale"),
                ("sec-fetch-site", "cross-site")
            ]),
            false,
            "https"
        ));
        // Safe method → exempt even with the cookie.
        assert!(!cookie_csrf_blocks(
            "GET",
            &headers_with(&[
                ("cookie", "access_token=tok"),
                ("sec-fetch-site", "cross-site")
            ]),
            true,
            "https"
        ));
        // A route/credential without cookie CSRF → exempt.
        assert!(!cookie_csrf_blocks(
            "POST",
            &headers_with(&[
                ("cookie", "access_token=tok"),
                ("sec-fetch-site", "cross-site")
            ]),
            false,
            "https"
        ));
    }
}
