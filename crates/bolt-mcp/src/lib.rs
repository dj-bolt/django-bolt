//! MCP (Model Context Protocol) serving, built on the official Rust SDK (rmcp).
//!
//! rmcp owns the protocol: dual-era Streamable HTTP (2026-07-28 stateless +
//! legacy sessions), version negotiation, MRTR types, header validation,
//! DNS-rebinding checks. This module owns the integration: the Actix bridge
//! (`bridge`), the registry snapshot compiled from Python at mount time
//! (`registry`), the `ServerHandler` (`handler`), the per-call Python context
//! (`context`), and the MRTR requestState policy (`state_codec`).
//!
//! MCP mounts are checked only on router miss (see `handler.rs` /
//! `testing.rs`), so the non-MCP hot path pays nothing.

pub mod bridge;
pub mod context;
pub mod handler;
pub mod registry;
pub mod state_codec;

use std::collections::HashMap;
use std::sync::Arc;

use once_cell::sync::OnceCell;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::streamable_http_server::session::never::NeverSessionManager;
use rmcp::transport::streamable_http_server::{StreamableHttpServerConfig, StreamableHttpService};

use bolt_core::metadata::{parse_auth_backend, parse_guard};
use bolt_core::middleware::auth::{AuthBackend, AuthContext};
use bolt_core::permissions::{Guard, GuardSet};
use bolt_core::state::AppState;
use handler::McpHandler;
use registry::McpRegistry;

/// Authenticated principal for the current MCP request, carried through the
/// http request extensions into rmcp's `RequestContext.extensions`.
#[derive(Clone)]
pub struct McpAuthExt(pub Option<Arc<AuthContext>>);

/// Tier-2 OAuth resource-server hook: a Python `verify_token(token) ->
/// principal dict | None` plus the prebuilt WWW-Authenticate challenge.
pub struct McpOAuth {
    pub verify_token: Py<PyAny>,
    pub www_authenticate: String,
}

pub enum McpService {
    Sessions(StreamableHttpService<McpHandler, LocalSessionManager>),
    Stateless(StreamableHttpService<McpHandler, NeverSessionManager>),
}

impl McpService {
    pub async fn handle<B>(
        &self,
        req: http::Request<B>,
    ) -> http::Response<http_body_util::combinators::BoxBody<bytes::Bytes, std::convert::Infallible>>
    where
        B: http_body::Body + Send + 'static,
        B::Data: Send + 'static,
        B::Error: std::fmt::Display,
    {
        match self {
            Self::Sessions(service) => service.handle(req).await,
            Self::Stateless(service) => service.handle(req).await,
        }
    }
}

pub struct McpMount {
    /// Exact mount path (e.g. "/mcp"); its trailing-slash variant also matches.
    pub path: String,
    /// Tier-1 auth, enforced in Rust before rmcp sees the request.
    pub auth_backends: Vec<AuthBackend>,
    pub guards: GuardSet,
    pub oauth: Option<McpOAuth>,
    pub service: McpService,
}

impl McpMount {
    #[inline]
    fn matches_path(&self, path: &str) -> bool {
        path == self.path
            || (path.len() == self.path.len() + 1
                && path.ends_with('/')
                && path.starts_with(self.path.as_str()))
    }
}

/// Production mounts, registered once at startup.
pub static GLOBAL_MCP_MOUNTS: OnceCell<Arc<Vec<McpMount>>> = OnceCell::new();

/// Find the MCP mount for a path. Tests provide per-instance mounts via
/// `AppState.extensions` (an `Arc<Vec<McpMount>>`); production uses `GLOBAL_MCP_MOUNTS`.
#[inline]
pub fn find_mcp_mount<'a>(state: &'a AppState, path: &str) -> Option<&'a McpMount> {
    if let Some(mounts) = state.extensions.get::<Arc<Vec<McpMount>>>() {
        return mounts.iter().find(|m| m.matches_path(path));
    }
    GLOBAL_MCP_MOUNTS
        .get()
        .and_then(|mounts| mounts.iter().find(|m| m.matches_path(path)))
}

fn get_bool(config: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    config
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("MCP mount config missing '{key}'")))?
        .extract()
        .map_err(|e| PyValueError::new_err(format!("MCP mount config '{key}': {e}")))
}

