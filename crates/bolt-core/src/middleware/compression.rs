use actix_http::encoding::Encoder;
/// Custom compression middleware that respects Content-Encoding: identity header
/// and CompressionConfig settings.
///
/// This middleware checks response headers BEFORE applying compression.
/// If Content-Encoding: identity is present (set when skip_compression=true),
/// it removes the header and returns the response uncompressed.
/// Otherwise, it applies compression based on CompressionConfig and Accept-Encoding.
use actix_web::{
    Error, HttpResponse,
    body::{BodySize, BoxBody, MessageBody},
    dev::{Service, ServiceRequest, ServiceResponse, Transform, forward_ready},
    http::header::{
        ACCEPT_ENCODING, CONTENT_ENCODING, ContentEncoding, ETAG, HeaderMap, HeaderValue,
        IF_NONE_MATCH, VARY,
    },
    http::{Method, StatusCode},
};
use futures_util::future::{FutureExt, Map};
use std::future::{Ready, ready};
use std::sync::Arc;

use crate::metadata::CompressionConfig;
use crate::state::AppState;
use crate::streaming_compression::accepts_encoding;

/// The size of the body that the GET answer has. The handler empties the body
/// of a HEAD answer before this middleware runs, so it keeps the size here.
#[derive(Clone, Copy)]
struct HeadBody(BodySize);

/// Empty the body of a HEAD answer and keep its size.
///
/// RFC 9110, section 9.3.2: a HEAD answer has the header fields of the GET
/// answer. This middleware reads the size from `HeadBody`, so a HEAD answer
/// gets the same `Content-Encoding`, `Vary` and `ETag` as the GET answer.
///
/// The new body has no size, so actix sends no `Content-Length`. An empty
/// body would send `Content-Length: 0`, and section 8.6 forbids a value that
/// is not the length of the GET body.
pub fn head_answer(response: HttpResponse) -> HttpResponse {
    let size = response.body().size();
    let mut response = response.set_body(BoxBody::new(actix_web::body::None::new()));
    response.extensions_mut().insert(HeadBody(size));
    response
}

/// Make a strong ETag weak (RFC 9110, section 8.8.1). Use this when Bolt
/// codes the body: the coded body is another representation of the resource.
pub fn weaken_etag(headers: &mut HeaderMap) {
    if let Some(weak) = headers.get(ETAG).and_then(weak_etag) {
        headers.insert(ETAG, weak);
    }
}

/// Compression middleware factory
pub struct CompressionMiddleware;

impl Default for CompressionMiddleware {
    fn default() -> Self {
        Self::new()
    }
}

impl CompressionMiddleware {
    pub fn new() -> Self {
        Self
    }
}

impl<S, B> Transform<S, ServiceRequest> for CompressionMiddleware
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<Encoder<B>>;
    type Error = Error;
    type InitError = ();
    type Transform = CompressionMiddlewareService<S>;
    type Future = Ready<Result<Self::Transform, Self::InitError>>;

    fn new_transform(&self, service: S) -> Self::Future {
        ready(Ok(CompressionMiddlewareService { service }))
    }
}

pub struct CompressionMiddlewareService<S> {
    service: S,
}

/// The result of the inner service, and of this middleware.
type Outcome<B> = Result<ServiceResponse<B>, Error>;

impl<S, B> Service<ServiceRequest> for CompressionMiddlewareService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<Encoder<B>>;
    type Error = Error;
    // A named future: the request needs no box, and it copies nothing.
    type Future = Map<S::Future, fn(Outcome<B>) -> Outcome<Encoder<B>>>;

    forward_ready!(service);

    fn call(&self, req: ServiceRequest) -> Self::Future {
        self.service.call(req).map(encode_response::<B>)
    }
}

