//! Per-chunk compression for streaming responses.
//!
//! Each codec wraps an upstream chunk stream and compresses each chunk
//! independently with a per-chunk sync flush so chunks still arrive at the
//! client one at a time (preserving SSE event boundaries and chunked
//! streaming semantics in general). One encoder runs per connection so
//! cross-chunk dictionary reuse still benefits compression ratio.
//!
//! Codec selection mirrors `select_encoding` in `src/middleware/compression.rs`:
//! preferred backend if the client accepts it, otherwise gzip fallback (if
//! enabled), otherwise no compression.

use actix_web::http::header::ACCEPT_ENCODING;
use actix_web::web::Bytes;
use actix_web::HttpRequest;
use brotli::CompressorWriter;
use futures_util::Stream;
use std::io::Write;
use std::pin::Pin;
use std::task::{Context, Poll};

use crate::metadata::CompressionConfig;

/// Which codec to apply to a stream, plus the codec-specific tuning params.
#[derive(Debug, Clone, Copy)]
pub enum StreamCodec {
    Brotli { quality: u32, lgwin: u32 },
    Gzip { level: u32 },
    Zstd { level: u32 },
}

impl StreamCodec {
    /// HTTP `Content-Encoding` token for this codec.
    pub fn header_name(self) -> &'static str {
        match self {
            StreamCodec::Brotli { .. } => "br",
            StreamCodec::Gzip { .. } => "gzip",
            StreamCodec::Zstd { .. } => "zstd",
        }
    }

    fn build_encoder(self) -> Box<dyn PerChunkEncoder> {
        match self {
            StreamCodec::Brotli { quality, lgwin } => Box::new(BrotliEncoder::new(quality, lgwin)),
            StreamCodec::Gzip { level } => Box::new(GzipEncoder::new(level)),
            StreamCodec::Zstd { level } => Box::new(ZstdEncoder::new(level)),
        }
    }
}

/// Negotiate the streaming codec for this request, honoring `@no_compress`
/// and falling back to gzip if the configured backend isn't accepted.
///
/// Returns `None` when:
/// - `skip_compression` is set (route opted out via `@no_compress`)
/// - the API has no `CompressionConfig`
/// - the client's `Accept-Encoding` matches no available codec
pub fn select_stream_encoding(
    req: &HttpRequest,
    config: Option<&CompressionConfig>,
    skip_compression: bool,
) -> Option<StreamCodec> {
    if skip_compression {
        return None;
    }
    let cfg = config?;
    let ae = req
        .headers()
        .get(ACCEPT_ENCODING)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");

    if let Some(codec) = codec_for_backend(&cfg.backend, cfg) {
        if accepts_encoding(ae, codec.header_name()) {
            return Some(codec);
        }
    }
    if cfg.gzip_fallback && accepts_encoding(ae, "gzip") {
        return Some(StreamCodec::Gzip { level: cfg.gzip_level });
    }
    None
}

fn codec_for_backend(backend: &str, cfg: &CompressionConfig) -> Option<StreamCodec> {
    match backend {
        "brotli" => Some(StreamCodec::Brotli {
            quality: cfg.brotli_level,
            lgwin: cfg.brotli_lgwin,
        }),
        "gzip" => Some(StreamCodec::Gzip { level: cfg.gzip_level }),
        "zstd" => Some(StreamCodec::Zstd { level: cfg.zstd_level }),
        _ => None,
    }
}

/// Zero-alloc check: does `Accept-Encoding` accept `name` with non-zero q?
/// Treats `*` as accepting any unmentioned coding (but `*;q=0` rejects
/// unmentioned codings). Case-insensitive on the coding name.
///
/// Per RFC 7231 §5.3.4: weight defaults to 1.0; q=0 means "not acceptable."
pub fn accepts_encoding(header: &str, name: &str) -> bool {
    let mut star_q: Option<f32> = None;
    let mut explicit_q: Option<f32> = None;
    for entry in header.split(',') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        let mut pieces = entry.split(';');
        let coding = pieces.next().unwrap_or("").trim();
        let mut q: f32 = 1.0;
        for piece in pieces {
            let piece = piece.trim();
            if let Some(rest) = piece.strip_prefix("q=").or_else(|| piece.strip_prefix("Q=")) {
                if let Ok(parsed) = rest.parse::<f32>() {
                    q = parsed;
                }
            }
        }
        if coding.eq_ignore_ascii_case(name) {
            explicit_q = Some(q);
        } else if coding == "*" {
            star_q = Some(q);
        }
    }
    match (explicit_q, star_q) {
        (Some(q), _) => q > 0.0,
        (None, Some(q)) => q > 0.0,
        (None, None) => false,
    }
}

// ─── Generic stream adapter ───────────────────────────────────────────────

