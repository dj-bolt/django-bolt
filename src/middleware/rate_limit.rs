use actix_web::HttpResponse;
use ahash::AHashMap;
use dashmap::DashMap;
use governor::clock::{Clock, DefaultClock};
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use once_cell::sync::Lazy;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use crate::metadata::RateLimitConfig;
use crate::response_builder;
use crate::responses;

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

// Store per-key limiters (IP-based)
static IP_LIMITERS: Lazy<DashMap<(usize, String), Arc<Limiter>>> = Lazy::new(DashMap::new);

// Track total limiter count for cleanup
static LIMITER_COUNT: AtomicUsize = AtomicUsize::new(0);

// SECURITY: Maximum number of rate limiters to prevent memory exhaustion
const MAX_LIMITERS: usize = 100_000;

// SECURITY: Maximum key length to prevent memory attacks
const MAX_KEY_LENGTH: usize = 256;

pub fn check_rate_limit(
    handler_id: usize,
    headers: &AHashMap<String, String>,
    peer_addr: Option<&str>,
    config: &RateLimitConfig,
    method: &str,
    path: &str,
) -> Option<HttpResponse> {
    // Config is already parsed at startup - no GIL needed!
    let rps = config.rps;
    let burst = config.burst;
    let key_type = &config.key_type;

    // Determine the rate limit key
    let key = match key_type.as_str() {
        "ip" => {
            // Try to get client IP from headers (X-Forwarded-For, X-Real-IP, etc.)
            headers
                .get("x-forwarded-for")
                .or_else(|| headers.get("x-real-ip"))
                .or_else(|| headers.get("remote-addr"))
                .map(|ip| {
                    // Take first IP if comma-separated
                    ip.split(',').next().unwrap_or(ip).trim().to_string()
                })
                // Fallback to peer_addr if headers are missing
                .or_else(|| peer_addr.map(|s| s.to_string()))
                .unwrap_or_else(|| "unknown".to_string())
        }
        header_name => {
            // Custom header key — already lowercased once at startup when the
            // RateLimitConfig was parsed (headers map stores lowercase names).
            headers
                .get(header_name)
                .cloned()
                .unwrap_or_else(|| "unknown".to_string())
        }
    };

    // SECURITY: Validate key length to prevent memory attacks
    if key.len() > MAX_KEY_LENGTH {
        // Truncate or reject long keys
        return Some(
            HttpResponse::BadRequest()
                .content_type("application/json")
                .body(r#"{"detail":"Rate limit key too long"}"#),
        );
    }

    // SECURITY: Check if we've exceeded max limiters (prevent memory exhaustion)
    let current_count = LIMITER_COUNT.load(Ordering::Relaxed);
    if current_count >= MAX_LIMITERS {
        // Trigger cleanup of old limiters (simple LRU-style)
        cleanup_old_limiters();
    }

    // Get or create rate limiter for this handler + key combination
    let limiter_key = (handler_id, key.clone());
    let limiter = IP_LIMITERS.entry(limiter_key.clone()).or_insert_with(|| {
        // Increment counter
        LIMITER_COUNT.fetch_add(1, Ordering::Relaxed);

        // Use NonZero constructors properly
        let rps_nonzero = std::num::NonZeroU32::new(rps.max(1)).unwrap();
        let burst_nonzero = std::num::NonZeroU32::new(burst.max(1)).unwrap();
        let quota = Quota::per_second(rps_nonzero).allow_burst(burst_nonzero);
        Arc::new(RateLimiter::direct(quota))
    });

    // Check rate limit
    match limiter.check() {
        Ok(_) => None, // Request allowed
        Err(not_until) => {
            // Calculate retry after in seconds
            let wait_time = not_until.wait_time_from(DefaultClock::default().now());
            let retry_after = wait_time.as_secs().max(1);

            // Log rate limit exceeded
            eprintln!(
                "[django-bolt] Rate limit exceeded: {} {} | key: {} | limit: {} rps (burst: {}) | retry after: {}s",
                method, path, key, rps, burst, retry_after
            );

            Some(response_builder::build_rate_limit_response(
                retry_after,
                rps,
                burst,
                responses::get_rate_limit_body(retry_after),
            ))
        }
    }
}

/// Cleanup old rate limiters when limit is reached
/// Simple strategy: remove 20% of limiters to make room for new ones
fn cleanup_old_limiters() {
    let to_remove = (MAX_LIMITERS as f64 * 0.2) as usize;
    let mut removed = 0;

    // Remove first N entries (simple cleanup, not LRU)
    IP_LIMITERS.retain(|_, _| {
        if removed < to_remove {
            removed += 1;
            LIMITER_COUNT.fetch_sub(1, Ordering::Relaxed);
            false // Remove this entry
        } else {
            true // Keep this entry
        }
    });
}