/// Wrap the body of a response in the encoder that the response gets. The
/// response holds its request, so the Accept-Encoding value and the config
/// are read in place.
fn encode_response<B: MessageBody>(res: Outcome<B>) -> Outcome<Encoder<B>> {
    let res = res?;

    // If the response already declares a Content-Encoding, the handler
    // owns the encoding (e.g. per-chunk SSE compression). Pass it
    // through without re-wrapping.
    //
    // Special case: `identity` is our internal "skip compression"
    // marker — strip it so clients don't see it. Any other value
    // (br, gzip, zstd) is preserved as-is.
    let pre_set = res
        .headers()
        .get(CONTENT_ENCODING)
        .and_then(|v| v.to_str().ok());

    if let Some(encoding) = pre_set {
        let is_identity = encoding.eq_ignore_ascii_case("identity");
        let (req, mut response) = res.into_parts();
        if is_identity {
            response.headers_mut().remove(CONTENT_ENCODING);
        }
        return Ok(ServiceResponse::new(
            req,
            response
                .map_body(|head, body| Encoder::response(ContentEncoding::Identity, head, body)),
        ));
    }

    let request = res.request();
    let accept_encoding = request
        .headers()
        .get(ACCEPT_ENCODING)
        .and_then(|v| v.to_str().ok());
    let config = request
        .app_data::<actix_web::web::Data<Arc<AppState>>>()
        .and_then(|state| state.global_compression_config.as_deref());
    // A HEAD answer has no body here. The size of its GET body decides.
    let head = (request.method() == Method::HEAD)
        .then(|| res.response().extensions().get::<HeadBody>().copied())
        .flatten();
    let size = head.map_or_else(|| res.response().body().size(), |head| head.0);
    let encoding = response_encoding(accept_encoding, config, size);
    let negotiated = negotiates(config, size, res.status());
    // A 304 has no body, so the size that decides a coding is not known here.
    let not_modified = res.status() == StatusCode::NOT_MODIFIED && config.is_some();
    let coded_304 =
        not_modified && select_encoding(accept_encoding, config) != ContentEncoding::Identity;

    // When the encoder compresses the body, it sets Content-Encoding
    // and appends `Vary: accept-encoding` after a Vary of the handler.
    let (req, mut response) = res.into_parts();
    if negotiated {
        let headers = response.headers_mut();
        if encoding == ContentEncoding::Identity {
            // RFC 9110, section 12.5.5: the choice read Accept-Encoding, so a
            // plain answer varies on it too. The encoder adds no Vary here.
            if !varies_on_accept_encoding(headers) {
                headers.append(VARY, HeaderValue::from_static("accept-encoding"));
            }
        } else {
            weaken_etag(headers);
            if head.is_some() {
                // The encoder sets these fields only on a body that it codes.
                // Set them as it does for the GET answer.
                headers.insert(CONTENT_ENCODING, encoding.to_header_value());
                headers.append(VARY, HeaderValue::from_static("accept-encoding"));
            }
        }
    } else if not_modified {
        // RFC 9110, section 15.4.5: a 304 has the Vary and the ETag that the
        // 200 would have. The size of the 200 body is not known here, so a
        // 304 varies on accept-encoding whenever a config is on.
        let headers = response.headers_mut();
        if !varies_on_accept_encoding(headers) {
            headers.append(VARY, HeaderValue::from_static("accept-encoding"));
        }
    }
    if coded_304 {
        // A client that holds the coded 200 sends its weak ETag, so it gets the
        // weak form back. A client that holds the plain 200 sends the strong one.
        let weak = response
            .headers()
            .get(ETAG)
            .filter(|etag| lists_weak_form(req.headers(), etag.as_bytes()))
            .and_then(weak_etag);
        if let Some(weak) = weak {
            response.headers_mut().insert(ETAG, weak);
        }
    }
    Ok(ServiceResponse::new(
        req,
        response.map_body(|head, body| Encoder::response(encoding, head, body)),
    ))
}

/// Whether the choice of a coding read `Accept-Encoding` for this answer: a
/// config is on, the body is not below its minimum size, and actix-http can
/// code the status (not 204, 206 or 304) and the body (not empty).
fn negotiates(config: Option<&CompressionConfig>, size: BodySize, status: StatusCode) -> bool {
    let Some(cfg) = config else {
        return false;
    };
    let codable_body = match size {
        BodySize::None => false,
        BodySize::Sized(n) => n > 0 && n >= cfg.minimum_size as u64,
        BodySize::Stream => true,
    };
    codable_body
        && !matches!(
            status,
            StatusCode::NO_CONTENT | StatusCode::PARTIAL_CONTENT | StatusCode::NOT_MODIFIED
        )
}

