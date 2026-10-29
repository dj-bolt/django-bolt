/// Optimized response building utilities
///
/// Reduces the number of mutations on HttpResponse::Builder
/// by batching operations and pre-allocating capacity.
use actix_web::body::MessageBody;
use actix_web::http::header::{self, HeaderName, HeaderValue};
use actix_web::{HttpResponse, HttpResponseBuilder, http::StatusCode};

use crate::cookies::format_cookie;
use crate::response_meta::ResponseMeta;

/// The `Content-Encoding` marker that tells the compression middleware to skip.
const IDENTITY: HeaderValue = HeaderValue::from_static("identity");

/// Build a streaming response with SSE headers.
/// Pre-bundles common SSE headers to avoid multiple mutations.
///
/// `encoding_name` is the `Content-Encoding` token to set:
/// - `"identity"` → handler owns "no compression"; the global compression
///   middleware bypasses (and strips) the marker.
/// - `"br" | "gzip" | "zstd"` → handler already compressed the body
///   per-chunk; sets `Content-Encoding` so the client decodes correctly.
///
/// `user_set_content_encoding` means the caller passed an explicit
/// `Content-Encoding` header on the response; in that case we don't touch
/// `Content-Encoding` or `Vary: Accept-Encoding` — the handler is in
/// charge of the encoding contract.
///
/// `Vary: Accept-Encoding` is APPENDED (not inserted) so it composes with
/// any `Vary` the caller set (e.g. `Vary: Origin` for CORS-keyed caches).
#[inline]
pub fn build_sse_response(
    status: StatusCode,
    custom_headers: Vec<(String, String)>,
    encoding_name: &str,
    user_set_content_encoding: bool,
) -> HttpResponseBuilder {
    let mut builder = HttpResponse::build(status);

    for (k, v) in custom_headers {
        builder.append_header((k, v));
    }

    builder.content_type("text/event-stream");
    builder.insert_header(("X-Accel-Buffering", "no"));
    builder.insert_header(("Cache-Control", "no-cache, no-store, must-revalidate"));
    builder.insert_header(("Pragma", "no-cache"));
    builder.insert_header(("Expires", "0"));

    if !user_set_content_encoding {
        builder.insert_header(("Content-Encoding", encoding_name));
        // Even on the identity path, body choice depended on Accept-Encoding
        // (a brotli-capable client would have gotten brotli), so advertise
        // the Vary qualifier unconditionally. Use append to preserve any
        // caller-set Vary tokens (Origin, Cookie, …).
        builder.append_header(("Vary", "Accept-Encoding"));
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

    // 1. Content-Type: use custom or derive from response_type. The header
    // name is a constant, and a static type needs no copy of its value.
    match meta.custom_content_type.as_deref() {
        Some(content_type) => {
            if !content_type.is_empty() {
                builder.insert_header((header::CONTENT_TYPE, content_type));
            }
        }
        None => {
            if let Some(value) = meta.response_type.content_type_header() {
                builder.insert_header((header::CONTENT_TYPE, value));
            }
        }
    }

    // 2. Custom headers. `HeaderName::from_bytes` gives a lower-case name,
    // so the key needs no lower-case copy.
    if let Some(ref headers) = meta.custom_headers {
        for (k, v) in headers {
            if let Ok(name) = HeaderName::from_bytes(k.as_bytes())
                && let Ok(val) = HeaderValue::try_from(v.as_str())
            {
                builder.append_header((name, val));
            }
        }
    }

    // 3. Cookies: serialize in Rust (skip invalid cookies with warning)
    if let Some(ref cookies) = meta.cookies {
        for cookie in cookies {
            if let Some(header_value) = format_cookie(cookie) {
                builder.append_header((header::SET_COOKIE, header_value));
            }
            // Invalid cookies are logged and skipped by format_cookie
        }
    }

    // 4. Compression skip if needed
    if skip_compression {
        builder.insert_header((header::CONTENT_ENCODING, IDENTITY));
    }

    // 5. A 204 has no body (RFC 9110 §15.3.5). Actix drops the content-length
    // header for it but writes the bytes it gets, so a body would corrupt the
    // next response on a keep-alive connection.
    if status == StatusCode::NO_CONTENT {
        return builder.body(());
    }

    builder.body(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::response_meta::{CookieData, ResponseType};

    fn meta(response_type: ResponseType) -> ResponseMeta {
        ResponseMeta {
            response_type,
            custom_content_type: None,
            custom_headers: None,
            cookies: None,
        }
    }

    fn session_cookie() -> CookieData {
        CookieData {
            name: "session".to_string(),
            value: "abc123".to_string(),
            path: "/".to_string(),
            max_age: Some(3600),
            expires: None,
            domain: None,
            secure: true,
            httponly: true,
            samesite: Some("Lax".to_string()),
        }
    }

    /// A cookie that `format_cookie` rejects: a space is not valid in a name.
    fn invalid_cookie() -> CookieData {
        CookieData {
            name: "bad name".to_string(),
            ..session_cookie()
        }
    }

    /// The values of each header `name` in the response, in order.
    fn values(response: &HttpResponse, name: &str) -> Vec<String> {
        response
            .headers()
            .get_all(name)
            .map(|value| value.to_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn test_build_sse_response_sets_the_encoding_and_appends_vary() {
        let headers = vec![
            ("vary".to_string(), "Origin".to_string()),
            ("x-trace".to_string(), "1".to_string()),
        ];
        let response = build_sse_response(StatusCode::OK, headers, "br", false).finish();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(values(&response, "content-type"), ["text/event-stream"]);
        assert_eq!(values(&response, "content-encoding"), ["br"]);
        assert_eq!(values(&response, "vary"), ["Origin", "Accept-Encoding"]);
        assert_eq!(values(&response, "x-trace"), ["1"]);
        assert_eq!(values(&response, "x-accel-buffering"), ["no"]);
        assert_eq!(
            values(&response, "cache-control"),
            ["no-cache, no-store, must-revalidate"]
        );
        assert_eq!(values(&response, "pragma"), ["no-cache"]);
        assert_eq!(values(&response, "expires"), ["0"]);
    }

    #[test]
    fn test_build_sse_response_keeps_a_user_set_encoding() {
        let headers = vec![("content-encoding".to_string(), "gzip".to_string())];
        let response = build_sse_response(StatusCode::OK, headers, "identity", true).finish();
        assert_eq!(values(&response, "content-encoding"), ["gzip"]);
        assert!(values(&response, "vary").is_empty());
    }

    #[test]
    fn test_build_rate_limit_response() {
        let response = build_rate_limit_response(60, 100, 200, b"{}".to_vec());
        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(values(&response, "retry-after"), ["60"]);
        assert_eq!(values(&response, "x-ratelimit-limit"), ["100"]);
        assert_eq!(values(&response, "x-ratelimit-burst"), ["200"]);
        assert_eq!(values(&response, "content-type"), ["application/json"]);
    }

    #[test]
    fn test_meta_to_headers_gives_each_header_in_order() {
        let meta = ResponseMeta {
            custom_headers: Some(vec![("X-Custom".to_string(), "Value".to_string())]),
            cookies: Some(vec![invalid_cookie(), session_cookie()]),
            ..meta(ResponseType::Json)
        };
        let cookie = format_cookie(&session_cookie()).unwrap();
        assert_eq!(
            meta_to_headers(&meta),
            [
                ("content-type".to_string(), "application/json".to_string()),
                ("x-custom".to_string(), "Value".to_string()),
                ("set-cookie".to_string(), cookie),
            ]
        );
    }

    #[test]
    fn test_meta_to_headers_prefers_the_custom_content_type() {
        let meta = ResponseMeta {
            custom_content_type: Some("application/vnd.api+json".to_string()),
            ..meta(ResponseType::Json)
        };
        assert_eq!(
            meta_to_headers(&meta),
            [(
                "content-type".to_string(),
                "application/vnd.api+json".to_string()
            )]
        );
    }

    #[test]
    fn test_meta_to_headers_has_no_content_type_for_an_empty_response() {
        assert!(meta_to_headers(&meta(ResponseType::Empty)).is_empty());
    }

    #[test]
    fn test_build_response_from_meta_json() {
        let response = build_response_from_meta(
            StatusCode::OK,
            &meta(ResponseType::Json),
            b"{}".to_vec(),
            false,
        );
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(values(&response, "content-type"), ["application/json"]);
        assert!(values(&response, "content-encoding").is_empty());
        assert_eq!(response.body().size(), actix_web::body::BodySize::Sized(2));
    }

    #[test]
    fn test_build_response_from_meta_empty_has_no_content_type() {
        let response = build_response_from_meta(
            StatusCode::OK,
            &meta(ResponseType::Empty),
            Vec::new(),
            false,
        );
        assert!(values(&response, "content-type").is_empty());
    }

    #[test]
    fn test_build_response_from_meta_skip_compression_sets_identity() {
        let response = build_response_from_meta(
            StatusCode::OK,
            &meta(ResponseType::Json),
            b"{}".to_vec(),
            true,
        );
        assert_eq!(values(&response, "content-encoding"), ["identity"]);
    }

    #[test]
    fn test_build_response_from_meta_no_content_drops_the_body() {
        let response = build_response_from_meta(
            StatusCode::NO_CONTENT,
            &meta(ResponseType::Json),
            b"{}".to_vec(),
            false,
        );
        assert_eq!(response.status(), StatusCode::NO_CONTENT);
        assert_eq!(response.body().size(), actix_web::body::BodySize::Sized(0));
    }

    #[test]
    fn test_build_response_from_meta_with_custom_content_type() {
        let meta = ResponseMeta {
            custom_content_type: Some("application/vnd.api+json".to_string()),
            ..meta(ResponseType::Json)
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(
            values(&response, "content-type"),
            ["application/vnd.api+json"]
        );
    }

    #[test]
    fn test_build_response_from_meta_with_headers() {
        // An invalid name or value drops that header only.
        let meta = ResponseMeta {
            custom_headers: Some(vec![
                ("X-Custom".to_string(), "value".to_string()),
                ("X-Custom".to_string(), "second".to_string()),
                ("bad name".to_string(), "dropped".to_string()),
                ("X-Bad-Value".to_string(), "line\nbreak".to_string()),
                ("X-Another".to_string(), "test".to_string()),
            ]),
            ..meta(ResponseType::Json)
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(values(&response, "x-custom"), ["value", "second"]);
        assert_eq!(values(&response, "x-another"), ["test"]);
        assert!(values(&response, "x-bad-value").is_empty());
        // Content-Type, two X-Custom values and X-Another.
        assert_eq!(response.headers().len(), 4);
    }

    #[test]
    fn test_build_response_from_meta_with_cookies() {
        let meta = ResponseMeta {
            cookies: Some(vec![invalid_cookie(), session_cookie()]),
            ..meta(ResponseType::Json)
        };
        let response = build_response_from_meta(StatusCode::OK, &meta, b"{}".to_vec(), false);
        assert_eq!(
            values(&response, "set-cookie"),
            [format_cookie(&session_cookie()).unwrap()]
        );
    }

    #[test]
    fn test_build_response_from_meta_html() {
        let response = build_response_from_meta(
            StatusCode::OK,
            &meta(ResponseType::Html),
            b"<html></html>".to_vec(),
            false,
        );
        assert_eq!(
            values(&response, "content-type"),
            ["text/html; charset=utf-8"]
        );
    }

    #[test]
    fn test_build_response_from_meta_plaintext() {
        let response = build_response_from_meta(
            StatusCode::OK,
            &meta(ResponseType::PlainText),
            b"Hello, World!".to_vec(),
            false,
        );
        assert_eq!(
            values(&response, "content-type"),
            ["text/plain; charset=utf-8"]
        );
    }
}
