//! rmcp `ServerHandler` implementation backed by the mount registry.
//!
//! Catalog requests (`tools/list`, `resources/list`, `prompts/list`,
//! `server/discover`, `get_tool`) are answered from the registry snapshot
//! with zero Python. Execution requests (`tools/call`, `resources/read`,
//! `prompts/get`) dispatch to the registered Python callables on the
//! WorkerLoop. Per-tool guards evaluate in Rust — the single guard
//! implementation for both routes and tools.
//!
//! GIL discipline: Python objects are built inside scoped `Python::attach`
//! blocks; awaits always happen outside them.

use std::collections::BTreeMap;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, DiscoverResult,
    ErrorCode, ErrorData as McpError, GetPromptRequestParams, GetPromptResponse, GetPromptResult,
    InputRequest, InputRequests, InputRequiredResult, ListPromptsResult,
    ListResourceTemplatesResult, ListResourcesResult, ListToolsResult, PaginatedRequestParams,
    ProtocolVersion, ReadResourceRequestParams, ReadResourceResponse, ReadResourceResult,
    ResultType, ServerInfo, Tool,
};
use rmcp::service::RequestContext;
use rmcp::RoleServer;
use serde_json::Value;

use crate::context::{CtxEvent, McpPyContext, ProtocolMode};
use crate::registry::{McpRegistry, McpToolEntry};
use crate::state_codec;
use crate::McpAuthExt;
use bolt_core::middleware::auth::{populate_auth_context, AuthContext};
use bolt_core::permissions::{evaluate_guards, GuardResult};
use bolt_loop as worker_loop;

#[derive(Clone)]
pub struct McpHandler {
    pub registry: Arc<McpRegistry>,
}

/// The authenticated principal for this request, injected by the bridge into
/// the http request extensions (rmcp copies `http::request::Parts` into the
/// MCP request extensions).
fn auth_from_ctx(ctx: &RequestContext<RoleServer>) -> Option<Arc<AuthContext>> {
    ctx.extensions
        .get::<http::request::Parts>()
        .and_then(|parts| parts.extensions.get::<McpAuthExt>())
        .and_then(|ext| ext.0.clone())
}

fn protocol_mode(ctx: &RequestContext<RoleServer>) -> ProtocolMode {
    match ctx.protocol_version() {
        Some(v) if v >= ProtocolVersion::V_2026_07_28 => ProtocolMode::Mrtr,
        _ => ProtocolMode::Live,
    }
}

/// Error envelope Python may return instead of a wire result:
/// {"__error__": {"kind": "...", "message": "..."}}.
#[derive(serde::Deserialize)]
struct PyErrorEnvelope {
    kind: String,
    message: String,
}

/// MRTR envelope Python returns when a tool hit an unanswered input point:
/// {"__inputRequired__": [{"key": ..., "method": ..., "params": {...}}]}.
#[derive(serde::Deserialize)]
struct PyInputRequired {
    key: String,
    method: String,
    params: Value,
}

fn map_py_error(kind: &str, message: String, mode: ProtocolMode) -> McpError {
    match kind {
        // 2026-07-28 aligned resource-not-found to JSON-RPC Invalid Params
        // (-32602); legacy peers still expect the historical -32002.
        "resource_not_found" => match mode {
            ProtocolMode::Mrtr => McpError::new(ErrorCode::INVALID_PARAMS, message, None),
            ProtocolMode::Live => McpError::new(ErrorCode::RESOURCE_NOT_FOUND, message, None),
        },
        "invalid_params" => McpError::new(ErrorCode::INVALID_PARAMS, message, None),
        _ => McpError::new(ErrorCode::INTERNAL_ERROR, message, None),
    }
}

/// Outcome of parsing a Python dispatch result string.
enum PyOutcome<T> {
    Ok(T),
    InputRequired(Vec<PyInputRequired>),
    Error(McpError),
}