/// The weak form of a strong ETag (`"x"` gives `W/"x"`), or `None` for an
/// ETag that is weak already or not quoted.
fn weak_etag(etag: &HeaderValue) -> Option<HeaderValue> {
    if !etag.as_bytes().starts_with(b"\"") {
        return None;
    }
    HeaderValue::from_bytes(&[b"W/", etag.as_bytes()].concat()).ok()
}

/// Whether `If-None-Match` lists the weak form of a strong `etag`, such as
/// `W/"v1"` for `"v1"`.
fn lists_weak_form(headers: &actix_web::http::header::HeaderMap, etag: &[u8]) -> bool {
    headers.get_all(IF_NONE_MATCH).any(|value| {
        let mut rest = value.as_bytes();
        loop {
            while let [b' ' | b'\t' | b',', tail @ ..] = rest {
                rest = tail;
            }
            let (weak, tag) = match rest.strip_prefix(b"W/") {
                Some(tag) => (true, tag),
                None => (false, rest),
            };
            // An entity tag is a quoted string with no quote in it (RFC 9110,
            // section 8.8.3). Anything else, such as `*`, ends the list.
            let Some(end) = tag.get(1..).and_then(|t| t.iter().position(|&b| b == b'"')) else {
                return false;
            };
            if !tag.starts_with(b"\"") {
                return false;
            }
            if weak && &tag[..end + 2] == etag {
                return true;
            }
            rest = &tag[end + 2..];
        }
    })
}

/// Whether a `Vary` of the handler names `accept-encoding` or is `*`.
fn varies_on_accept_encoding(headers: &actix_web::http::header::HeaderMap) -> bool {
    headers
        .get_all(VARY)
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(str::trim)
        .any(|name| name == "*" || name.eq_ignore_ascii_case("accept-encoding"))
}

#[cfg(test)]
mod bypass_tests {
    use super::*;
    use actix_web::http::header::{HeaderName, HeaderValue};
    use actix_web::test::{self, TestRequest};
    use actix_web::{App, HttpResponse, web};

    async fn assert_encoding_preserved(pre_set_encoding: &'static str) {
        // The body must be >= minimum_size (default 500 bytes) to trigger the
        // compression path in the middleware. With a short body the middleware
        // skips compression regardless, which would hide the bug we are testing.
        let large_body: Vec<u8> = b"already-compressed-bytes-"
            .iter()
            .cycle()
            .take(600)
            .cloned()
            .collect();
        let large_body_clone = large_body.clone();

        let app = test::init_service(App::new().wrap(CompressionMiddleware::new()).route(
            "/",
            web::get().to(move || {
                let body = large_body_clone.clone();
                async move {
                    HttpResponse::Ok()
                        .insert_header((
                            HeaderName::from_static("content-encoding"),
                            HeaderValue::from_static(pre_set_encoding),
                        ))
                        .body(body)
                }
            }),
        ))
        .await;

        let req = TestRequest::get()
            .uri("/")
            .insert_header(("accept-encoding", "br, gzip, zstd"))
            .to_request();

        let resp = test::call_service(&app, req).await;
        assert_eq!(resp.status(), 200);

        let ce = resp
            .headers()
            .get("content-encoding")
            .map(|v| v.to_str().unwrap().to_string());
        assert_eq!(ce, Some(pre_set_encoding.to_string()));

        let body = test::read_body(resp).await;
        assert_eq!(
            body.as_ref(),
            large_body.as_slice(),
            "body was re-encoded; expected pass-through"
        );
    }

    #[actix_web::test]
    async fn pre_set_brotli_encoding_is_preserved() {
        assert_encoding_preserved("br").await;
    }

    #[actix_web::test]
    async fn pre_set_gzip_encoding_is_preserved() {
        assert_encoding_preserved("gzip").await;
    }

    #[actix_web::test]
    async fn pre_set_zstd_encoding_is_preserved() {
        assert_encoding_preserved("zstd").await;
    }

