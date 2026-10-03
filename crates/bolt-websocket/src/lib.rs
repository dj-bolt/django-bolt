//! WebSocket support for Django-Bolt
//!
//! This module provides WebSocket support with proper Python handler integration.
//! Uses tokio channels to bridge Actix's actor-based WebSocket with Python's
//! ASGI-style async interface.
//!
//! ## Module Structure
//!
//! - `config` - Cached configuration (read once at startup)
//! - `messages` - Message types for actor/Python communication
//! - `actor` - Actix WebSocket actor implementation
//! - `router` - WebSocket route matching
//! - `handler` - HTTP upgrade handler

pub mod actor;
pub mod config;
pub mod handler;
pub mod messages;
pub mod router;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

// Re-export public API
#[allow(unused_imports)] // Re-exported for external use
pub use actor::WebSocketActor;
#[allow(unused_imports)] // Re-exported for external use
pub use config::WS_CONFIG;
pub use handler::{
    handle_websocket_upgrade_with_handler, is_websocket_upgrade, requested_subprotocols,
};
#[allow(unused_imports)] // Re-exported for external use
pub use messages::{SendToClient, WsMessage};
#[allow(unused_imports)] // Re-exported for external use
pub use router::WebSocketRoute;
pub use router::WebSocketRouter;

/// Process-wide WebSocket router, set once at server startup.
pub static GLOBAL_WEBSOCKET_ROUTER: once_cell::sync::OnceCell<std::sync::Arc<WebSocketRouter>> =
    once_cell::sync::OnceCell::new();

/// Global counter for active WebSocket connections
pub static ACTIVE_WS_CONNECTIONS: AtomicUsize = AtomicUsize::new(0);

/// One count in `ACTIVE_WS_CONNECTIONS`, released on drop.
///
/// The upgrade holds the slot while the handler decides the handshake.
/// The actor takes it on accept. A refused or dropped handshake releases it.
pub(crate) struct ConnectionSlot(());

impl ConnectionSlot {
    pub(crate) fn acquire() -> Self {
        ACTIVE_WS_CONNECTIONS.fetch_add(1, Ordering::Relaxed);
        ConnectionSlot(())
    }
}

impl Drop for ConnectionSlot {
    fn drop(&mut self) {
        ACTIVE_WS_CONNECTIONS.fetch_sub(1, Ordering::Relaxed);
    }
}

/// Process-wide flag: the server is shutting down or being recycled.
///
/// While draining, new WebSocket upgrades are refused with 503 and existing
/// connections are closed with 1012 (Service Restart) so clients reconnect
/// and land on a healthy worker (SO_REUSEPORT routes them there).
static DRAINING: AtomicBool = AtomicBool::new(false);

/// Begin draining WebSocket connections (called once on shutdown signal).
pub fn begin_drain() {
    DRAINING.store(true, Ordering::Release);
}

/// Whether the server is currently draining for shutdown/recycle.
pub fn is_draining() -> bool {
    DRAINING.load(Ordering::Acquire)
}
