use actix_web::HttpResponse;
use ahash::AHashMap;
use dashmap::DashMap;
use governor::clock::{Clock, DefaultClock};
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use once_cell::sync::Lazy;
use std::fmt;
use std::net::IpAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use crate::metadata::{RateLimitConfig, RateLimitKey};
use crate::middleware::auth::AuthContext;
use crate::response_builder;
use crate::responses;

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

/// One bucket identity. `Ip` is `Copy`, so the common path hashes 17 bytes
/// and never allocates. `Header` owns its value because it is the map key.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum LimiterKey {
    Ip(IpAddr),
    Header(Box<str>),
    Identity(Box<str>),
    Unknown,
}

impl fmt::Display for LimiterKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LimiterKey::Ip(ip) => ip.fmt(f),
            LimiterKey::Header(value) => f.write_str(value),
            LimiterKey::Identity(value) => f.write_str(value),
            LimiterKey::Unknown => f.write_str("unknown"),
        }
    }
}

/// Per-key limiters. The quota is part of the identity: handler ids are reused
/// after a reload or by the next test app, and a stale limiter must not keep
/// an old quota alive.
static IP_LIMITERS: Lazy<DashMap<(usize, u32, u32, LimiterKey), Arc<Limiter>>> =
    Lazy::new(DashMap::new);

// Track total limiter count for cleanup
static LIMITER_COUNT: AtomicUsize = AtomicUsize::new(0);

// SECURITY: Maximum number of rate limiters to prevent memory exhaustion
const MAX_LIMITERS: usize = 100_000;

// SECURITY: Maximum key length to prevent memory attacks
const MAX_KEY_LENGTH: usize = 256;

/// The check for address and header keys. Runs before authentication so a
/// flood never pays for token verification. Identity keys return `None` here.
#[inline]
pub fn check_before_auth(
    handler_id: usize,
    headers: &AHashMap<String, String>,
    client_ip: Option<&IpAddr>,
    config: &RateLimitConfig,
    method: &str,
    path: &str,
) -> Option<HttpResponse> {
    if config.key.needs_identity() {
        return None;
    }
    check_rate_limit(handler_id, headers, client_ip, None, config, method, path)
}

/// The check for identity keys. Runs after authentication, with the
/// `AuthContext` of the request. Other keys return `None` here.
#[inline]
pub fn check_after_auth(
    handler_id: usize,
    headers: &AHashMap<String, String>,
    client_ip: Option<&IpAddr>,
    auth_ctx: Option<&AuthContext>,
    config: &RateLimitConfig,
    method: &str,
    path: &str,
) -> Option<HttpResponse> {
    if !config.key.needs_identity() {
        return None;
    }
    check_rate_limit(handler_id, headers, client_ip, auth_ctx, config, method, path)
}

pub fn check_rate_limit(
    handler_id: usize,
    headers: &AHashMap<String, String>,
    client_ip: Option<&IpAddr>,
    auth_ctx: Option<&AuthContext>,
    config: &RateLimitConfig,
    method: &str,
    path: &str,
) -> Option<HttpResponse> {
    // Config is already parsed at startup - no GIL needed!
    let rps = config.rps;
    let burst = config.burst;

    // Determine the rate limit key
    let key = match &config.key {
        RateLimitKey::Ip => client_ip.map(|ip| LimiterKey::Ip(*ip)),
        // Custom header key — already lowercased once at startup when the
        // RateLimitConfig was parsed (headers map stores lowercase names).
        RateLimitKey::Header(name) => match headers.get(name) {
            // SECURITY: Validate key length to prevent memory attacks
            Some(value) if value.len() > MAX_KEY_LENGTH => {
                return Some(
                    HttpResponse::BadRequest()
                        .content_type("application/json")
                        .body(r#"{"detail":"Rate limit key too long"}"#),
                );
            }
            Some(value) => Some(LimiterKey::Header(value.as_str().into())),
            None => None,
        },
        // Identity keys run after authentication. A caller with no identity
        // is limited per client address, so an unauthenticated flood cannot
        // pick a fresh bucket per request.
        RateLimitKey::User => identity_key(auth_ctx, client_ip, |_| true),
        RateLimitKey::ApiKey => identity_key(auth_ctx, client_ip, |ctx| ctx.backend == "api_key"),
    }
    .unwrap_or(LimiterKey::Unknown);

    // SECURITY: Check if we've exceeded max limiters (prevent memory exhaustion)
    let current_count = LIMITER_COUNT.load(Ordering::Relaxed);
    if current_count >= MAX_LIMITERS {
        // Trigger cleanup of old limiters (simple LRU-style)
        cleanup_old_limiters();
    }

    // Get or create rate limiter for this handler + key combination
    let limiter = IP_LIMITERS
        .entry((handler_id, rps, burst, key))
        .or_insert_with(|| {
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
                method, path, limiter.key().3, rps, burst, retry_after
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

/// The authenticated identity when `accept` admits the backend, else the
/// client address.
#[inline]
fn identity_key(
    auth_ctx: Option<&AuthContext>,
    client_ip: Option<&IpAddr>,
    accept: impl Fn(&AuthContext) -> bool,
) -> Option<LimiterKey> {
    auth_ctx
        .filter(|ctx| accept(ctx))
        .and_then(|ctx| ctx.user_id.as_deref())
        .map(|id| LimiterKey::Identity(id.into()))
        .or_else(|| client_ip.map(|ip| LimiterKey::Ip(*ip)))
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