    /// Call a middleware app that answers with 600 bytes and `headers`.
    async fn call_with_headers(
        headers: &'static [(&'static str, &'static str)],
    ) -> ServiceResponse<impl MessageBody> {
        let app = test::init_service(App::new().wrap(CompressionMiddleware::new()).route(
            "/",
            web::get().to(move || async move {
                let mut response = HttpResponse::Ok();
                for (name, value) in headers {
                    response.insert_header((*name, *value));
                }
                response.body(vec![b'x'; 600])
            }),
        ))
        .await;
        let req = TestRequest::get()
            .uri("/")
            .insert_header(("accept-encoding", "br, gzip, zstd"))
            .to_request();
        test::call_service(&app, req).await
    }

    #[actix_web::test]
    async fn the_identity_marker_is_removed() {
        let resp = call_with_headers(&[("content-encoding", "IDENTITY")]).await;
        assert!(resp.headers().get("content-encoding").is_none());
        assert!(resp.headers().get("vary").is_none());
        assert_eq!(test::read_body(resp).await, vec![b'x'; 600]);
    }

    #[actix_web::test]
    async fn an_app_without_a_compression_config_does_not_compress() {
        // The app has no AppState, so the middleware has no config.
        let resp = call_with_headers(&[("vary", "Origin")]).await;
        assert!(resp.headers().get("content-encoding").is_none());
        let vary: Vec<_> = resp.headers().get_all("vary").collect();
        assert_eq!(vary, [&HeaderValue::from_static("Origin")]);
        assert_eq!(test::read_body(resp).await, vec![b'x'; 600]);
    }
}

/// Select best compression encoding based on config and client support.
///
/// Mirrors `select_stream_encoding` in `streaming_compression.rs` so buffered
/// and streaming responses honor the same RFC 7231 §5.3.4 Accept-Encoding
/// rules (q-values, `*` wildcard, case-insensitive coding names).
///
/// Returns `Identity` when:
/// - the request omitted `Accept-Encoding`,
/// - `compression=None` (no `CompressionConfig` on the app),
/// - the client rejects every supported coding.
fn select_encoding(
    accept_encoding: Option<&str>,
    config: Option<&CompressionConfig>,
) -> ContentEncoding {
    let ae = match accept_encoding {
        Some(ae) => ae,
        None => return ContentEncoding::Identity,
    };
    let cfg = match config {
        Some(c) => c,
        None => return ContentEncoding::Identity,
    };

    let (preferred_token, preferred_encoding) = match cfg.backend.as_str() {
        "brotli" => ("br", ContentEncoding::Brotli),
        "gzip" => ("gzip", ContentEncoding::Gzip),
        "zstd" => ("zstd", ContentEncoding::Zstd),
        _ => return ContentEncoding::Identity,
    };

    if accepts_encoding(ae, preferred_token) {
        return preferred_encoding;
    }
    if cfg.gzip_fallback && accepts_encoding(ae, "gzip") {
        return ContentEncoding::Gzip;
    }
    ContentEncoding::Identity
}

/// The coding for a response body of `size`. A body that is smaller than the
/// minimum size of the config gets `Identity`. A body of unknown size gets
/// the coding of `select_encoding`.
fn response_encoding(
    accept_encoding: Option<&str>,
    config: Option<&CompressionConfig>,
    size: BodySize,
) -> ContentEncoding {
    match (config, size) {
        (Some(cfg), BodySize::Sized(size)) if size < cfg.minimum_size as u64 => {
            ContentEncoding::Identity
        }
        _ => select_encoding(accept_encoding, config),
    }
}

#[cfg(test)]
mod representation_tests {
    //! The headers that tell a cache which answer it holds: `Vary` on each
    //! negotiated answer (RFC 9110, section 12.5.5) and a weak ETag on a
    //! coded one (RFC 9110, section 8.8.1).
    use super::*;
    use actix_web::http::StatusCode;
    use actix_web::http::header::HeaderValue;