/// Codec-agnostic per-chunk encoder. Implementors hold one encoder per
/// connection and emit a self-contained block per `write_chunk` call.
trait PerChunkEncoder: Send {
    /// Compress one chunk and flush. The returned bytes are a self-contained
    /// block the client decoder can surface immediately.
    fn write_chunk(&mut self, input: &[u8]) -> std::io::Result<Vec<u8>>;
    /// Called once after the inner stream ends. Returns any tail bytes
    /// (FINISH frame, gzip trailer, zstd epilogue).
    fn finish(self: Box<Self>) -> std::io::Result<Vec<u8>>;
}

/// In-memory sink shared by all three encoder impls. After each per-chunk
/// flush we swap in a fresh 4 KiB-capacity `Vec` and hand the old one to
/// the downstream stream — this keeps the encoder writing into a steadily-
/// sized buffer instead of regrowing from zero every event.
struct SinkWriter {
    buf: Vec<u8>,
}

impl SinkWriter {
    fn new() -> Self {
        SinkWriter { buf: Vec::with_capacity(SINK_BUF_CAPACITY) }
    }

    /// Yield the current buffer, leaving a fresh 4 KiB-capacity `Vec` in place.
    fn drain(&mut self) -> Vec<u8> {
        std::mem::replace(&mut self.buf, Vec::with_capacity(SINK_BUF_CAPACITY))
    }
}

/// 4 KiB sink buffer — matches brotli reference impl, plenty for typical
/// SSE events. Used at construction and on every per-chunk drain.
const SINK_BUF_CAPACITY: usize = 4096;

