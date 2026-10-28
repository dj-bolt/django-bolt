/// Pre-allocated error responses for common HTTP status codes
///
/// Inspired by Bun/uWebSockets zero-copy patterns, these responses are
/// stored as static byte slices to eliminate allocations in hot paths.
use crate::error::{ErrorInfo, with_error_info};
use crate::permissions::GuardDenial;
use actix_web::HttpResponse;
use once_cell::sync::Lazy;
use std::borrow::Cow;
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
    let response = HttpResponse::Unauthorized()
        .content_type("application/json")
        .insert_header(("WWW-Authenticate", "Bearer"))
        .body(ERROR_BODY_401);
    with_error_info(response, || {
        ErrorInfo::core("not_authenticated", "Authentication required")
    })
}

#[inline]
pub fn error_403() -> HttpResponse {
    let response = HttpResponse::Forbidden()
        .content_type("application/json")
        .body(ERROR_BODY_403);
    with_error_info(response, || {
        ErrorInfo::core("forbidden", "Permission denied")
    })
}

/// A 403 carrying a guard's custom `detail`, falling back to the static body.
///
/// The custom body was serialized at registration, so this clones a `Bytes`
/// handle rather than formatting anything.
#[inline]
pub fn error_403_denial(denial: Option<&GuardDenial>) -> HttpResponse {
    match denial {
        None => error_403(),
        Some(denial) => {
            let response = HttpResponse::Forbidden()
                .content_type("application/json")
                .body(denial.body.clone());
            with_error_info(response, || {
                ErrorInfo::core("forbidden", denial.text.clone())
            })
        }
    }
}

#[inline]
pub fn error_404() -> HttpResponse {
    let response = HttpResponse::NotFound()
        .content_type("application/json")
        .body(ERROR_BODY_404);
    with_error_info(response, || ErrorInfo::core("not_found", "Not Found"))
}

#[inline]
pub fn error_400_too_many_headers() -> HttpResponse {
    let response = HttpResponse::BadRequest()
        .content_type("application/json")
        .body(ERROR_BODY_400_HEADERS);
    with_error_info(response, || {
        ErrorInfo::core("bad_request", "Too many headers")
    })
}

/// 413 Payload Too Large with static body
#[inline]
pub fn error_413() -> HttpResponse {
    let response = HttpResponse::PayloadTooLarge()
        .content_type("application/json")
        .body(ERROR_BODY_413);
    with_error_info(response, || {
        ErrorInfo::core("payload_too_large", "Payload too large")
    })
}