    #[test]
    fn if_none_match_lists_the_weak_form() {
        use actix_web::http::header::{HeaderMap, IF_NONE_MATCH};
        let lists = |value: &'static str| {
            let mut headers = HeaderMap::new();
            headers.insert(IF_NONE_MATCH, HeaderValue::from_static(value));
            lists_weak_form(&headers, b"\"v1\"")
        };
        assert!(lists("W/\"v1\""));
        assert!(lists("\"v0\", W/\"v1\""));
        assert!(lists("W/\"a,b\", W/\"v1\""));
        assert!(!lists("\"v1\""));
        assert!(!lists("W/\"v2\", \"v1\""));
        assert!(!lists("*"));
        assert!(!lists("W/\"v1"));
        assert!(!lists("W/\"v10\""));
    }

    #[test]
    fn an_answer_negotiates_when_a_config_could_code_it() {
        let cfg = CompressionConfig::default();
        let min = cfg.minimum_size as u64;
        assert!(negotiates(Some(&cfg), BodySize::Sized(min), StatusCode::OK));
        assert!(negotiates(
            Some(&cfg),
            BodySize::Stream,
            StatusCode::NOT_FOUND
        ));
        assert!(!negotiates(None, BodySize::Sized(min), StatusCode::OK));
        assert!(!negotiates(
            Some(&cfg),
            BodySize::Sized(min - 1),
            StatusCode::OK
        ));
        assert!(!negotiates(Some(&cfg), BodySize::Sized(0), StatusCode::OK));
        assert!(!negotiates(Some(&cfg), BodySize::None, StatusCode::OK));
        for status in [
            StatusCode::NO_CONTENT,
            StatusCode::PARTIAL_CONTENT,
            StatusCode::NOT_MODIFIED,
        ] {
            assert!(
                !negotiates(Some(&cfg), BodySize::Sized(min), status),
                "{status}"
            );
        }
    }

    #[test]
    fn a_strong_etag_becomes_weak() {
        let weak = weak_etag(&HeaderValue::from_static("\"v1\"")).unwrap();
        assert_eq!(weak, "W/\"v1\"");
        assert!(weak_etag(&HeaderValue::from_static("W/\"v1\"")).is_none());
        assert!(weak_etag(&HeaderValue::from_static("v1")).is_none());
    }
}

#[cfg(test)]
mod select_encoding_tests {
    //! Buffered `select_encoding` mirrors the streaming
    //! `select_stream_encoding` parser, so its negotiation contract should
    //! match: q-values honored, `*` wildcard handled, no config → identity.
    use super::*;

    fn cfg_brotli() -> CompressionConfig {
        CompressionConfig::default()
    }

    fn cfg_gzip() -> CompressionConfig {
        CompressionConfig {
            backend: "gzip".to_string(),
            ..Default::default()
        }
    }