impl Write for SinkWriter {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.buf.extend_from_slice(b);
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Stream adapter that runs `inner` chunks through a per-chunk encoder.
///
/// `encoder` doubles as a "is the stream still alive?" flag: it's `take()`-en
/// at finalization, and any subsequent poll short-circuits to `Ready(None)`.
/// This matters because the upstream Python SSE path uses
/// `futures_util::stream::unfold`, which panics if polled after exhaustion.
pub struct EncoderStream<S> {
    inner: S,
    encoder: Option<Box<dyn PerChunkEncoder>>,
}

impl<S> EncoderStream<S> {
    pub fn new(inner: S, codec: StreamCodec) -> Self {
        EncoderStream {
            inner,
            encoder: Some(codec.build_encoder()),
        }
    }
}

impl<S> Stream for EncoderStream<S>
where
    S: Stream<Item = Result<Bytes, std::io::Error>> + Send + Unpin,
{
    type Item = Result<Bytes, std::io::Error>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        if self.encoder.is_none() {
            return Poll::Ready(None);
        }
        loop {
            match Pin::new(&mut self.inner).poll_next(cx) {
                Poll::Ready(Some(Ok(chunk))) => {
                    let enc = self.encoder.as_mut().expect("encoder present");
                    let out = match enc.write_chunk(&chunk) {
                        Ok(b) => b,
                        Err(e) => return Poll::Ready(Some(Err(e))),
                    };
                    if out.is_empty() {
                        // Encoder consumed input without producing output —
                        // pull the next inner chunk rather than yielding
                        // an empty Bytes downstream.
                        continue;
                    }
                    return Poll::Ready(Some(Ok(Bytes::from(out))));
                }
                Poll::Ready(Some(Err(e))) => return Poll::Ready(Some(Err(e))),
                Poll::Ready(None) => {
                    let enc = self.encoder.take().expect("encoder present");
                    match enc.finish() {
                        Ok(tail) if !tail.is_empty() => {
                            return Poll::Ready(Some(Ok(Bytes::from(tail))));
                        }
                        Ok(_) => return Poll::Ready(None),
                        Err(e) => return Poll::Ready(Some(Err(e))),
                    }
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

// ─── Brotli encoder ───────────────────────────────────────────────────────

struct BrotliEncoder {
    inner: CompressorWriter<SinkWriter>,
}

impl BrotliEncoder {
    fn new(quality: u32, lgwin: u32) -> Self {
        BrotliEncoder {
            inner: CompressorWriter::new(SinkWriter::new(), SINK_BUF_CAPACITY, quality, lgwin),
        }
    }
}

impl PerChunkEncoder for BrotliEncoder {
    fn write_chunk(&mut self, input: &[u8]) -> std::io::Result<Vec<u8>> {
        self.inner.write_all(input)?;
        self.inner.flush()?; // BROTLI_OPERATION_FLUSH
        Ok(self.inner.get_mut().drain())
    }

    fn finish(self: Box<Self>) -> std::io::Result<Vec<u8>> {
        // CompressorWriter::into_inner runs BROTLI_OPERATION_FINISH.
        let sink = (*self).inner.into_inner();
        Ok(sink.buf)
    }
}

// ─── Gzip encoder ─────────────────────────────────────────────────────────

struct GzipEncoder {
    inner: flate2::write::GzEncoder<SinkWriter>,
}

impl GzipEncoder {
    fn new(level: u32) -> Self {
        GzipEncoder {
            inner: flate2::write::GzEncoder::new(
                SinkWriter::new(),
                flate2::Compression::new(level),
            ),
        }
    }
}

impl PerChunkEncoder for GzipEncoder {
    fn write_chunk(&mut self, input: &[u8]) -> std::io::Result<Vec<u8>> {
        self.inner.write_all(input)?;
        self.inner.flush()?; // Z_SYNC_FLUSH — emits a decodable sync marker
        Ok(self.inner.get_mut().drain())
    }

    fn finish(self: Box<Self>) -> std::io::Result<Vec<u8>> {
        // GzEncoder::finish writes the final block + gzip trailer.
        let sink = (*self).inner.finish()?;
        Ok(sink.buf)
    }
}

// ─── Zstd encoder ─────────────────────────────────────────────────────────

struct ZstdEncoder {
    inner: zstd::stream::write::Encoder<'static, SinkWriter>,
}

impl ZstdEncoder {
    fn new(level: u32) -> Self {
        let enc = zstd::stream::write::Encoder::new(SinkWriter::new(), level as i32)
            .expect("zstd encoder init");
        ZstdEncoder { inner: enc }
    }
}

impl PerChunkEncoder for ZstdEncoder {
    fn write_chunk(&mut self, input: &[u8]) -> std::io::Result<Vec<u8>> {
        self.inner.write_all(input)?;
        // zstd's Write::flush emits a flush block; decoders surface bytes
        // up to that point.
        self.inner.flush()?;
        Ok(self.inner.get_mut().drain())
    }

    fn finish(self: Box<Self>) -> std::io::Result<Vec<u8>> {
        let sink = (*self).inner.finish()?;
        Ok(sink.buf)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use brotli::Decompressor;
    use flate2::read::GzDecoder;
    use futures_util::stream;
    use futures_util::StreamExt;
    use std::io::Read;

    async fn collect_compressed(codec: StreamCodec, events: Vec<&'static [u8]>) -> Vec<Bytes> {
        let items: Vec<Result<Bytes, std::io::Error>> =
            events.into_iter().map(|b| Ok(Bytes::from_static(b))).collect();
        let inner = stream::iter(items);
        let mut wrapped = EncoderStream::new(inner, codec);
        let mut out = Vec::new();
        while let Some(chunk) = wrapped.next().await {
            out.push(chunk.expect("chunk"));
        }
        out
    }

    fn concat(chunks: &[Bytes]) -> Vec<u8> {
        let mut v = Vec::new();
        for c in chunks {
            v.extend_from_slice(c);
        }
        v
    }

    // ─── Brotli ───────────────────────────────────────────────────────

    #[tokio::test]
    async fn brotli_roundtrip_decodes_to_concatenated_input() {
        let chunks = collect_compressed(
            StreamCodec::Brotli { quality: 5, lgwin: 18 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        let compressed = concat(&chunks);
        let mut decoded = Vec::new();
        Decompressor::new(&compressed[..], 4096).read_to_end(&mut decoded).unwrap();
        assert_eq!(decoded, b"data: one\n\ndata: two\n\ndata: three\n\n");
    }

    #[tokio::test]
    async fn brotli_emits_per_event_chunks() {
        let chunks = collect_compressed(
            StreamCodec::Brotli { quality: 5, lgwin: 18 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        assert!(chunks.len() >= 3, "expected per-event flush, got {} chunks", chunks.len());
    }

    #[tokio::test]
    async fn brotli_empty_stream() {
        let chunks = collect_compressed(StreamCodec::Brotli { quality: 5, lgwin: 18 }, vec![]).await;
        let compressed = concat(&chunks);
        let mut decoded = Vec::new();
        Decompressor::new(&compressed[..], 4096).read_to_end(&mut decoded).unwrap();
        assert!(decoded.is_empty());
    }

    // ─── Gzip ─────────────────────────────────────────────────────────

    #[tokio::test]
    async fn gzip_roundtrip_decodes_to_concatenated_input() {
        let chunks = collect_compressed(
            StreamCodec::Gzip { level: 6 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        let compressed = concat(&chunks);
        let mut decoded = Vec::new();
        GzDecoder::new(&compressed[..]).read_to_end(&mut decoded).unwrap();
        assert_eq!(decoded, b"data: one\n\ndata: two\n\ndata: three\n\n");
    }

    #[tokio::test]
    async fn gzip_emits_per_event_chunks() {
        let chunks = collect_compressed(
            StreamCodec::Gzip { level: 6 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        assert!(chunks.len() >= 3, "expected per-event flush, got {} chunks", chunks.len());
    }

    // ─── Zstd ─────────────────────────────────────────────────────────

    #[tokio::test]
    async fn zstd_roundtrip_decodes_to_concatenated_input() {
        let chunks = collect_compressed(
            StreamCodec::Zstd { level: 3 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        let compressed = concat(&chunks);
        let decoded = zstd::stream::decode_all(&compressed[..]).unwrap();
        assert_eq!(decoded, b"data: one\n\ndata: two\n\ndata: three\n\n");
    }

    #[tokio::test]
    async fn zstd_emits_per_event_chunks() {
        let chunks = collect_compressed(
            StreamCodec::Zstd { level: 3 },
            vec![b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"],
        )
        .await;
        assert!(chunks.len() >= 3, "expected per-event flush, got {} chunks", chunks.len());
    }

    // ─── Accept-Encoding parser ────────────────────────────────────────

    #[test]
    fn accepts_br_plain() {
        assert!(accepts_encoding("br", "br"));
        assert!(accepts_encoding("gzip, br", "br"));
        assert!(accepts_encoding("br, gzip", "br"));
    }

    #[test]
    fn accepts_br_with_qvalue() {
        assert!(accepts_encoding("br;q=0.5", "br"));
        assert!(accepts_encoding("gzip;q=0.8, br;q=0.5", "br"));
        assert!(!accepts_encoding("br;q=0", "br"));
    }

    #[test]
    fn accepts_capitalized_coding() {
        assert!(accepts_encoding("BR", "br"));
        assert!(accepts_encoding("Br;Q=0.5", "br"));
    }

    #[test]
    fn star_with_positive_q_accepts() {
        assert!(accepts_encoding("*", "br"));
        assert!(accepts_encoding("gzip, *", "br"));
    }

    #[test]
    fn star_q0_rejects_unmentioned() {
        assert!(!accepts_encoding("gzip, *;q=0", "br"));
        assert!(accepts_encoding("gzip, *;q=0", "gzip"));
    }

    #[test]
    fn explicit_q0_overrides_star() {
        // `br;q=0, *` — br is explicitly rejected even though * is generous.
        assert!(!accepts_encoding("br;q=0, *", "br"));
    }

    #[test]
    fn missing_or_empty_header_rejects() {
        assert!(!accepts_encoding("", "br"));
        assert!(!accepts_encoding("gzip", "br"));
    }

    #[test]
    fn malformed_qvalue_falls_back_to_one() {
        // Unparseable q= ⇒ q stays at the default 1.0 ⇒ coding is accepted.
        assert!(accepts_encoding("br;q=abc", "br"));
    }

    // ─── select_stream_encoding ────────────────────────────────────────

    fn req_with_ae(ae: &str) -> HttpRequest {
        actix_web::test::TestRequest::default()
            .insert_header(("accept-encoding", ae))
            .to_http_request()
    }

    #[test]
    fn select_returns_none_when_skip_compression() {
        let cfg = CompressionConfig::default();
        let r = req_with_ae("br");
        assert!(select_stream_encoding(&r, Some(&cfg), true).is_none());
    }

    #[test]
    fn select_returns_none_when_no_config() {
        let r = req_with_ae("br");
        assert!(select_stream_encoding(&r, None, false).is_none());
    }

    #[test]
    fn select_picks_preferred_backend() {
        let cfg = CompressionConfig::default(); // brotli
        let r = req_with_ae("br, gzip");
        match select_stream_encoding(&r, Some(&cfg), false).unwrap() {
            StreamCodec::Brotli { .. } => {}
            other => panic!("expected Brotli, got {:?}", other),
        }
    }

    #[test]
    fn select_falls_back_to_gzip() {
        let cfg = CompressionConfig::default(); // brotli, gzip_fallback=true
        let r = req_with_ae("gzip"); // no br
        match select_stream_encoding(&r, Some(&cfg), false).unwrap() {
            StreamCodec::Gzip { .. } => {}
            other => panic!("expected Gzip, got {:?}", other),
        }
    }

    #[test]
    fn select_skips_fallback_when_disabled() {
        let mut cfg = CompressionConfig::default();
        cfg.gzip_fallback = false;
        let r = req_with_ae("gzip");
        assert!(select_stream_encoding(&r, Some(&cfg), false).is_none());
    }

    #[test]
    fn select_returns_none_when_no_negotiable() {
        let cfg = CompressionConfig::default();
        let r = req_with_ae("deflate, identity");
        assert!(select_stream_encoding(&r, Some(&cfg), false).is_none());
    }

    #[test]
    fn select_picks_zstd_when_configured() {
        let mut cfg = CompressionConfig::default();
        cfg.backend = "zstd".to_string();
        let r = req_with_ae("zstd, gzip");
        match select_stream_encoding(&r, Some(&cfg), false).unwrap() {
            StreamCodec::Zstd { .. } => {}
            other => panic!("expected Zstd, got {:?}", other),
        }
    }
}
