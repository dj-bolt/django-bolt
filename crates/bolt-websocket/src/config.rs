//! WebSocket configuration - cached at startup
//!
//! Settings are read once from environment variables and Django settings,
//! then cached for the lifetime of the server to avoid per-request GIL overhead.

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use std::time::Duration;

/// Cached WebSocket configuration
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

/// Global cached configuration - initialized once at first access
pub static WS_CONFIG: Lazy<WsConfig> = Lazy::new(|| WsConfig {
    max_connections: load_max_connections(),
    channel_buffer_size: load_channel_buffer_size(),
    heartbeat_interval: load_heartbeat_interval(),
    client_timeout: load_client_timeout(),
    max_message_size: load_max_message_size(),
});

/// Load max connections from env var or Django settings
fn load_max_connections() -> usize {
    // 1. Check environment variable (highest priority)
    if let Ok(val) = std::env::var("DJANGO_BOLT_WS_MAX_CONNECTIONS") {
        if let Ok(max) = val.parse::<usize>() {
            return max;
        }
    }

    // 2. Check Django settings
    Python::attach(|py| {
        if let Ok(django_conf) = py.import("django.conf") {
            if let Ok(settings) = django_conf.getattr("settings") {
                if let Ok(max) = settings.getattr("BOLT_WS_MAX_CONNECTIONS") {
                    if let Ok(val) = max.extract::<usize>() {
                        return val;
                    }
                }
            }
        }
        10000 // Default: 10k connections
    })
}

/// Load channel buffer size from env var or Django settings
fn load_channel_buffer_size() -> usize {
    // 1. Check environment variable
    if let Ok(val) = std::env::var("DJANGO_BOLT_WS_CHANNEL_SIZE") {
        if let Ok(size) = val.parse::<usize>() {
            return size;
        }
    }

    // 2. Check Django settings
    Python::attach(|py| {
        if let Ok(django_conf) = py.import("django.conf") {
            if let Ok(settings) = django_conf.getattr("settings") {
                if let Ok(size) = settings.getattr("BOLT_WS_CHANNEL_SIZE") {
                    if let Ok(val) = size.extract::<usize>() {
                        return val;
                    }
                }
            }
        }
        100 // Default: 100 messages buffer
    })
}

/// Load heartbeat interval from env var or Django settings
fn load_heartbeat_interval() -> Duration {
    // 1. Check environment variable
    if let Ok(val) = std::env::var("DJANGO_BOLT_WS_HEARTBEAT_INTERVAL") {
        if let Ok(secs) = val.parse::<u64>() {
            return Duration::from_secs(secs);
        }
    }

    // 2. Check Django settings
    Python::attach(|py| {
        if let Ok(django_conf) = py.import("django.conf") {
            if let Ok(settings) = django_conf.getattr("settings") {
                if let Ok(interval) = settings.getattr("BOLT_WS_HEARTBEAT_INTERVAL") {
                    if let Ok(secs) = interval.extract::<u64>() {
                        return Duration::from_secs(secs);
                    }
                }
            }
        }
        Duration::from_secs(5) // Default: 5 seconds
    })
}

/// Load client timeout from env var or Django settings
fn load_client_timeout() -> Duration {
    // 1. Check environment variable
    if let Ok(val) = std::env::var("DJANGO_BOLT_WS_CLIENT_TIMEOUT") {
        if let Ok(secs) = val.parse::<u64>() {
            return Duration::from_secs(secs);
        }
    }

    // 2. Check Django settings
    Python::attach(|py| {
        if let Ok(django_conf) = py.import("django.conf") {
            if let Ok(settings) = django_conf.getattr("settings") {
                if let Ok(timeout) = settings.getattr("BOLT_WS_CLIENT_TIMEOUT") {
                    if let Ok(secs) = timeout.extract::<u64>() {
                        return Duration::from_secs(secs);
                    }
                }
            }
        }
        Duration::from_secs(10) // Default: 10 seconds
    })
}

/// Load max message size from env var or Django settings
fn load_max_message_size() -> usize {
    // 1. Check environment variable
    if let Ok(val) = std::env::var("DJANGO_BOLT_WS_MAX_MESSAGE_SIZE") {
        if let Ok(size) = val.parse::<usize>() {
            return size;
        }
    }

    // 2. Check Django settings
    Python::attach(|py| {
        if let Ok(django_conf) = py.import("django.conf") {
            if let Ok(settings) = django_conf.getattr("settings") {
                if let Ok(size) = settings.getattr("BOLT_WS_MAX_MESSAGE_SIZE") {
                    if let Ok(val) = size.extract::<usize>() {
                        return val;
                    }
                }
            }
        }
        1024 * 1024 // Default: 1MB
    })
}
