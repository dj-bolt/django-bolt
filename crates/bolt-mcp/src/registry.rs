//! MCP mount registry: everything the server needs to answer catalog
//! requests (`tools/list`, `server/discover`, `resources/list`,
//! `prompts/list`, `get_tool`) with zero Python involvement.
//!
//! Python (`bolt_mcp`) compiles its decorator registries into one wire-format
//! JSON catalog plus a small config dict at mount time. Rust parses both once
//! at registration; the request path only reads the resulting structures.

use std::collections::HashMap;

use ahash::AHashMap;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use rmcp::model::{
    CacheScope, Implementation, Prompt, ProtocolVersion, RequestStateCodec, Resource,
    ResourceTemplate, ServerCapabilities, ServerConfig, Tool,
};
use serde::Deserialize;

use bolt_core::metadata::{parse_auth_backend, parse_guard};
use bolt_core::middleware::auth::{AuthBackend, AuthContext};
use bolt_core::permissions::{Guard, GuardSet};
use bolt_core::validation::{AuthGuardResult, cookie_csrf_blocks, validate_auth_and_guards};

use crate::McpRouteRequest;
use crate::tasks::{TaskOptions, TaskStore};

/// The auth and the guards of an exposed route. Each call of its tool runs
/// them on the MCP request, as an HTTP request to the route runs them.
pub struct RouteGate {
    pub auth_backends: Vec<AuthBackend>,
    pub guards: GuardSet,
}

impl RouteGate {
    /// The principal that the auth of the route gives for this request.
    /// Returns None when the route refuses the request.
    pub fn check(&self, request: Option<&McpRouteRequest>) -> Option<Option<AuthContext>> {
        // The bridge adds the request for each mount with a gate. Fail closed without it.
        let request = request?;
        match validate_auth_and_guards(&request.headers, &self.auth_backends, &self.guards) {
            AuthGuardResult::Allow(auth) => {
                let csrf = auth.as_ref().is_some_and(|auth| auth.cookie_csrf);
                if csrf
                    && cookie_csrf_blocks(&request.method, &request.headers, true, &request.scheme)
                {
                    return None;
                }
                Some(auth)
            }
            // Django middleware does not run for a tool call, so no code can
            // check the guards of a session request.
            AuthGuardResult::Deferred(_)
            | AuthGuardResult::Unauthorized
            | AuthGuardResult::Forbidden(_) => None,
        }
    }
}

/// One tool entry: prebuilt wire model + Rust-evaluated guards + the Python
/// callable that executes it.
pub struct McpToolEntry {
    pub tool: Tool,
    pub guards: GuardSet,
    /// Async Python callable taking one payload dict, returning result JSON.
    pub dispatch: Py<PyAny>,
    /// Whether the tool declared a `Context` parameter (progress/log/elicit).
    pub needs_context: bool,
    /// The auth and the guards of the route, for a tool that exposes a route.
    pub route_gate: Option<RouteGate>,
    /// Whether the tool takes the request, or a `Context` that holds it.
    /// Only then does a call build the request object.
    pub needs_request: bool,
    /// The task settings of a tool with `task=True` (SEP-2663).
    pub task: Option<TaskOptions>,
}

pub struct McpRegistry {
    pub server_info: ServerConfig,
    /// Insertion order of tools for deterministic `tools/list` output
    /// (spec SHOULD: stable ordering improves client prompt-cache hits).
    pub tool_order: Vec<String>,
    pub tools: AHashMap<String, McpToolEntry>,
    pub resources: Vec<Resource>,
    pub resource_templates: Vec<ResourceTemplate>,
    pub prompts: Vec<Prompt>,
    /// Async Python callable: payload dict {"uri": str, ...} -> ReadResourceResult JSON.
    pub resource_dispatch: Py<PyAny>,
    /// Async Python callable: payload dict {"name": str, "arguments_json": str} -> GetPromptResult JSON.
    pub prompt_dispatch: Py<PyAny>,
    /// Whether resource middleware exists. Without it, a `resources/read`
    /// sends no request `_meta` and no principal to Python.
    pub resource_middleware: bool,
    /// Whether prompt middleware exists. Without it, a `prompts/get`
    /// sends no request `_meta` and no principal to Python.
    pub prompt_middleware: bool,
    /// HMAC codec for MRTR requestState (key derived from SECRET_KEY in Python).
    pub codec: RequestStateCodec,
    /// Freshness hint applied to all list results (SEP-2549).
    pub list_ttl_ms: u64,
    pub list_cache_scope: CacheScope,
    /// Whether a tool has a `route_gate`.
    pub route_gates: bool,
    /// Whether a tool takes the request.
    pub needs_request: bool,
    /// The tasks of the mount. Present when a tool has `task=True`.
    pub tasks: Option<TaskStore>,
}

