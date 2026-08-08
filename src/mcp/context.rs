//! Per-call context bridge between Python tool code and the rmcp peer.
//!
//! A `McpPyContext` is created per `tools/call` (when the tool declared a
//! Context parameter) and handed into the Python dispatch payload. Python's
//! `bolt_mcp.Context` wraps it. All methods are sync sends on an unbounded
//! channel; the notification pump inside `call_tool` (see `handler.rs`)
//! forwards events to the rmcp peer so they ride the request's own SSE
//! stream (stateless) or the session stream (legacy).

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use std::collections::HashMap;
use std::sync::Mutex;

/// Which interaction model this request uses for server->client input.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ProtocolMode {
    /// Legacy session peer (< 2026-07-28): live server-initiated requests.
    Live,
    /// Modern stateless peer: MRTR (input_required + replay).
    Mrtr,
}

pub enum CtxEvent {
    Progress {
        progress: f64,
        total: Option<f64>,
        message: Option<String>,
    },
    Log {
        /// MCP level name ("debug".."emergency"); converted to the (deprecated)
        /// rmcp LoggingLevel only at the peer boundary.
        level: String,
        data_json: String,
        logger: Option<String>,
    },
    /// Live-mode server->client request (elicitation/create or
    /// sampling/createMessage). `reply` is an asyncio Future created by the
    /// tool coroutine on the WorkerLoop; the pump resolves it thread-safely
    /// with a JSON envelope: {"result": ...} or {"error": {...}}.
    ServerRequest {
        method: &'static str,
        params_json: String,
        reply: Py<PyAny>,
    },
}

pub const METHOD_ELICIT: &str = "elicitation/create";
pub const METHOD_SAMPLE: &str = "sampling/createMessage";

/// MCP logging level rank (RFC 5424 subset), most verbose = 0.
pub fn level_rank(level: &str) -> Option<u8> {
    match level {
        "debug" => Some(0),
        "info" => Some(1),
        "notice" => Some(2),
        "warning" => Some(3),
        "error" => Some(4),
        "critical" => Some(5),
        "alert" => Some(6),
        "emergency" => Some(7),
        _ => None,
    }
}

#[pyclass(frozen, name = "McpCallContext", module = "django_bolt._core")]
pub struct McpPyContext {
    pub(crate) tx: tokio::sync::mpsc::UnboundedSender<CtxEvent>,
    pub(crate) has_progress_token: bool,
    pub(crate) mode: ProtocolMode,
    /// Per-request log gate as a rank (see `level_rank`): modern era = the
    /// request's `io.modelcontextprotocol/logLevel` (None -> never emit);
    /// legacy era = Some(0) (session logging, no gate).
    pub(crate) log_gate: Option<u8>,
    pub(crate) caps_elicitation: bool,
    pub(crate) caps_sampling: bool,
    /// MRTR replay answers, keyed by input key; values are JSON envelopes
    /// ({"result": <client result>}), matching the live-path reply format.
    pub(crate) input_responses: Mutex<HashMap<String, String>>,
    /// Whether this invocation is an MRTR replay (requestState was present).
    pub(crate) is_replay: bool,
    /// Raw request `_meta` JSON for Python-side passthrough.
    pub(crate) meta_json: Option<String>,
}

#[pymethods]
impl McpPyContext {
    /// "legacy" or "modern" — selects the Python-side elicit/sample path.
    #[getter]
    fn era(&self) -> &'static str {
        match self.mode {
            ProtocolMode::Live => "legacy",
            ProtocolMode::Mrtr => "modern",
        }
    }

    #[getter]
    fn is_replay(&self) -> bool {
        self.is_replay
    }

    #[getter]
    fn has_progress_token(&self) -> bool {
        self.has_progress_token
    }

    #[getter]
    fn supports_elicitation(&self) -> bool {
        self.caps_elicitation
    }

    #[getter]
    fn supports_sampling(&self) -> bool {
        self.caps_sampling
    }

    #[getter]
    fn meta_json(&self) -> Option<&str> {
        self.meta_json.as_deref()
    }

    /// True when a log at `level` would actually be emitted for this request.
    /// Lets Python skip serializing log data that Rust would drop.
    fn wants_log(&self, level: &str) -> bool {
        let Some(rank) = level_rank(level) else {
            return false;
        };
        match self.log_gate {
            Some(gate) => rank >= gate,
            None => false,
        }
    }

    /// Emit notifications/progress for this request. Dropped (no-op) when the
    /// client sent no progressToken, per spec.
    fn notify_progress(&self, progress: f64, total: Option<f64>, message: Option<String>) {
        if !self.has_progress_token {
            return;
        }
        let _ = self.tx.send(CtxEvent::Progress {
            progress,
            total,
            message,
        });
    }

    /// Emit notifications/message. Enforced against the per-request gate here
    /// as well — `wants_log` is only an optimization hint for Python.
    fn notify_log(&self, level: &str, data_json: String, logger: Option<String>) {
        if !self.wants_log(level) {
            return;
        }
        let _ = self.tx.send(CtxEvent::Log {
            level: level.to_string(),
            data_json,
            logger,
        });
    }

    /// MRTR replay lookup: the JSON envelope for `key` if the client already
    /// answered it on a previous round trip.
    fn input_response(&self, key: &str) -> Option<String> {
        self.input_responses.lock().unwrap().get(key).cloned()
    }

    /// Live path (legacy peers): send a server->client request and resolve
    /// `reply_future` (an asyncio Future created by the calling coroutine on
    /// the WorkerLoop) with the JSON envelope when the client responds.
    fn server_request_send(
        &self,
        kind: &str,
        params_json: String,
        reply_future: Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let method = match kind {
            "elicitation" => METHOD_ELICIT,
            "sampling" => METHOD_SAMPLE,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown server request kind '{other}'"
                )))
            }
        };
        self.tx
            .send(CtxEvent::ServerRequest {
                method,
                params_json,
                reply: reply_future.unbind(),
            })
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "MCP request finished; context is no longer usable",
                )
            })
    }

}

static MCP_RESOLVE_FUTURE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Resolve an asyncio Future from a non-loop thread with a JSON string, via
/// the future's own loop (`call_soon_threadsafe`). The Python-side helper
/// checks `fut.done()` so a cancelled request does not raise
/// InvalidStateError into the loop's exception handler. Errors are logged and
/// swallowed: the tool coroutine may already be gone.
pub fn resolve_reply_future(reply: &Py<PyAny>, envelope_json: &str) {
    Python::attach(|py| {
        let result: PyResult<()> = (|| {
            let resolve = MCP_RESOLVE_FUTURE.get_or_try_init(py, || {
                Ok::<_, PyErr>(
                    py.import("django_bolt._worker_loop")?
                        .getattr("mcp_resolve_future")?
                        .unbind(),
                )
            })?;
            let fut = reply.bind(py);
            let fut_loop = fut.call_method0("get_loop")?;
            fut_loop.call_method1("call_soon_threadsafe", (resolve, fut, envelope_json))?;
            Ok(())
        })();
        if let Err(err) = result {
            log::warn!("failed to resolve MCP reply future: {err}");
        }
    });
}