fn parse_py_result<T: serde::de::DeserializeOwned>(raw: &str, mode: ProtocolMode) -> PyOutcome<T> {
    // Cheap envelope sniff before typed parsing: envelopes are single-key
    // objects produced by our own dispatch layer.
    if raw.contains("\"__inputRequired__\"") || raw.contains("\"__error__\"") {
        if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(raw) {
            if let Some(items) = map.get("__inputRequired__") {
                match serde_json::from_value::<Vec<PyInputRequired>>(items.clone()) {
                    Ok(items) => return PyOutcome::InputRequired(items),
                    Err(e) => {
                        return PyOutcome::Error(McpError::internal_error(
                            format!("invalid __inputRequired__ envelope: {e}"),
                            None,
                        ))
                    }
                }
            }
            if let Some(err) = map.get("__error__") {
                match serde_json::from_value::<PyErrorEnvelope>(err.clone()) {
                    Ok(env) => return PyOutcome::Error(map_py_error(&env.kind, env.message, mode)),
                    Err(e) => {
                        return PyOutcome::Error(McpError::internal_error(
                            format!("invalid __error__ envelope: {e}"),
                            None,
                        ))
                    }
                }
            }
        }
    }
    match serde_json::from_str::<T>(raw) {
        Ok(value) => PyOutcome::Ok(value),
        Err(e) => PyOutcome::Error(McpError::internal_error(
            format!("MCP dispatch returned invalid result JSON: {e}"),
            None,
        )),
    }
}

fn extract_result_string(result: PyResult<Py<PyAny>>) -> Result<String, McpError> {
    match result {
        Ok(obj) => Python::attach(|py| {
            let bound = obj.bind(py);
            if let Ok(s) = bound.cast::<PyString>() {
                Ok(s.to_string_lossy().into_owned())
            } else if let Ok(b) = bound.extract::<Vec<u8>>() {
                String::from_utf8(b).map_err(|e| {
                    McpError::internal_error(
                        format!("MCP dispatch returned non-UTF8 bytes: {e}"),
                        None,
                    )
                })
            } else {
                Err(McpError::internal_error(
                    "MCP dispatch must return str or bytes JSON",
                    None,
                ))
            }
        }),
        Err(err) => {
            log::error!("MCP Python dispatch raised: {err}");
            Err(McpError::internal_error(
                format!("MCP dispatch failed: {err}"),
                None,
            ))
        }
    }
}

/// Build the single payload dict handed to a Python dispatch callable.
/// Every key is always present (registration-time-guarantee rule).
fn build_payload(py: Python<'_>, fields: &[(&str, Py<PyAny>)]) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    for (key, value) in fields {
        dict.set_item(*key, value)?;
    }
    Ok(dict.into_any().unbind())
}

fn auth_dict(py: Python<'_>, auth: Option<&AuthContext>) -> PyResult<Py<PyAny>> {
    match auth {
        Some(ctx) => {
            let dict: Py<PyDict> = PyDict::new(py).unbind();
            populate_auth_context(&dict, ctx, py);
            Ok(dict.into_any())
        }
        None => Ok(py.None()),
    }
}

