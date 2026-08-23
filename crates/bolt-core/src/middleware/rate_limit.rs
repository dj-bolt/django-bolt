use actix_web::HttpResponse;
use ahash::{AHashMap, RandomState};
use dashmap::DashMap;
use governor::clock::{Clock, DefaultClock};
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use once_cell::sync::Lazy;
use std::fmt;
use std::hash::{BuildHasher, Hash, Hasher};
use std::net::IpAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use crate::metadata::{RateLimitConfig, RateLimitKey};
use crate::middleware::auth::AuthContext;
use crate::response_builder;
use crate::responses;

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

/// Seeds the bucket hash once per process. A caller must not be able to pick
/// a key that lands in another caller's bucket, so the map from key material
/// to bucket is not predictable from outside the process. Buckets are already
/// per-process, so the seed does not need to agree across workers.
static KEY_HASHER: Lazy<RandomState> = Lazy::new(RandomState::new);

/// One bucket identity, hashed to 8 bytes. The map holds no key material, so
/// nothing is allocated per request and a long header value costs the same as
/// an address.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct LimiterKey(u64);

/// Where a bucket key comes from. This borrows the key material rather than
/// owning it, so resolving one allocates nothing. The rejection path can still
/// name the key, which keeps formatting off the hot path.
#[derive(Debug, Clone, Copy)]
enum KeySource<'a> {
    Ip(IpAddr),
    Header(&'a str),
    Identity(&'a str),
    Unknown,
}

impl KeySource<'_> {
    /// The map key. The leading tag keeps two sources apart on one route: an
    /// identity-keyed route falls back to the address, and an identity that
    /// reads like an address must not join that address bucket.
    #[inline]
    fn bucket(&self) -> LimiterKey {
        let mut hasher = KEY_HASHER.build_hasher();
        match self {
            KeySource::Ip(ip) => (0u8, ip).hash(&mut hasher),
            KeySource::Header(value) => (1u8, value).hash(&mut hasher),
            KeySource::Identity(value) => (2u8, value).hash(&mut hasher),
            KeySource::Unknown => 3u8.hash(&mut hasher),
        }
        LimiterKey(hasher.finish())
    }
}

impl fmt::Display for KeySource<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeySource::Ip(ip) => ip.fmt(f),
            KeySource::Header(value) => f.write_str(value),
            KeySource::Identity(value) => f.write_str(value),
            KeySource::Unknown => f.write_str("unknown"),
        }
    }
}

/// Per-key limiters. The quota is part of the identity: handler ids are reused
/// after a reload or by the next test app, and a stale limiter must not keep
/// an old quota alive.
static LIMITERS: Lazy<DashMap<(usize, u32, u32, LimiterKey), Arc<Limiter>>> =
    Lazy::new(DashMap::new);

// Track total limiter count for cleanup
static LIMITER_COUNT: AtomicUsize = AtomicUsize::new(0);

// SECURITY: Maximum number of rate limiters to prevent memory exhaustion
const MAX_LIMITERS: usize = 100_000;

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
    let source = match &config.key {
        RateLimitKey::Ip => client_ip.map(|ip| KeySource::Ip(*ip)),
        // Custom header key — already lowercased once at startup when the
        // RateLimitConfig was parsed (headers map stores lowercase names).
        // The value is hashed rather than stored, so its length is not capped:
        // `key="authorization"` with a JWT works.
        RateLimitKey::Header(name) => headers
            .get(name)
            .map(|value| KeySource::Header(value.as_str())),
        // Identity keys run after authentication. A caller with no identity
        // is limited per client address, so an unauthenticated flood cannot
        // pick a fresh bucket per request.
        RateLimitKey::User => identity_source(auth_ctx, client_ip, |_| true),
        RateLimitKey::ApiKey => {
            identity_source(auth_ctx, client_ip, |ctx| ctx.backend == "api_key")
        }
    }
    .unwrap_or(KeySource::Unknown);

    // SECURITY: Check if we've exceeded max limiters (prevent memory exhaustion)
    let current_count = LIMITER_COUNT.load(Ordering::Relaxed);
    if current_count >= MAX_LIMITERS {
        // Trigger cleanup of old limiters (simple LRU-style)
        cleanup_old_limiters();
    }

    // Get or create rate limiter for this handler + key combination
    let limiter = LIMITERS
        .entry((handler_id, rps, burst, source.bucket()))
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
                method, path, source, rps, burst, retry_after
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
fn identity_source<'a>(
    auth_ctx: Option<&'a AuthContext>,
    client_ip: Option<&IpAddr>,
    accept: impl Fn(&AuthContext) -> bool,
) -> Option<KeySource<'a>> {
    auth_ctx
        .filter(|ctx| accept(ctx))
        .and_then(|ctx| ctx.user_id.as_deref())
        .map(KeySource::Identity)
        .or_else(|| client_ip.map(|ip| KeySource::Ip(*ip)))
}

