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
    ResourceTemplate, ServerCapabilities, ServerInfo, Tool,
};
use serde::Deserialize;

use crate::metadata::parse_guard;
use crate::permissions::{Guard, GuardSet};

/// One tool entry: prebuilt wire model + Rust-evaluated guards + the Python
/// callable that executes it.
pub struct McpToolEntry {
    pub tool: Tool,
    pub guards: GuardSet,
    /// Async Python callable taking one payload dict, returning result JSON.
    pub dispatch: Py<PyAny>,
    /// Whether the tool declared a `Context` parameter (progress/log/elicit).
    pub needs_context: bool,
}

pub struct McpRegistry {
    pub server_info: ServerInfo,
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
    /// HMAC codec for MRTR requestState (key derived from SECRET_KEY in Python).
    pub codec: RequestStateCodec,
    /// Freshness hint applied to all list results (SEP-2549).
    pub list_ttl_ms: u64,
    pub list_cache_scope: CacheScope,
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
            let guards_obj = cfg.get_item("guards")?.ok_or_else(|| {
                PyValueError::new_err(format!("MCP tool '{name}' config missing 'guards'"))
            })?;
            let guards = parse_guard_list(py, &guards_obj)?;
            tool_order.push(name.clone());
            tools.insert(
                name,
                McpToolEntry {
                    tool,
                    guards,
                    dispatch,
                    needs_context,
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

        let state_key = config
            .get_item("state_key")?
            .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'state_key'"))?;
        let state_key = state_key
            .cast::<PyBytes>()
            .map_err(|_| PyValueError::new_err("MCP mount config 'state_key' must be bytes"))?
            .as_bytes()
            .to_vec();
        if state_key.len() < 32 {
            return Err(PyValueError::new_err(
                "MCP mount config 'state_key' must be at least 32 bytes",
            ));
        }

        let list_cache_scope = match catalog.list_cache_scope.as_deref() {
            None | Some("private") => CacheScope::Private,
            Some("public") => CacheScope::Public,
            Some(other) => {
                return Err(PyValueError::new_err(format!(
                    "Invalid MCP listCacheScope '{other}': expected 'public' or 'private'"
                )))
            }
        };

        // Logging capability is deprecated in 2026-07-28 but stays advertised
        // for legacy session clients that use `ctx.log()` streams.
        #[allow(deprecated)]
        let capabilities = ServerCapabilities::builder()
            .enable_tools()
            .enable_resources()
            .enable_prompts()
            .enable_logging()
            .build();
        let mut server_info = ServerInfo::new(capabilities);
        // Advertise the modern revision explicitly: rmcp's LATEST is the
        // previous revision and `initialize` negotiation caps at this value.
        server_info.protocol_version = ProtocolVersion::V_2026_07_28;
        server_info.server_info = catalog.server_info;
        server_info.instructions = catalog.instructions;

        Ok(Self {
            server_info,
            tool_order,
            tools,
            resources: catalog.resources,
            resource_templates: catalog.resource_templates,
            prompts: catalog.prompts,
            resource_dispatch,
            prompt_dispatch,
            codec: RequestStateCodec::new(state_key),
            list_ttl_ms: catalog.list_ttl_ms.unwrap_or(0),
            list_cache_scope,
        })
    }

    /// Tools visible to this request's principal, in registration order.
    pub fn visible_tools(
        &self,
        auth_ctx: Option<&crate::middleware::auth::AuthContext>,
    ) -> Vec<Tool> {
        self.tool_order
            .iter()
            .filter_map(|name| self.tools.get(name))
            .filter(|entry| {
                entry.guards.is_empty()
                    || matches!(
                        crate::permissions::evaluate_guards(&entry.guards, auth_ctx),
                        crate::permissions::GuardResult::Allow
                    )
            })
            .map(|entry| entry.tool.clone())
            .collect()
    }
}