/// Parse one mount definition dict into a ready-to-serve `McpMount`.
pub fn parse_mount(py: Python<'_>, mount_def: &Bound<'_, PyDict>) -> PyResult<McpMount> {
    let path: String = mount_def
        .get_item("path")?
        .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'path'"))?
        .extract()?;
    let catalog_json: String = mount_def
        .get_item("catalog_json")?
        .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'catalog_json'"))?
        .extract()?;

    let registry = Arc::new(McpRegistry::from_python(py, &catalog_json, mount_def)?);

    // ---- Tier-1 auth + mount guards (same wire format as route metadata) ----
    let mut auth_backends = Vec::new();
    if let Some(list) = mount_def.get_item("auth_backends")? {
        if !list.is_none() {
            let dicts: Vec<HashMap<String, Py<PyAny>>> = list.extract().map_err(|e| {
                PyValueError::new_err(format!("Invalid MCP mount 'auth_backends': {e}"))
            })?;
            for dict in &dicts {
                auth_backends.push(parse_auth_backend(dict, py)?);
            }
        }
    }
    let guards = match mount_def.get_item("guards")? {
        Some(list) if !list.is_none() => {
            let dicts: Vec<HashMap<String, Py<PyAny>>> = list
                .extract()
                .map_err(|e| PyValueError::new_err(format!("Invalid MCP mount 'guards': {e}")))?;
            let mut parsed: Vec<Guard> = Vec::with_capacity(dicts.len());
            for dict in &dicts {
                parsed.push(parse_guard(dict, py)?);
            }
            GuardSet::from_guards(parsed)
        }
        _ => GuardSet::default(),
    };

    // ---- Tier-2 OAuth hook ----
    let oauth = match mount_def.get_item("verify_token")? {
        Some(obj) if !obj.is_none() => {
            let www_authenticate: String = mount_def
                .get_item("www_authenticate")?
                .ok_or_else(|| {
                    PyValueError::new_err("MCP mount with verify_token missing 'www_authenticate'")
                })?
                .extract()?;
            Some(McpOAuth {
                verify_token: obj.unbind(),
                www_authenticate,
            })
        }
        _ => None,
    };

    // ---- Transport config ----
    let legacy_sessions = get_bool(mount_def, "legacy_sessions")?;
    let json_response = get_bool(mount_def, "json_response")?;
    let max_body_bytes: usize = mount_def
        .get_item("max_body_bytes")?
        .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'max_body_bytes'"))?
        .extract()?;
    let allowed_hosts: Option<Vec<String>> = mount_def
        .get_item("allowed_hosts")?
        .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'allowed_hosts'"))?
        .extract()?;
    let allowed_origins: Option<Vec<String>> = mount_def
        .get_item("allowed_origins")?
        .ok_or_else(|| PyValueError::new_err("MCP mount config missing 'allowed_origins'"))?
        .extract()?;

    let mut config = StreamableHttpServerConfig::default()
        .with_legacy_session_mode(legacy_sessions)
        .with_json_response(json_response)
        .with_max_request_body_bytes(max_body_bytes);
    if let Some(hosts) = allowed_hosts {
        config = config.with_allowed_hosts(hosts);
    }
    if let Some(origins) = allowed_origins {
        config = config.with_allowed_origins(origins);
    }

    let mcp_handler = McpHandler { registry };
    let service = if legacy_sessions {
        McpService::Sessions(StreamableHttpService::new(
            move || Ok(mcp_handler.clone()),
            Arc::new(LocalSessionManager::default()),
            config,
        ))
    } else {
        McpService::Stateless(StreamableHttpService::new(
            move || Ok(mcp_handler.clone()),
            Arc::new(NeverSessionManager::default()),
            config,
        ))
    };

    Ok(McpMount {
        path,
        auth_backends,
        guards,
        oauth,
        service,
    })
}

/// Register production MCP mounts. Called once at startup by `mount_mcp`
/// (via the runbolt registrar), before the server starts serving.
#[pyfunction]
pub fn register_mcp_mounts(py: Python<'_>, mounts: Vec<Py<PyAny>>) -> PyResult<()> {
    let mut parsed = Vec::with_capacity(mounts.len());
    for mount in &mounts {
        let dict = mount.bind(py);
        let dict = dict
            .cast::<PyDict>()
            .map_err(|_| PyValueError::new_err("MCP mount definition must be a dict"))?;
        parsed.push(parse_mount(py, dict)?);
    }
    GLOBAL_MCP_MOUNTS
        .set(Arc::new(parsed))
        .map_err(|_| PyValueError::new_err("MCP mounts already registered"))
}
