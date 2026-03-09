use ahash::AHashMap;
use once_cell::sync::OnceCell;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use regex::Regex;
use std::sync::atomic::AtomicU64;
use std::sync::Arc;
use std::time::Duration;

use crate::metadata::{CompressionConfig, CorsConfig, RouteMetadata, RouteMetadataStore};
use crate::router::Router;
use crate::websocket::WebSocketRouter;

/// ASGI sub-application mount configuration.
///
/// `prefix` is normalized and static (e.g. "/", "/admin", "/django/accounts").
/// `app` is an ASGI callable: `(scope, receive, send) -> awaitable`.
pub struct AsgiMount {
    pub prefix: String,
    pub app: Py<PyAny>,
}

impl AsgiMount {
    /// Check whether request path belongs to this mount.
    ///
    /// Matching rules:
    /// - "/" matches all paths
    /// - "/prefix" matches "/prefix" and "/prefix/..."
    #[inline]
    pub fn matches_path(&self, path: &str) -> bool {
        if self.prefix == "/" {
            return true;
        }

        if path == self.prefix {
            return true;
        }

        let prefix_len = self.prefix.len();
        path.len() > prefix_len
            && path.starts_with(&self.prefix)
            && path.as_bytes().get(prefix_len) == Some(&b'/')
    }
}

#[inline]
fn find_mount_in_slice<'a>(mounts: &'a [AsgiMount], path: &str) -> Option<&'a AsgiMount> {
    // Mounts are pre-sorted by descending prefix length.
    mounts.iter().find(|mount| mount.matches_path(path))
}

/// Configuration for serving static files via Actix
#[derive(Clone, Debug)]
pub struct StaticFilesConfig {
    pub url_prefix: String,         // URL prefix (e.g., "/static")
    pub directories: Vec<String>,   // List of directories to serve from
    pub csp_header: Option<String>, // Pre-built Content-Security-Policy header from Django settings
}

pub struct AppState {
    pub dispatch: Py<PyAny>,
    pub dispatch_sync: Py<PyAny>,
    pub debug: bool,
    pub max_header_size: usize,
    pub max_payload_size: usize,
    pub asgi_mount_timeout: Duration,
    pub global_cors_config: Option<CorsConfig>, // Global CORS configuration from Django settings
    pub cors_origin_regexes: Vec<Regex>,        // Compiled regex patterns for origin matching
    pub global_compression_config: Option<CompressionConfig>, // Global compression configuration used by middleware
    pub router: Option<Arc<Router>>, // Router (used by test infrastructure, optional in production)
    pub route_metadata: Option<Arc<RouteMetadataStore>>, // Route metadata (used by test infrastructure)
    pub asgi_mounts: Option<Arc<Vec<AsgiMount>>>, // ASGI mounts (tests). Production uses GLOBAL_ASGI_MOUNTS.
    pub static_files_config: Option<StaticFilesConfig>, // Static files configuration from Django settings
    pub access_logger: Option<Py<PyAny>>,       // Python logger instance for access logging (django.server). None when disabled.
}

pub static GLOBAL_ROUTER: OnceCell<Arc<Router>> = OnceCell::new();
pub static GLOBAL_WEBSOCKET_ROUTER: OnceCell<Arc<WebSocketRouter>> = OnceCell::new();
pub static GLOBAL_ASGI_MOUNTS: OnceCell<Arc<Vec<AsgiMount>>> = OnceCell::new();
pub static TASK_LOCALS: OnceCell<TaskLocals> = OnceCell::new(); // reuse global python event loop
pub static ROUTE_METADATA: OnceCell<Arc<RouteMetadataStore>> = OnceCell::new();
pub static ROUTE_METADATA_TEMP: OnceCell<AHashMap<usize, RouteMetadata>> = OnceCell::new(); // Temporary storage before CORS injection

/// Find the mounted ASGI app for a path.
///
/// Test infrastructure can provide per-instance mounts via `AppState.asgi_mounts`.
/// Production uses `GLOBAL_ASGI_MOUNTS`.
#[inline]
pub fn find_asgi_mount<'a>(state: &'a AppState, path: &str) -> Option<&'a AsgiMount> {
    if let Some(ref mounts) = state.asgi_mounts {
        return find_mount_in_slice(mounts.as_ref(), path);
    }

    GLOBAL_ASGI_MOUNTS
        .get()
        .and_then(|mounts| find_mount_in_slice(mounts.as_ref(), path))
}

// Sync streaming thread limiting to prevent thread exhaustion DoS
// Tracks number of active sync streaming threads (each uses an OS thread)
pub static ACTIVE_SYNC_STREAMING_THREADS: AtomicU64 = AtomicU64::new(0);

/// Get the configured maximum concurrent sync streaming threads
/// Default: 1000 if not configured
/// Reads from (in order of precedence):
/// 1. Environment variable: DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS
/// 2. Django setting: BOLT_MAX_SYNC_STREAMING_THREADS
/// 3. Default: 1000
pub fn get_max_sync_streaming_threads() -> u64 {
    // Check environment variable first
    if let Ok(val) = std::env::var("DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS") {
        if let Ok(n) = val.parse::<u64>() {
            if n > 0 {
                return n;
            }
        }
    }

    // Check Django settings via Python
    let limit = Python::attach(|py| {
        if let Ok(django_module) = py.import("django.conf") {
            if let Ok(settings) = django_module.getattr("settings") {
                if let Ok(limit_obj) = settings.getattr("BOLT_MAX_SYNC_STREAMING_THREADS") {
                    if let Ok(n) = limit_obj.extract::<u64>() {
                        if n > 0 {
                            return Some(n);
                        }
                    }
                }
            }
        }
        None
    });

    limit.unwrap_or(1000) // Default to 1000
}