impl McpHandler {
    /// Forward one execution request to Python and parse its result.
    async fn dispatch_simple<T: serde::de::DeserializeOwned>(
        &self,
        dispatch: &Py<PyAny>,
        fields: Vec<(&'static str, String)>,
        ctx: &RequestContext<RoleServer>,
    ) -> Result<T, McpError> {
        let mode = protocol_mode(ctx);
        let (callable, payload) = Python::attach(|py| {
            let fields: Vec<(&str, Py<PyAny>)> = fields
                .iter()
                .map(|(k, v)| (*k, PyString::new(py, v).into_any().unbind()))
                .collect();
            let payload = build_payload(py, &fields)?;
            Ok::<_, PyErr>((dispatch.clone_ref(py), payload))
        })
        .map_err(|e| McpError::internal_error(format!("MCP payload build failed: {e}"), None))?;

        let result = worker_loop::dispatch_cancellable(callable, payload, ctx.ct.clone()).await;
        let raw = extract_result_string(result)?;
        match parse_py_result::<T>(&raw, mode) {
            PyOutcome::Ok(value) => Ok(value),
            PyOutcome::Error(err) => Err(err),
            PyOutcome::InputRequired(_) => Err(McpError::internal_error(
                "input_required is only supported for tools/call",
                None,
            )),
        }
    }

    async fn run_tool(
        &self,
        entry: &McpToolEntry,
        params: &CallToolRequestParams,
        ctx: &RequestContext<RoleServer>,
        auth: Option<Arc<AuthContext>>,
    ) -> Result<CallToolResponse, McpError> {
        let mode = protocol_mode(ctx);

        // ---- MRTR state: open sealed state, merge fresh client responses ----
        let mut responses: BTreeMap<String, Value> = BTreeMap::new();
        let is_replay = params.request_state.is_some();
        if let Some(sealed) = &params.request_state {
            match state_codec::open_verified(
                &self.registry.codec,
                sealed,
                &entry.tool.name,
                params.arguments.as_ref(),
                auth.as_deref(),
            ) {
                Ok(state) => responses = state.responses,
                Err(reason) => {
                    // Attacker-controlled input failing verification: in-band
                    // error so the client sees an actionable message.
                    return Ok(CallToolResult::error(vec![ContentBlock::text(reason)]).into());
                }
            }
        }
        if let Some(new_responses) = &params.input_responses {
            for (key, value) in new_responses {
                responses.insert(key.clone(), value.clone());
            }
        }

        let arguments_json = match &params.arguments {
            Some(args) => serde_json::to_string(args).unwrap_or_else(|_| "null".to_string()),
            None => "null".to_string(),
        };

        // ---- Context wiring ----
        let caps = ctx.client_capabilities();
        let (caps_elicitation, caps_sampling) = caps
            .map(|c| (c.elicitation.is_some(), c.sampling.is_some()))
            .unwrap_or((false, false));
        let log_gate = match mode {
            ProtocolMode::Live => Some(0),
            #[allow(deprecated)]
            ProtocolMode::Mrtr => ctx.meta.log_level().map(|level| {
                // Convert via the wire name to avoid depending on enum layout.
                serde_json::to_value(level)
                    .ok()
                    .and_then(|v| v.as_str().and_then(crate::context::level_rank))
                    .unwrap_or(0)
            }),
        };
        let meta_json = serde_json::to_string(&ctx.meta).ok();

        let mut rx = None;
        let (callable, payload) = Python::attach(|py| {
            let ctx_obj: Py<PyAny> = if entry.needs_context {
                let (tx, receiver) = tokio::sync::mpsc::unbounded_channel();
                rx = Some(receiver);
                let replay_map: std::collections::HashMap<String, String> = responses
                    .iter()
                    .map(|(k, v)| (k.clone(), serde_json::json!({ "result": v }).to_string()))
                    .collect();
                Py::new(
                    py,
                    McpPyContext {
                        tx,
                        has_progress_token: ctx.meta.get_progress_token().is_some(),
                        mode,
                        log_gate,
                        caps_elicitation,
                        caps_sampling,
                        input_responses: std::sync::Mutex::new(replay_map),
                        is_replay,
                        meta_json: meta_json.clone(),
                    },
                )?
                .into_any()
            } else {
                py.None()
            };
            let payload = build_payload(
                py,
                &[
                    (
                        "arguments_json",
                        PyString::new(py, &arguments_json).into_any().unbind(),
                    ),
                    ("ctx", ctx_obj),
                    ("auth", auth_dict(py, auth.as_deref())?),
                ],
            )?;
            Ok::<_, PyErr>((entry.dispatch.clone_ref(py), payload))
        })
        .map_err(|e| McpError::internal_error(format!("MCP payload build failed: {e}"), None))?;

        // ---- Run + notification pump ----
        let dispatch_fut = worker_loop::dispatch_cancellable(callable, payload, ctx.ct.clone());
        let result = match rx {
            None => dispatch_fut.await,
            Some(mut rx) => {
                let mut dispatch_fut = std::pin::pin!(dispatch_fut);
                let result = loop {
                    tokio::select! {
                        result = &mut dispatch_fut => break result,
                        event = rx.recv() => {
                            match event {
                                Some(event) => self.pump_event(ctx, event).await,
                                None => break dispatch_fut.await,
                            }
                        }
                    }
                };
                // Eager-start tasks can finish the whole tool before the pump
                // first polls; drain queued events so notifications sent before
                // completion still precede the final response on the stream.
                while let Ok(event) = rx.try_recv() {
                    self.pump_event(ctx, event).await;
                }
                result
            }
        };

        let raw = extract_result_string(result)?;
        match parse_py_result::<CallToolResult>(&raw, mode) {
            PyOutcome::Ok(result) => Ok(result.into()),
            PyOutcome::Error(err) => Err(err),
            PyOutcome::InputRequired(items) => {
                // rmcp rejects InputRequired for legacy peers; Python only
                // raises it in modern mode, so reaching this in Live mode is a
                // bug worth failing loudly on.
                if mode != ProtocolMode::Mrtr {
                    return Err(McpError::internal_error(
                        "input_required produced for a legacy peer",
                        None,
                    ));
                }
                let mut requests: InputRequests = BTreeMap::new();
                for item in items {
                    let request: InputRequest = serde_json::from_value(serde_json::json!({
                        "method": item.method,
                        "params": item.params,
                    }))
                    .map_err(|e| {
                        McpError::internal_error(format!("invalid input request: {e}"), None)
                    })?;
                    requests.insert(item.key, request);
                }
                let state = state_codec::McpRequestState {
                    tool: entry.tool.name.to_string(),
                    args_hash: state_codec::args_hash(params.arguments.as_ref()),
                    principal_hash: state_codec::principal_hash(auth.as_deref()),
                    responses,
                };
                let sealed = state_codec::seal(&self.registry.codec, &state).map_err(|e| {
                    McpError::internal_error(format!("failed to seal requestState: {e}"), None)
                })?;
                Ok(InputRequiredResult::new(Some(requests), Some(sealed)).into())
            }
        }
    }

    /// Forward one context event to the rmcp peer. Notifications ride the
    /// request's own response stream (stateless) or the session stream
    /// (legacy, via OriginatingRequestId) — rmcp handles the routing.
    async fn pump_event(&self, ctx: &RequestContext<RoleServer>, event: CtxEvent) {
        match event {
            CtxEvent::Progress {
                progress,
                total,
                message,
            } => {
                let Some(progress_token) = ctx.meta.get_progress_token() else {
                    return;
                };
                let mut params =
                    rmcp::model::ProgressNotificationParam::new(progress_token, progress);
                if let Some(total) = total {
                    params = params.with_total(total);
                }
                if let Some(message) = message {
                    params = params.with_message(message);
                }
                let _ = ctx.peer.notify_progress(params).await;
            }
            CtxEvent::Log {
                level,
                data_json,
                logger,
            } => {
                #[allow(deprecated)]
                let Ok(level) =
                    serde_json::from_value::<rmcp::model::LoggingLevel>(Value::String(level))
                else {
                    return;
                };
                let data = serde_json::from_str(&data_json).unwrap_or(Value::String(data_json));
                #[allow(deprecated)]
                let mut params = rmcp::model::LoggingMessageNotificationParam::new(level, data);
                if let Some(logger) = logger {
                    params = params.with_logger(logger);
                }
                #[allow(deprecated)]
                let _ = ctx.peer.notify_logging_message(params).await;
            }
            CtxEvent::ServerRequest {
                method,
                params_json,
                reply,
            } => {
                let envelope = self.live_server_request(ctx, method, &params_json).await;
                crate::context::resolve_reply_future(&reply, &envelope);
            }
        }
    }

    /// Live-path server->client request (legacy peers). Returns the JSON
    /// envelope Python's Context expects: {"result": ...} or {"error": {...}}.
    async fn live_server_request(
        &self,
        ctx: &RequestContext<RoleServer>,
        method: &str,
        params_json: &str,
    ) -> String {
        let outcome: Result<Value, String> = match method {
            crate::context::METHOD_ELICIT => {
                match serde_json::from_str::<rmcp::model::ElicitRequestParams>(params_json) {
                    Ok(params) => ctx
                        .peer
                        .create_elicitation(params)
                        .await
                        .map_err(|e| e.to_string())
                        .and_then(|r| serde_json::to_value(r).map_err(|e| e.to_string())),
                    Err(e) => Err(format!("invalid elicitation params: {e}")),
                }
            }
            crate::context::METHOD_SAMPLE => {
                #[allow(deprecated)]
                match serde_json::from_str::<rmcp::model::CreateMessageRequestParams>(params_json) {
                    Ok(params) => ctx
                        .peer
                        .create_message(params)
                        .await
                        .map_err(|e| e.to_string())
                        .and_then(|r| serde_json::to_value(r).map_err(|e| e.to_string())),
                    Err(e) => Err(format!("invalid sampling params: {e}")),
                }
            }
            other => Err(format!("unsupported server request method '{other}'")),
        };
        match outcome {
            Ok(result) => serde_json::json!({ "result": result }).to_string(),
            Err(message) => serde_json::json!({ "error": { "message": message } }).to_string(),
        }
    }
}

impl rmcp::ServerHandler for McpHandler {
    fn get_info(&self) -> ServerInfo {
        self.registry.server_info.clone()
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.registry
            .tools
            .get(name)
            .map(|entry| entry.tool.clone())
    }

