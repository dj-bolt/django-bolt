//! WebSocket configuration - read once at startup
//!
//! Settings are read once from environment variables and Django settings.
//! The app state holds the result, so a connection reads it without the GIL.

use bolt_core::settings::{env_var, DjangoSettings};
use pyo3::prelude::*;
use std::time::Duration;

/// WebSocket configuration of one app
#[derive(Debug)]
pub struct WsConfig {
    /// Maximum allowed concurrent WebSocket connections
    pub max_connections: usize,
    /// Channel buffer size for message passing
    pub channel_buffer_size: usize,
    /// Heartbeat ping interval
    pub heartbeat_interval: Duration,
    /// Client timeout (disconnect if no pong received)
    pub client_timeout: Duration,
    /// Maximum message size in bytes
    pub max_message_size: usize,
}

impl WsConfig {
    /// Read the WebSocket configuration.
    ///
    /// An environment variable comes first, then the Django setting, then the
    /// default. An invalid variable or a Django setting with the wrong type
    /// raises `ImproperlyConfigured`.
    pub fn from_django_settings(settings: &DjangoSettings<'_>) -> PyResult<Self> {
        let py = settings.py();
        const NON_NEGATIVE: &str = "an int of 0 or more";
        const POSITIVE: &str = "an int of 1 or more";

        let max_connections = match env_var(
            py,
            "DJANGO_BOLT_WS_MAX_CONNECTIONS",
            NON_NEGATIVE,
            |_: &usize| true,
        )? {
            Some(max) => max,
            None => settings.non_negative_int("BOLT_WS_MAX_CONNECTIONS", 10000)?, // Default: 10k connections
        };
        let channel_buffer_size =
            match env_var(py, "DJANGO_BOLT_WS_CHANNEL_SIZE", POSITIVE, |n: &usize| {
                *n >= 1
            })? {
                Some(size) => size,
                None => settings.positive_int("BOLT_WS_CHANNEL_SIZE", 100)?, // Default: 100 messages buffer
            };
        let heartbeat_secs = match env_var(
            py,
            "DJANGO_BOLT_WS_HEARTBEAT_INTERVAL",
            POSITIVE,
            |n: &u64| *n >= 1,
        )? {
            Some(secs) => secs,
            None => settings.positive_int("BOLT_WS_HEARTBEAT_INTERVAL", 5u64)?, // Default: 5 seconds
        };
        let client_timeout_secs = match env_var(
            py,
            "DJANGO_BOLT_WS_CLIENT_TIMEOUT",
            NON_NEGATIVE,
            |_: &u64| true,
        )? {
            Some(secs) => secs,
            None => settings.non_negative_int("BOLT_WS_CLIENT_TIMEOUT", 10u64)?, // Default: 10 seconds
        };
        let max_message_size = match env_var(
            py,
            "DJANGO_BOLT_WS_MAX_MESSAGE_SIZE",
            NON_NEGATIVE,
            |_: &usize| true,
        )? {
            Some(size) => size,
            None => settings.non_negative_int("BOLT_WS_MAX_MESSAGE_SIZE", 1024 * 1024)?, // Default: 1MB
        };
        Ok(Self {
            max_connections,
            channel_buffer_size,
            heartbeat_interval: Duration::from_secs(heartbeat_secs),
            client_timeout: Duration::from_secs(client_timeout_secs),
            max_message_size,
        })
    }
}
