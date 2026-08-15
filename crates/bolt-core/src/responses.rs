/// Pre-allocated error responses for common HTTP status codes
///
/// Inspired by Bun/uWebSockets zero-copy patterns, these responses are
/// stored as static byte slices to eliminate allocations in hot paths.
use crate::permissions::GuardDenial;
use actix_web::HttpResponse;
use once_cell::sync::Lazy;
use std::collections::HashMap;

/// Pre-allocated JSON error bodies for common status codes
/// These are &'static [u8] to avoid any runtime allocations
/// The bodies are stored as static byte slices, eliminating allocation in hot paths
pub static ERROR_BODY_401: &[u8] = br#"{"detail":"Authentication required"}"#;
pub static ERROR_BODY_403: &[u8] = br#"{"detail":"Permission denied"}"#;
pub static ERROR_BODY_404: &[u8] = br#"{"detail":"Not Found"}"#;
pub static ERROR_BODY_400_HEADERS: &[u8] = br#"{"detail":"Too many headers"}"#;
pub static ERROR_BODY_413: &[u8] = br#"{"detail":"Payload too large"}"#;

/// Cache for pre-formatted rate limit error bodies (keyed by retry_after seconds)
/// This avoids allocating the same error message repeatedly
pub static RATE_LIMIT_CACHE: Lazy<parking_lot::Mutex<HashMap<u64, Vec<u8>>>> =
    Lazy::new(|| parking_lot::Mutex::new(HashMap::with_capacity(64)));

const MAX_RATE_LIMIT_CACHE_SIZE: usize = 128;

/// Get or create a rate limit error body for a given retry_after value
/// Uses a small cache (128 entries) to avoid formatting the same values repeatedly
pub fn get_rate_limit_body(retry_after: u64) -> Vec<u8> {
    // Fast path: check cache first
    {
        let cache = RATE_LIMIT_CACHE.lock();
        if let Some(body) = cache.get(&retry_after) {
            return body.clone();
        }
    }

    // Slow path: format and cache
    let body = format!(
        r#"{{"detail":"Rate limit exceeded. Try again in {} seconds.","retry_after":{}}}"#,
        retry_after, retry_after
    )
    .into_bytes();

    // Insert into cache (with size limit)
    let mut cache = RATE_LIMIT_CACHE.lock();
    if cache.len() < MAX_RATE_LIMIT_CACHE_SIZE {
        cache.insert(retry_after, body.clone());
    }

    body
}

/// Create error responses using static bodies
/// These functions build HttpResponse objects from pre-allocated static byte slices
/// The benefit: no heap allocation for the response body
#[inline]
pub fn error_401() -> HttpResponse {
    // RFC 6750 §3: a 401 to a request for a Bearer-protected resource
    // carries a WWW-Authenticate challenge. No error attribute is included
    // (the failure reason is deliberately not disclosed to clients).
    HttpResponse::Unauthorized()
        .content_type("application/json")
        .insert_header(("WWW-Authenticate", "Bearer"))
        .body(ERROR_BODY_401)
}

#[inline]
pub fn error_403() -> HttpResponse {
    HttpResponse::Forbidden()
        .content_type("application/json")
        .body(ERROR_BODY_403)
}

/// A 403 carrying a guard's custom `detail`, falling back to the static body.
///
/// The custom body was serialized at registration, so this clones a `Bytes`
/// handle rather than formatting anything.
#[inline]
pub fn error_403_denial(denial: Option<&GuardDenial>) -> HttpResponse {
    match denial {
        None => error_403(),
        Some(denial) => HttpResponse::Forbidden()
            .content_type("application/json")
            .body(denial.body.clone()),
    }
}

#[inline]
pub fn error_404() -> HttpResponse {
    HttpResponse::NotFound()
        .content_type("application/json")
        .body(ERROR_BODY_404)
}

#[inline]
pub fn error_400_too_many_headers() -> HttpResponse {
    HttpResponse::BadRequest()
        .content_type("application/json")
        .body(ERROR_BODY_400_HEADERS)
}

/// 413 Payload Too Large with static body
#[inline]
pub fn error_413() -> HttpResponse {
    HttpResponse::PayloadTooLarge()
        .content_type("application/json")
        .body(ERROR_BODY_413)
}

/// For errors that need dynamic content, we provide a fast formatter
#[inline]
pub fn error_400_header_too_large(max_size: usize) -> HttpResponse {
    // Use a faster format approach with capacity pre-allocation
    let mut body = Vec::with_capacity(64);
    body.extend_from_slice(br#"{"detail":"Header value too large (max "#);
    body.extend_from_slice(max_size.to_string().as_bytes());
    body.extend_from_slice(br#" bytes)"}"#);

    HttpResponse::BadRequest()
        .content_type("application/json")
        .body(body)
}

/// 422 Unprocessable Entity for validation errors (type coercion failures)
/// Used when path/query parameters fail type validation in Rust
#[inline]
pub fn error_422_validation(detail: &str) -> HttpResponse {
    // Pre-allocate based on expected size (avoid reallocation)
    let mut body = Vec::with_capacity(32 + detail.len());
    body.extend_from_slice(br#"{"detail":""#);
    // Escape any quotes in the detail message
    for byte in detail.bytes() {
        if byte == b'"' {
            body.extend_from_slice(br#"\""#);
        } else if byte == b'\\' {
            body.extend_from_slice(br#"\\"#);
        } else {
            body.push(byte);
        }
    }
    body.extend_from_slice(br#""}"#);

    HttpResponse::UnprocessableEntity()
        .content_type("application/json")
        .body(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_error_bodies_valid_json() {
        // Ensure all error bodies are valid JSON
        assert!(serde_json::from_slice::<serde_json::Value>(ERROR_BODY_401).is_ok());
        assert!(serde_json::from_slice::<serde_json::Value>(ERROR_BODY_403).is_ok());
        assert!(serde_json::from_slice::<serde_json::Value>(ERROR_BODY_404).is_ok());
        assert!(serde_json::from_slice::<serde_json::Value>(ERROR_BODY_400_HEADERS).is_ok());
        assert!(serde_json::from_slice::<serde_json::Value>(ERROR_BODY_413).is_ok());
    }

    #[test]
    fn test_error_413_static() {
        let response = error_413();
        assert_eq!(
            response.status(),
            actix_web::http::StatusCode::PAYLOAD_TOO_LARGE
        );
    }

    #[test]
    fn test_rate_limit_cache() {
        let body1 = get_rate_limit_body(60);
        let body2 = get_rate_limit_body(60);

        // Second call should use cache
        assert_eq!(body1, body2);

        // Verify JSON structure
        let json: serde_json::Value = serde_json::from_slice(&body1).unwrap();
        assert_eq!(json["retry_after"], 60);
    }

    #[test]
    fn test_error_400_header_size() {
        let response = error_400_header_too_large(8192);
        // Just verify it doesn't panic
        assert_eq!(response.status(), actix_web::http::StatusCode::BAD_REQUEST);
    }
}