/// Cleanup old rate limiters when limit is reached
/// Simple strategy: remove 20% of limiters to make room for new ones
fn cleanup_old_limiters() {
    let to_remove = (MAX_LIMITERS as f64 * 0.2) as usize;
    let mut removed = 0;

    // Remove first N entries (simple cleanup, not LRU)
    LIMITERS.retain(|_, _| {
        if removed < to_remove {
            removed += 1;
            LIMITER_COUNT.fetch_sub(1, Ordering::Relaxed);
            false // Remove this entry
        } else {
            true // Keep this entry
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(value: &str) -> IpAddr {
        value.parse().unwrap()
    }

    #[test]
    fn same_key_material_lands_in_one_bucket() {
        assert_eq!(
            KeySource::Header("tenant-a").bucket(),
            KeySource::Header("tenant-a").bucket()
        );
        assert_eq!(
            KeySource::Ip(ip("203.0.113.7")).bucket(),
            KeySource::Ip(ip("203.0.113.7")).bucket()
        );
        assert_eq!(KeySource::Unknown.bucket(), KeySource::Unknown.bucket());
    }

    #[test]
    fn different_key_material_lands_in_different_buckets() {
        assert_ne!(
            KeySource::Header("tenant-a").bucket(),
            KeySource::Header("tenant-b").bucket()
        );
        assert_ne!(
            KeySource::Ip(ip("203.0.113.7")).bucket(),
            KeySource::Ip(ip("203.0.113.8")).bucket()
        );
        assert_ne!(
            KeySource::Identity("7").bucket(),
            KeySource::Identity("8").bucket()
        );
    }

    #[test]
    fn the_tag_keeps_two_sources_apart() {
        // An identity-keyed route falls back to the address, so both sources
        // reach one map. An identity that reads like an address must not join
        // that address bucket.
        assert_ne!(
            KeySource::Identity("203.0.113.7").bucket(),
            KeySource::Ip(ip("203.0.113.7")).bucket()
        );
        assert_ne!(
            KeySource::Header("203.0.113.7").bucket(),
            KeySource::Ip(ip("203.0.113.7")).bucket()
        );
        assert_ne!(
            KeySource::Header("unknown").bucket(),
            KeySource::Unknown.bucket()
        );
    }

    #[test]
    fn a_long_key_is_accepted_and_stays_distinct() {
        // Nothing caps the length now: the map holds the hash, not the value.
        // `key="authorization"` with a JWT goes through here.
        let long_a = "t".repeat(4096);
        let long_b = format!("{}x", "t".repeat(4095));
        assert_eq!(
            KeySource::Header(&long_a).bucket(),
            KeySource::Header(&long_a).bucket()
        );
        assert_ne!(
            KeySource::Header(&long_a).bucket(),
            KeySource::Header(&long_b).bucket()
        );
    }

    #[test]
    fn concatenation_does_not_collide() {
        // "ab" + "c" must not hash like "a" + "bc".
        assert_ne!(
            KeySource::Header("ab").bucket(),
            KeySource::Header("a").bucket()
        );
        assert_ne!(
            KeySource::Identity("ab").bucket(),
            KeySource::Identity("a").bucket()
        );
    }
}