    fn cfg_zstd() -> CompressionConfig {
        CompressionConfig {
            backend: "zstd".to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn missing_accept_encoding_returns_identity() {
        assert_eq!(
            select_encoding(None, Some(&cfg_brotli())),
            ContentEncoding::Identity
        );
    }

    #[test]
    fn missing_config_returns_identity() {
        // `compression=None` on BoltAPI → AppState.global_compression_config = None.
        // Buffered middleware must not silently compress in that case.
        assert_eq!(
            select_encoding(Some("br, gzip"), None),
            ContentEncoding::Identity
        );
    }

    #[test]
    fn picks_preferred_backend_when_accepted() {
        assert_eq!(
            select_encoding(Some("br, gzip"), Some(&cfg_brotli())),
            ContentEncoding::Brotli
        );
        assert_eq!(
            select_encoding(Some("gzip, br"), Some(&cfg_gzip())),
            ContentEncoding::Gzip
        );
        assert_eq!(
            select_encoding(Some("zstd, gzip"), Some(&cfg_zstd())),
            ContentEncoding::Zstd
        );
    }

    #[test]
    fn falls_back_to_gzip_when_backend_rejected() {
        // Brotli configured, client only accepts gzip.
        assert_eq!(
            select_encoding(Some("gzip"), Some(&cfg_brotli())),
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn skips_fallback_when_disabled() {
        let mut cfg = cfg_brotli();
        cfg.gzip_fallback = false;
        assert_eq!(
            select_encoding(Some("gzip"), Some(&cfg)),
            ContentEncoding::Identity
        );
    }

    #[test]
    fn no_negotiable_coding_returns_identity() {
        assert_eq!(
            select_encoding(Some("deflate, identity"), Some(&cfg_brotli())),
            ContentEncoding::Identity
        );
    }

    // ─── RFC 7231 §5.3.4 conformance (regressions vs. old substring matcher)

    #[test]
    fn q0_rejects_preferred_and_falls_back() {
        // `br;q=0` explicitly rejects brotli; old substring matcher missed
        // this and still picked Brotli because the string contained "br".
        assert_eq!(
            select_encoding(Some("br;q=0, gzip"), Some(&cfg_brotli())),
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn q0_on_all_returns_identity() {
        assert_eq!(
            select_encoding(Some("br;q=0, gzip;q=0"), Some(&cfg_brotli())),
            ContentEncoding::Identity
        );
    }

    #[test]
    fn star_accepts_preferred_backend() {
        // Plain `*` accepts any unmentioned coding.
        assert_eq!(
            select_encoding(Some("*"), Some(&cfg_brotli())),
            ContentEncoding::Brotli
        );
    }

    #[test]
    fn star_q0_rejects_unmentioned_codings() {
        // `gzip, *;q=0` means: gzip only, everything else rejected.
        assert_eq!(
            select_encoding(Some("gzip, *;q=0"), Some(&cfg_brotli())),
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn explicit_q0_overrides_generous_star() {
        // `br;q=0, *` — brotli is explicitly rejected even though `*` is generous.
        assert_eq!(
            select_encoding(Some("br;q=0, *"), Some(&cfg_brotli())),
            // `*` allows gzip → fallback kicks in.
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn case_insensitive_coding_names() {
        assert_eq!(
            select_encoding(Some("BR"), Some(&cfg_brotli())),
            ContentEncoding::Brotli
        );
        assert_eq!(
            select_encoding(Some("GZip;Q=0.5"), Some(&cfg_gzip())),
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn substring_false_positive_no_longer_matches() {
        // `x-gzip-old` contains "gzip" as a substring but is NOT the gzip
        // coding token. The old substring matcher would have falsely picked
        // gzip; the RFC parser correctly returns identity (no fallback either,
        // since `x-gzip-old` ≠ `gzip`).
        let mut cfg = cfg_brotli();
        cfg.gzip_fallback = false;
        assert_eq!(
            select_encoding(Some("x-gzip-old"), Some(&cfg)),
            ContentEncoding::Identity
        );
    }

    // ─── response_encoding ────────────────────────────────────────────

    #[test]
    fn a_body_from_the_minimum_size_up_is_compressed() {
        let cfg = cfg_brotli();
        let min = cfg.minimum_size as u64;
        for size in [min, min + 1] {
            assert_eq!(
                response_encoding(Some("br"), Some(&cfg), BodySize::Sized(size)),
                ContentEncoding::Brotli,
                "size {size}"
            );
        }
    }

    #[test]
    fn a_body_under_the_minimum_size_is_not_compressed() {
        let cfg = cfg_brotli();
        let size = cfg.minimum_size as u64 - 1;
        assert_eq!(
            response_encoding(Some("br"), Some(&cfg), BodySize::Sized(size)),
            ContentEncoding::Identity
        );
    }

    #[test]
    fn a_body_of_unknown_size_is_compressed() {
        // A stream has no size, so the minimum size does not apply.
        let cfg = CompressionConfig {
            minimum_size: 1 << 20,
            ..cfg_gzip()
        };
        assert_eq!(
            response_encoding(Some("gzip"), Some(&cfg), BodySize::Stream),
            ContentEncoding::Gzip
        );
    }

    #[test]
    fn a_large_body_follows_the_accept_encoding() {
        let cfg = cfg_zstd();
        let size = BodySize::Sized(cfg.minimum_size as u64 * 2);
        assert_eq!(
            response_encoding(None, Some(&cfg), size),
            ContentEncoding::Identity
        );
        assert_eq!(
            response_encoding(Some("identity"), Some(&cfg), size),
            ContentEncoding::Identity
        );
        assert_eq!(
            response_encoding(Some("zstd"), Some(&cfg), size),
            ContentEncoding::Zstd
        );
    }

    #[test]
    fn no_config_is_never_compressed() {
        for size in [
            BodySize::Sized(0),
            BodySize::Sized(1 << 20),
            BodySize::Stream,
        ] {
            assert_eq!(
                response_encoding(Some("br, gzip, zstd"), None, size),
                ContentEncoding::Identity
            );
        }
    }
}