/// Serde model of the wire-format catalog JSON produced by
/// `bolt_mcp._catalog.compile_catalog`. Tool/resource/prompt entries reuse
/// rmcp's own `Deserialize` impls so the shapes cannot drift from the spec.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CatalogWire {
    server_info: Implementation,
    #[serde(default)]
    instructions: Option<String>,
    #[serde(default)]
    tools: Vec<Tool>,
    #[serde(default)]
    resources: Vec<Resource>,
    #[serde(default)]
    resource_templates: Vec<ResourceTemplate>,
    #[serde(default)]
    prompts: Vec<Prompt>,
    #[serde(default)]
    list_ttl_ms: Option<u64>,
    #[serde(default)]
    list_cache_scope: Option<String>,
}

fn parse_guard_list(py: Python, obj: &Bound<'_, PyAny>) -> PyResult<GuardSet> {
    if obj.is_none() {
        return Ok(GuardSet::default());
    }
    let dicts: Vec<HashMap<String, Py<PyAny>>> = obj
        .extract()
        .map_err(|e| PyValueError::new_err(format!("Invalid MCP guards metadata: {e}")))?;
    let mut guards: Vec<Guard> = Vec::with_capacity(dicts.len());
    for dict in &dicts {
        guards.push(parse_guard(dict, py)?);
    }
    Ok(GuardSet::from_guards(guards))
}

/// Parse the `route_auth` of a tool: None, or the auth and the guards of its route.
fn parse_route_gate(
    py: Python<'_>,
    name: &str,
    obj: &Bound<'_, PyAny>,
) -> PyResult<Option<RouteGate>> {
    if obj.is_none() {
        return Ok(None);
    }
    let invalid = |e: &dyn std::fmt::Display| {
        PyValueError::new_err(format!("Invalid 'route_auth' of MCP tool '{name}': {e}"))
    };
    let dict = obj.cast::<PyDict>().map_err(|e| invalid(&e))?;
    let backends_obj = dict
        .get_item("auth_backends")?
        .ok_or_else(|| invalid(&"missing 'auth_backends'"))?;
    let dicts: Vec<HashMap<String, Py<PyAny>>> = backends_obj.extract().map_err(|e| invalid(&e))?;
    let auth_backends = dicts
        .iter()
        .map(|backend| parse_auth_backend(backend, py))
        .collect::<PyResult<Vec<_>>>()?;
    let guards_obj = dict
        .get_item("guards")?
        .ok_or_else(|| invalid(&"missing 'guards'"))?;
    let guards = parse_guard_list(py, &guards_obj)?;
    Ok(Some(RouteGate {
        auth_backends,
        guards,
    }))
}

/// Parse the `task` of a tool: None, or {"ttl_ms": int, "poll_interval_ms": int}.
fn parse_task_options(name: &str, obj: &Bound<'_, PyAny>) -> PyResult<Option<TaskOptions>> {
    if obj.is_none() {
        return Ok(None);
    }
    let invalid = |e: &dyn std::fmt::Display| {
        PyValueError::new_err(format!("Invalid 'task' of MCP tool '{name}': {e}"))
    };
    let dict = obj.cast::<PyDict>().map_err(|e| invalid(&e))?;
    let field = |key: &str| -> PyResult<u64> {
        dict.get_item(key)?
            .ok_or_else(|| invalid(&format!("missing '{key}'")))?
            .extract()
            .map_err(|e| invalid(&e))
    };
    Ok(Some(TaskOptions {
        ttl_ms: field("ttl_ms")?,
        poll_interval_ms: field("poll_interval_ms")?,
    }))
}