/// For errors that need dynamic content, we provide a fast formatter
#[inline]
pub fn error_400_header_too_large(max_size: usize) -> HttpResponse {
    let detail = format!("Header value too large (max {} bytes)", max_size);
    let mut body = Vec::with_capacity(16 + detail.len());
    body.extend_from_slice(br#"{"detail":""#);
    body.extend_from_slice(detail.as_bytes());
    body.extend_from_slice(br#""}"#);

    let response = HttpResponse::BadRequest()
        .content_type("application/json")
        .body(body);
    with_error_info(response, || ErrorInfo::core("bad_request", detail))
}

/// The location source of a request value, from the label of its 422 detail.
fn loc_source(label: &str) -> &str {
    match label {
        "Path parameter" => "path",
        "Query parameter" => "query",
        "Header" => "header",
        "Cookie" => "cookie",
        other => other,
    }
}

/// 422 Unprocessable Entity for a path, query, header or cookie value that
/// fails type validation in Rust.
///
/// The body is `{"detail": "<label> '<name>': <msg>"}`. The error formatter
/// gets the field error with the location `[<source>, <name>]`.
#[inline]
pub fn error_422_param(label: &str, name: &str, msg: &str) -> HttpResponse {
    let detail = format!("{} '{}': {}", label, name, msg);
    let mut body = Vec::with_capacity(32 + detail.len());
    body.extend_from_slice(br#"{"detail":"#);
    // The detail echoes client input. serde_json escapes quotes, backslashes,
    // and all control characters, so the body is always valid JSON.
    // It copies the unescaped runs as blocks, which is faster than a byte loop.
    serde_json::to_writer(&mut body, &detail).expect("a write to a Vec cannot fail");
    body.push(b'}');

    let response = HttpResponse::UnprocessableEntity()
        .content_type("application/json")
        .body(body);
    with_error_info(response, || {
        validation_info(serde_json::json!([{
            "loc": [loc_source(label), name],
            "msg": msg,
            "type": "validation_error",
        }]))
    })
}

/// The error information of a 422 with the field errors `errors`.
pub fn validation_info(errors: serde_json::Value) -> ErrorInfo {
    ErrorInfo::Core {
        kind: "validation",
        detail: Cow::Borrowed("Request validation failed"),
        errors: Some(errors),
        extra: None,
        reason: None,
    }
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
    fn test_error_422_param_escapes_to_valid_json() {
        use actix_web::body::MessageBody;

        // Every control character, quotes, backslash, and non-ASCII text.
        let mut detail: String = (0u8..0x20).map(char::from).collect();
        detail.push_str("\"\\ caf\u{e9} \u{1f600}");

        let response = error_422_param("Query parameter", "q", &detail);
        assert_eq!(
            response.status(),
            actix_web::http::StatusCode::UNPROCESSABLE_ENTITY
        );
        let body = response
            .into_body()
            .try_into_bytes()
            .expect("body is bytes");
        let json: serde_json::Value =
            serde_json::from_slice(&body).expect("422 body is valid JSON");
        assert_eq!(json["detail"], format!("Query parameter 'q': {detail}"));
        assert!(body.starts_with(br#"{"detail":"Query parameter 'q': \u0000\u0001"#));
        let short_forms = br#"\t\n\u000b\f\r"#;
        assert!(body.windows(short_forms.len()).any(|w| w == short_forms));
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

    fn core_info(response: &HttpResponse) -> (&'static str, String, Option<serde_json::Value>) {
        match response.extensions().get::<ErrorInfo>() {
            Some(ErrorInfo::Core {
                kind,
                detail,
                errors,
                ..
            }) => (kind, detail.to_string(), errors.clone()),
            _ => panic!("the response has no core error info"),
        }
    }

    #[test]
    fn each_error_response_names_its_kind_for_the_formatter() {
        crate::error::enable_error_info();
        let cases = [
            (error_401(), "not_authenticated", "Authentication required"),
            (error_403(), "forbidden", "Permission denied"),
            (error_404(), "not_found", "Not Found"),
            (error_413(), "payload_too_large", "Payload too large"),
            (
                error_400_too_many_headers(),
                "bad_request",
                "Too many headers",
            ),
            (
                error_400_header_too_large(8192),
                "bad_request",
                "Header value too large (max 8192 bytes)",
            ),
        ];
        for (response, kind, detail) in cases {
            assert_eq!(core_info(&response), (kind, detail.to_string(), None));
        }
    }

    #[test]
    fn a_guard_denial_gives_its_own_detail() {
        crate::error::enable_error_info();
        let denial = GuardDenial::new("Client accounts only".to_string());
        let response = error_403_denial(Some(&denial));
        assert_eq!(core_info(&response).1, "Client accounts only");
    }

    #[test]
    fn a_param_error_gives_its_location() {
        crate::error::enable_error_info();
        let response = error_422_param("Header", "x-count", "Invalid integer 'a'");
        let (kind, detail, errors) = core_info(&response);
        assert_eq!(
            (kind, detail.as_str()),
            ("validation", "Request validation failed")
        );
        assert_eq!(
            errors,
            Some(serde_json::json!([{
                "loc": ["header", "x-count"],
                "msg": "Invalid integer 'a'",
                "type": "validation_error",
            }]))
        );
    }

    #[test]
    fn test_error_400_header_size() {
        let response = error_400_header_too_large(8192);
        // Just verify it doesn't panic
        assert_eq!(response.status(), actix_web::http::StatusCode::BAD_REQUEST);
    }
}