    async fn discover(
        &self,
        _context: RequestContext<RoleServer>,
    ) -> Result<DiscoverResult, McpError> {
        let mut result = DiscoverResult::from_server_info(
            rmcp::model::ProtocolVersion::KNOWN_VERSIONS.to_vec(),
            self.get_info(),
        );
        result.ttl_ms = self.registry.list_ttl_ms;
        result.cache_scope = self.registry.list_cache_scope;
        Ok(result)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        let auth = auth_from_ctx(&context);
        Ok(ListToolsResult {
            result_type: Some(ResultType::COMPLETE),
            meta: None,
            next_cursor: None,
            ttl_ms: Some(self.registry.list_ttl_ms),
            cache_scope: Some(self.registry.list_cache_scope),
            tools: self.registry.visible_tools(auth.as_deref()),
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let Some(entry) = self.registry.tools.get(request.name.as_ref()) else {
            // In-band error (parity with bolt-mcp v1): the caller sees the
            // message instead of an opaque protocol error.
            return Ok(CallToolResult::error(vec![ContentBlock::text(format!(
                "Unknown tool: {}",
                request.name
            ))])
            .into());
        };
        let auth = auth_from_ctx(&context);
        if !entry.guards.is_empty() {
            match evaluate_guards(&entry.guards, auth.as_deref()) {
                GuardResult::Allow => {}
                GuardResult::Unauthorized | GuardResult::Forbidden(_) => {
                    return Ok(CallToolResult::error(vec![ContentBlock::text(format!(
                        "Permission denied for tool '{}'",
                        request.name
                    ))])
                    .into());
                }
            }
        }
        self.run_tool(entry, &request, &context, auth).await
    }

    async fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourcesResult, McpError> {
        Ok(ListResourcesResult {
            result_type: Some(ResultType::COMPLETE),
            meta: None,
            next_cursor: None,
            ttl_ms: Some(self.registry.list_ttl_ms),
            cache_scope: Some(self.registry.list_cache_scope),
            resources: self.registry.resources.clone(),
        })
    }

    async fn list_resource_templates(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourceTemplatesResult, McpError> {
        Ok(ListResourceTemplatesResult {
            result_type: Some(ResultType::COMPLETE),
            meta: None,
            next_cursor: None,
            ttl_ms: Some(self.registry.list_ttl_ms),
            cache_scope: Some(self.registry.list_cache_scope),
            resource_templates: self.registry.resource_templates.clone(),
        })
    }

    async fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<ReadResourceResponse, McpError> {
        let dispatch = Python::attach(|py| self.registry.resource_dispatch.clone_ref(py));
        let mut result: ReadResourceResult = self
            .dispatch_simple(&dispatch, vec![("uri", request.uri.clone())], &context)
            .await?;
        if result.ttl_ms.is_none() {
            result.ttl_ms = Some(self.registry.list_ttl_ms);
        }
        if result.cache_scope.is_none() {
            result.cache_scope = Some(self.registry.list_cache_scope);
        }
        Ok(result.into())
    }

    async fn list_prompts(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListPromptsResult, McpError> {
        Ok(ListPromptsResult {
            result_type: Some(ResultType::COMPLETE),
            meta: None,
            next_cursor: None,
            ttl_ms: Some(self.registry.list_ttl_ms),
            cache_scope: Some(self.registry.list_cache_scope),
            prompts: self.registry.prompts.clone(),
        })
    }

    async fn get_prompt(
        &self,
        request: GetPromptRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<GetPromptResponse, McpError> {
        let arguments_json = match &request.arguments {
            Some(args) => serde_json::to_string(args).unwrap_or_else(|_| "null".to_string()),
            None => "null".to_string(),
        };
        let dispatch = Python::attach(|py| self.registry.prompt_dispatch.clone_ref(py));
        let result: GetPromptResult = self
            .dispatch_simple(
                &dispatch,
                vec![
                    ("name", request.name.to_string()),
                    ("arguments_json", arguments_json),
                ],
                &context,
            )
            .await?;
        Ok(result.into())
    }
}
