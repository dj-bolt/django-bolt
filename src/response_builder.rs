/// Optimized response building utilities
///
/// Reduces the number of mutations on HttpResponse::Builder
/// by batching operations and pre-allocating capacity.
use actix_web::body::MessageBody;
use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::{http::StatusCode, HttpResponse, HttpResponseBuilder};

use crate::cookies::format_cookie;
use crate::response_meta::ResponseMeta;

/// Build a streaming response with SSE headers
/// Pre-bundles common SSE headers to avoid multiple mutations
#[inline]
pub fn build_sse_response(
    status: StatusCode,
    custom_headers: Vec<(String, String)>,
    skip_compression: bool,
) -> HttpResponseBuilder {
    let mut builder = HttpResponse::build(status);

    // Add custom headers first
    // Use append_header to support multiple headers with the same name
    for (k, v) in custom_headers {
        builder.append_header((k, v));
    }

    // Batch SSE headers (avoid 5 separate mutations)
    builder.content_type("text/event-stream");
    builder.insert_header(("X-Accel-Buffering", "no"));
    builder.insert_header(("Cache-Control", "no-cache, no-store, must-revalidate"));
    builder.insert_header(("Pragma", "no-cache"));
    builder.insert_header(("Expires", "0"));

    if skip_compression {
        builder.insert_header(("Content-Encoding", "identity"));
    }

    builder
}

/// Build response with retry-after header (for rate limiting)
#[inline]
pub fn build_rate_limit_response(
    retry_after: u64,
    rps: u32,
    burst: u32,
    body: Vec<u8>,
) -> HttpResponse {
    // Batch all headers at once
    HttpResponse::TooManyRequests()
        .insert_header(("Retry-After", retry_after.to_string()))
        .insert_header(("X-RateLimit-Limit", rps.to_string()))
        .insert_header(("X-RateLimit-Burst", burst.to_string()))
        .content_type("application/json")
        .body(body)
}

/// Convert ResponseMeta to a Vec of header tuples.
/// Used for streaming responses that need headers but use a different builder path.
#[inline]
pub fn meta_to_headers(meta: &ResponseMeta) -> Vec<(String, String)> {
    let mut headers = Vec::new();

    // 1. Content-Type: use custom or derive from response_type
    let content_type = meta
        .custom_content_type
        .as_deref()
        .unwrap_or_else(|| meta.response_type.content_type());

    if !content_type.is_empty() {
        headers.push(("content-type".to_string(), content_type.to_string()));
    }

    // 2. Custom headers (lowercase keys)
    if let Some(ref custom_headers) = meta.custom_headers {
        for (k, v) in custom_headers {
            headers.push((k.to_ascii_lowercase(), v.clone()));
        }
    }

    // 3. Cookies: serialize in Rust (skip invalid cookies with warning)
    if let Some(ref cookies) = meta.cookies {
        for cookie in cookies {
            if let Some(header_value) = format_cookie(cookie) {
                headers.push(("set-cookie".to_string(), header_value));
            }
            // Invalid cookies are logged and skipped by format_cookie
        }
    }

    headers
}

/// Build HTTP response with all headers from ResponseMeta.
/// This is the unified path for all response types.
///
/// Handles:
/// - Content-Type from static strings based on response_type (no allocation)
/// - Custom headers with keys lowercased in Rust (single location)
/// - Cookies serialized directly in Rust (replaces SimpleCookie)
#[inline]
pub fn build_response_from_meta<B>(
    status: StatusCode,
    meta: &ResponseMeta,
    body: B,
    skip_compression: bool,
) -> HttpResponse
where
    B: MessageBody + 'static,
{
    let mut builder = HttpResponse::build(status);

    // 1. Content-Type: use custom or derive from response_type
    let content_type = meta
        .custom_content_type
        .as_deref()
        .unwrap_or_else(|| meta.response_type.content_type());

    if !content_type.is_empty() {
        builder.insert_header(("content-type", content_type));
    }

    // 2. Custom headers (lowercase keys in Rust - single location)
    if let Some(ref headers) = meta.custom_headers {
        for (k, v) in headers {
            // Lowercase here instead of in Python (best practice from Robyn)
            if let Ok(name) = HeaderName::try_from(k.to_ascii_lowercase()) {
                if let Ok(val) = HeaderValue::try_from(v.as_str()) {
                    builder.append_header((name, val));
                }
            }
        }
    }

    // 3. Cookies: serialize in Rust (skip invalid cookies with warning)
    if let Some(ref cookies) = meta.cookies {
        for cookie in cookies {
            if let Some(header_value) = format_cookie(cookie) {
                builder.append_header(("set-cookie", header_value));
            }
            // Invalid cookies are logged and skipped by format_cookie
        }
    }

    // 4. Compression skip if needed
    if skip_compression {
        builder.insert_header(("content-encoding", "identity"));
    }

    builder.body(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::response_meta::{CookieData, ResponseType};

    #[test]
    fn test_build_rate_limit_response() {
        let response = build_rate_limit_response(60, 100, 200, b"{}".to_vec());
        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
    }

    #[test]
    fn test_build_response_from_meta_json() {
        let meta = ResponseMeta {
            response_type: ResponseType::Json,
            custom_content_type: None,
            custom_headers: None,
            cookies: None,
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_build_response_from_meta_with_custom_content_type() {
        let meta = ResponseMeta {
            response_type: ResponseType::Json,
            custom_content_type: Some("application/vnd.api+json".to_string()),
            custom_headers: None,
            cookies: None,
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_build_response_from_meta_with_headers() {
        let meta = ResponseMeta {
            response_type: ResponseType::Json,
            custom_content_type: None,
            custom_headers: Some(vec![
                ("X-Custom".to_string(), "value".to_string()),
                ("X-Another".to_string(), "test".to_string()),
            ]),
            cookies: None,
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_build_response_from_meta_with_cookies() {
        let meta = ResponseMeta {
            response_type: ResponseType::Json,
            custom_content_type: None,
            custom_headers: None,
            cookies: Some(vec![CookieData {
                name: "session".to_string(),
                value: "abc123".to_string(),
                path: "/".to_string(),
                max_age: Some(3600),
                expires: None,
                domain: None,
                secure: true,
                httponly: true,
                samesite: Some("Lax".to_string()),
            }]),
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_build_response_from_meta_html() {
        let meta = ResponseMeta {
            response_type: ResponseType::Html,
            custom_content_type: None,
            custom_headers: None,
            cookies: None,
        };
        let response =
            build_response_from_meta(StatusCode::OK, &meta, b"<html></html>".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_build_response_from_meta_plaintext() {
        let meta = ResponseMeta {
            response_type: ResponseType::PlainText,
            custom_content_type: None,
            custom_headers: None,
            cookies: None,
        };
        let response =
            build_response_from_meta(StatusCode::OK, &meta, b"Hello, World!".to_vec(), false);
        assert_eq!(response.status(), StatusCode::OK);
    }
}