impl McpRegistry {
    /// Build the registry from the catalog JSON and the per-tool Python config.
    ///
    /// `tools_cfg` maps tool name -> {"dispatch": callable, "needs_context":
    /// bool, "guards": [guard metadata dicts]}. Every tool in the catalog must
    /// have a config entry; registration fails loudly otherwise (never leave a
    /// tool callable-less or guard-less by accident).
    pub fn from_python(
        py: Python<'_>,
        catalog_json: &str,
        config: &Bound<'_, PyDict>,
    ) -> PyResult<Self> {
        let catalog: CatalogWire = serde_json::from_str(catalog_json)
            .map_err(|e| PyValueError::new_err(format!("Invalid MCP catalog JSON: {e}")))?;

        let tools_cfg = config
            .get_item("tools")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'tools'"))?;
        let tools_cfg = tools_cfg.cast::<PyDict>().map_err(|_| {
            PyValueError::new_err("MCP mount config 'tools' must be a dict keyed by tool name")
        })?;

        let mut tool_order = Vec::with_capacity(catalog.tools.len());
        let mut tools: AHashMap<String, McpToolEntry> =
            AHashMap::with_capacity(catalog.tools.len());
        for tool in catalog.tools {
            let name = tool.name.to_string();
            let cfg = tools_cfg.get_item(&name)?.ok_or_else(|| {
                PyValueError::new_err(format!(
                    "MCP catalog tool '{name}' has no entry in the mount config"
                ))
            })?;
            let cfg = cfg.cast::<PyDict>().map_err(|_| {
                PyValueError::new_err(format!("MCP tool config for '{name}' must be a dict"))
            })?;
            let dispatch: Py<PyAny> = cfg
                .get_item("dispatch")?
                .ok_or_else(|| {
                    PyValueError::new_err(format!("MCP tool '{name}' config missing 'dispatch'"))
                })?
                .unbind();
            let needs_context: bool = cfg
                .get_item("needs_context")?
                .ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "MCP tool '{name}' config missing 'needs_context'"
                    ))
                })?
                .extract()?;
            let needs_request: bool = cfg
                .get_item("needs_request")?
                .ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "MCP tool '{name}' config missing 'needs_request'"
                    ))
                })?
                .extract()?;
            let guards_obj = cfg.get_item("guards")?.ok_or_else(|| {
                PyValueError::new_err(format!("MCP tool '{name}' config missing 'guards'"))
            })?;
            let guards = parse_guard_list(py, &guards_obj)?;
            let route_obj = cfg.get_item("route_auth")?.ok_or_else(|| {
                PyValueError::new_err(format!("MCP tool '{name}' config missing 'route_auth'"))
            })?;
            let route_gate = parse_route_gate(py, &name, &route_obj)?;
            let task_obj = cfg.get_item("task")?.ok_or_else(|| {
                PyValueError::new_err(format!("MCP tool '{name}' config missing 'task'"))
            })?;
            let task = parse_task_options(&name, &task_obj)?;
            tool_order.push(name.clone());
            tools.insert(
                name,
                McpToolEntry {
                    tool,
                    guards,
                    dispatch,
                    needs_context,
                    route_gate,
                    needs_request,
                    task,
                },
            );
        }

        let resource_dispatch: Py<PyAny> = config
            .get_item("resource_dispatch")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'resource_dispatch'"))?
            .unbind();
        let prompt_dispatch: Py<PyAny> = config
            .get_item("prompt_dispatch")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'prompt_dispatch'"))?
            .unbind();
        let resource_middleware = crate::get_bool(config, "resource_middleware")?;
        let prompt_middleware = crate::get_bool(config, "prompt_middleware")?;

        let state_key = config
            .get_item("state_key")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'state_key'"))?;
        let state_key = state_key
            .cast::<PyBytes>()
            .map_err(|_| PyValueError::new_err("MCP mount config 'state_key' must be bytes"))?
            .as_bytes()
            .to_vec();
        let codec = RequestStateCodec::try_new(state_key)
            .map_err(|err| PyValueError::new_err(format!("MCP mount config 'state_key': {err}")))?;

        let list_cache_scope = match catalog.list_cache_scope.as_deref() {
            None | Some("private") => CacheScope::Private,
            Some("public") => CacheScope::Public,
            Some(other) => {
                return Err(PyValueError::new_err(format!(
                    "Invalid MCP listCacheScope '{other}': expected 'public' or 'private'"
                )));
            }
        };

        let max_tasks: usize = config
            .get_item("max_tasks")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'max_tasks'"))?
            .extract()?;
        let tasks = tools
            .values()
            .any(|entry| entry.task.is_some())
            .then(|| TaskStore::new(max_tasks));

        // Logging capability is deprecated in 2026-07-28 but stays advertised
        // for legacy session clients that use `ctx.log()` streams.
        #[allow(deprecated)]
        let mut capabilities = ServerCapabilities::builder()
            .enable_tools()
            .enable_resources()
            .enable_prompts()
            .enable_logging();
        if tasks.is_some() {
            capabilities = capabilities.enable_tasks();
        }
        let capabilities = capabilities.build();
        let mut server_info = ServerConfig::new(capabilities);
        // Pin the advertised revision. The rmcp default is its LATEST, and a
        // minor release of rmcp can change LATEST. `initialize` falls back
        // to the newest revision that has the handshake.
        server_info.protocol_version = ProtocolVersion::V_2026_07_28;
        server_info.server_info = catalog.server_info;
        server_info.instructions = catalog.instructions;

        let route_gates = tools.values().any(|entry| entry.route_gate.is_some());
        let needs_request = tools.values().any(|entry| entry.needs_request);
        Ok(Self {
            server_info,
            tool_order,
            tools,
            resources: catalog.resources,
            resource_templates: catalog.resource_templates,
            prompts: catalog.prompts,
            resource_dispatch,
            prompt_dispatch,
            resource_middleware,
            prompt_middleware,
            codec,
            list_ttl_ms: catalog.list_ttl_ms.unwrap_or(0),
            list_cache_scope,
            route_gates,
            needs_request,
            tasks,
        })
    }

    /// Tools visible to this request's principal, in registration order.
    /// A tool that exposes a route is visible when the route accepts the request.
    pub fn visible_tools(
        &self,
        auth_ctx: Option<&AuthContext>,
        route_request: Option<&McpRouteRequest>,
    ) -> Vec<Tool> {
        self.tool_order
            .iter()
            .filter_map(|name| self.tools.get(name))
            .filter(|entry| {
                entry.guards.is_empty()
                    || matches!(
                        bolt_core::permissions::evaluate_guards(&entry.guards, auth_ctx),
                        bolt_core::permissions::GuardResult::Allow
                    )
            })
            .filter(|entry| {
                entry
                    .route_gate
                    .as_ref()
                    .is_none_or(|gate| gate.check(route_request).is_some())
            })
            .map(|entry| entry.tool.clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bolt_core::permissions::Guard;

    fn gate(guards: Vec<Guard>) -> RouteGate {
        RouteGate {
            auth_backends: Vec::new(),
            guards: GuardSet::from_guards(guards),
        }
    }

    fn request() -> McpRouteRequest {
        McpRouteRequest {
            headers: AHashMap::new(),
            method: "POST".to_owned(),
            scheme: "http".to_owned(),
            path: "/mcp".to_owned(),
            query_string: String::new(),
            conn_scheme: "http",
            conn_remote_addr: None,
        }
    }

    #[test]
    fn a_gate_with_no_request_refuses() {
        assert!(gate(vec![Guard::AllowAny]).check(None).is_none());
    }

    #[test]
    fn a_gate_refuses_a_request_that_its_guards_refuse() {
        assert!(
            gate(vec![Guard::IsAuthenticated])
                .check(Some(&request()))
                .is_none()
        );
    }

    #[test]
    fn a_gate_allows_a_request_that_its_guards_allow() {
        assert!(matches!(
            gate(vec![Guard::AllowAny]).check(Some(&request())),
            Some(None)
        ));
    }
}
