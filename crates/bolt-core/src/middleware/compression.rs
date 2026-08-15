use actix_http::encoding::Encoder;
/// Custom compression middleware that respects Content-Encoding: identity header
/// and CompressionConfig settings.
///
/// This middleware checks response headers BEFORE applying compression.
/// If Content-Encoding: identity is present (set when skip_compression=true),
/// it removes the header and returns the response uncompressed.
/// Otherwise, it applies compression based on CompressionConfig and Accept-Encoding.
use actix_web::{
    body::{BodySize, MessageBody},
    dev::{forward_ready, Service, ServiceRequest, ServiceResponse, Transform},
    http::header::{ContentEncoding, ACCEPT_ENCODING, CONTENT_ENCODING, VARY},
    Error,
};
use futures_util::future::LocalBoxFuture;
use std::future::{ready, Ready};
use std::sync::Arc;

use crate::metadata::CompressionConfig;
use crate::state::AppState;
use crate::streaming_compression::accepts_encoding;

/// Compression middleware factory
pub struct CompressionMiddleware;

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

impl<S, B> Service<ServiceRequest> for CompressionMiddlewareService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<Encoder<B>>;
    type Error = Error;
    type Future = LocalBoxFuture<'static, Result<Self::Response, Self::Error>>;

    forward_ready!(service);

    fn call(&self, req: ServiceRequest) -> Self::Future {
        // Store Accept-Encoding header from request for later use
        let accept_encoding = req
            .headers()
            .get(ACCEPT_ENCODING)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());

        // Get compression config from app state
        let compression_config = req
            .app_data::<actix_web::web::Data<Arc<AppState>>>()
            .and_then(|state| state.global_compression_config.clone());

        let fut = self.service.call(req);

        Box::pin(async move {
            let res = fut.await?;

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
                    response.map_body(|head, body| {
                        Encoder::response(ContentEncoding::Identity, head, body)
                    }),
                ));
            }

            // Apply compression based on CompressionConfig and Accept-Encoding
            let (req, mut response) = res.into_parts();

            // Select encoding based on config and client support
            let encoding =
                select_encoding(accept_encoding.as_deref(), compression_config.as_deref());

            // Get minimum size from config or use default
            let minimum_size = compression_config
                .as_ref()
                .map(|c| c.minimum_size)
                .unwrap_or(500);

            // Check if response size warrants compression (skip small responses)
            let should_compress = match response.body().size() {
                BodySize::None => encoding != ContentEncoding::Identity,
                BodySize::Sized(size) => {
                    size >= minimum_size as u64 && encoding != ContentEncoding::Identity
                }
                _ => encoding != ContentEncoding::Identity,
            };

            if should_compress {
                // Add Vary header to indicate content varies by Accept-Encoding
                // Use append (not insert) to preserve existing Vary headers like CORS's Vary: Origin
                response.headers_mut().append(
                    VARY,
                    actix_web::http::header::HeaderValue::from_static("accept-encoding"),
                );

                // Create encoder with selected encoding
                Ok(ServiceResponse::new(
                    req,
                    response.map_body(|head, body| Encoder::response(encoding, head, body)),
                ))
            } else {
                // No compression needed
                Ok(ServiceResponse::new(
                    req,
                    response.map_body(|head, body| {
                        Encoder::response(ContentEncoding::Identity, head, body)
                    }),
                ))
            }
        })
    }
}

#[cfg(test)]
mod bypass_tests {
    use super::*;
    use actix_web::http::header::{HeaderName, HeaderValue};
    use actix_web::test::{self, TestRequest};
    use actix_web::{web, App, HttpResponse};

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
        let mut c = CompressionConfig::default();
        c.backend = "gzip".to_string();
        c
    }

    fn cfg_zstd() -> CompressionConfig {
        let mut c = CompressionConfig::default();
        c.backend = "zstd".to_string();
        c
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
}
