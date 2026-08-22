use actix_web::HttpResponse;
use ahash::AHashMap;
use dashmap::DashMap;
use governor::clock::{Clock, DefaultClock};
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use once_cell::sync::Lazy;
use parking_lot::RwLock;
use std::hash::{Hash, Hasher};
use std::net::IpAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use crate::metadata::{RateLimitConfig, RateLimitKey};
use crate::response_builder;
use crate::responses;

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

// Store per-key limiters (IP-based)
static IP_LIMITERS: Lazy<DashMap<(usize, String), Arc<Limiter>>> = Lazy::new(DashMap::new);

// Track total limiter count for cleanup
static LIMITER_COUNT: AtomicUsize = AtomicUsize::new(0);

// SECURITY: Maximum number of rate limiters to prevent memory exhaustion
const MAX_LIMITERS: usize = 100_000;

// SECURITY: Keys longer than this are hashed down to a fixed-size digest so a
// long header value (for example a JWT) cannot bloat the limiter map.
const MAX_KEY_LENGTH: usize = 256;

/// Trusted proxy networks, parsed once at startup from `BOLT_TRUSTED_PROXIES`.
///
/// When the set is empty (the default), forwarding headers are ignored and the
/// client IP is the TCP peer address. When the peer is a trusted proxy, the
/// client IP is the rightmost `X-Forwarded-For` entry that is not itself a
/// trusted proxy.
#[derive(Debug, Default)]
pub struct TrustedProxies {
    nets: Vec<(IpAddr, u8)>,
}

impl TrustedProxies {
    /// Parse CIDR strings ("10.0.0.0/8", "::1/128") or bare IPs ("127.0.0.1").
    pub fn parse(specs: &[String]) -> Result<Self, String> {
        let mut nets = Vec::with_capacity(specs.len());
        for spec in specs {
            let (addr_part, prefix_part) = match spec.split_once('/') {
                Some((a, p)) => (a, Some(p)),
                None => (spec.as_str(), None),
            };
            let addr: IpAddr = addr_part
                .trim()
                .parse()
                .map_err(|_| format!("invalid IP address in trusted proxy entry: {spec:?}"))?;
            let max_prefix = match addr {
                IpAddr::V4(_) => 32u8,
                IpAddr::V6(_) => 128u8,
            };
            let prefix = match prefix_part {
                Some(p) => p
                    .trim()
                    .parse::<u8>()
                    .ok()
                    .filter(|p| *p <= max_prefix)
                    .ok_or_else(|| {
                        format!("invalid prefix length in trusted proxy entry: {spec:?}")
                    })?,
                None => max_prefix,
            };
            nets.push((addr, prefix));
        }
        Ok(TrustedProxies { nets })
    }

    pub fn is_empty(&self) -> bool {
        self.nets.is_empty()
    }

    pub fn contains(&self, ip: &IpAddr) -> bool {
        let ip = ip.to_canonical();
        self.nets.iter().any(|(net, prefix)| match (net, ip) {
            (IpAddr::V4(net), IpAddr::V4(ip)) => {
                let bits = u32::from(*net) ^ u32::from(ip);
                *prefix == 0 || bits >> (32 - *prefix as u32) == 0
            }
            (IpAddr::V6(net), IpAddr::V6(ip)) => {
                let bits = u128::from(*net) ^ u128::from(ip);
                *prefix == 0 || bits >> (128 - *prefix as u32) == 0
            }
            _ => false,
        })
    }
}

static TRUSTED_PROXIES: Lazy<RwLock<Arc<TrustedProxies>>> =
    Lazy::new(|| RwLock::new(Arc::new(TrustedProxies::default())));

/// Install the trusted proxy set. Called at server startup and at
/// `TestClient` creation with the value of `BOLT_TRUSTED_PROXIES`.
pub fn set_trusted_proxies(specs: &[String]) -> Result<(), String> {
    let parsed = TrustedProxies::parse(specs)?;
    *TRUSTED_PROXIES.write() = Arc::new(parsed);
    Ok(())
}

fn trusted_proxies() -> Arc<TrustedProxies> {
    TRUSTED_PROXIES.read().clone()
}

/// Read `BOLT_TRUSTED_PROXIES` from Django settings and install it.
///
/// An absent setting clears the set (forwarding headers are then ignored).
/// A malformed setting is a loud startup error, never a silent fallback.
pub fn configure_trusted_proxies_from_settings(py: pyo3::Python<'_>) -> pyo3::PyResult<()> {
    use pyo3::prelude::PyAnyMethods;

    let specs: Vec<String> = match py
        .import("django.conf")
        .and_then(|m| m.getattr("settings"))
        .and_then(|s| s.getattr("BOLT_TRUSTED_PROXIES"))
    {
        Ok(value) => value.extract()?,
        Err(_) => Vec::new(),
    };
    set_trusted_proxies(&specs)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("BOLT_TRUSTED_PROXIES: {e}")))
}

/// Resolve the client IP for rate limiting.
///
/// With no trusted proxies, forwarding headers are client-controlled and are
/// ignored: the key is the TCP peer address. With trusted proxies configured,
/// forwarding headers are honored only when the peer is trusted. A missing
/// peer (in-process test requests) counts as trusted so test suites can fake
/// client IPs; a real TCP connection always has a peer.
pub fn resolve_client_ip(
    headers: &AHashMap<String, String>,
    peer_addr: Option<&str>,
) -> Option<String> {
    resolve_client_ip_with(&trusted_proxies(), headers, peer_addr)
}

fn resolve_client_ip_with(
    proxies: &TrustedProxies,
    headers: &AHashMap<String, String>,
    peer_addr: Option<&str>,
) -> Option<String> {
    if proxies.is_empty() {
        return peer_addr.map(str::to_string);
    }

    let peer_trusted = match peer_addr {
        None => true,
        Some(peer) => peer
            .parse::<IpAddr>()
            .map(|ip| proxies.contains(&ip))
            .unwrap_or(false),
    };
    if !peer_trusted {
        return peer_addr.map(str::to_string);
    }

    if let Some(xff) = headers.get("x-forwarded-for") {
        // Walk right to left: the first entry that is not a trusted proxy is
        // the client. Entries to its left are client-controlled.
        let mut leftmost = None;
        for entry in xff.rsplit(',') {
            let entry = entry.trim();
            if entry.is_empty() {
                continue;
            }
            leftmost = Some(entry);
            match entry.parse::<IpAddr>() {
                Ok(ip) if proxies.contains(&ip) => continue,
                _ => return Some(entry.to_string()),
            }
        }
        // Every entry is a trusted proxy: the leftmost one originated the chain.
        if let Some(entry) = leftmost {
            return Some(entry.to_string());
        }
    }

    if let Some(real_ip) = headers.get("x-real-ip") {
        let trimmed = real_ip.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    }

    peer_addr.map(str::to_string)
}

/// Check the rate limit for one request.
///
/// `identity` is the authenticated identity (`AuthContext::user_id`); it is
/// only consulted for `key="user"` / `key="api_key"` routes, whose check runs
/// after auth. All other strategies run before auth with `identity: None`.
pub fn check_rate_limit(
    handler_id: usize,
    headers: &AHashMap<String, String>,
    peer_addr: Option<&str>,
    identity: Option<&str>,
    config: &RateLimitConfig,
    method: &str,
    path: &str,
) -> Option<HttpResponse> {
    // Config is already parsed at startup - no GIL needed!
    let rps = config.rps;
    let burst = config.burst;

    // Determine the rate limit key. Strategies that miss (anonymous request on
    // an identity-keyed route, absent header) fall back to the client IP so
    // those requests never pool into one shared bucket by accident.
    let key = match &config.key {
        RateLimitKey::Ip => resolve_client_ip(headers, peer_addr),
        RateLimitKey::User | RateLimitKey::ApiKey => identity
            .map(str::to_string)
            .or_else(|| resolve_client_ip(headers, peer_addr)),
        RateLimitKey::Header(header_name) => headers
            .get(header_name)
            .cloned()
            .or_else(|| resolve_client_ip(headers, peer_addr)),
    };
    let mut key = key.unwrap_or_else(|| "unknown".to_string());

    // SECURITY: hash oversized keys down to a fixed-size digest so a long
    // header value (a JWT in `authorization`, say) cannot bloat the limiter
    // map or get rejected outright.
    if key.len() > MAX_KEY_LENGTH {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        key.hash(&mut hasher);
        key = format!("h:{:016x}", hasher.finish());
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

/// Drop every limiter bucket. Test-only helper: keeps buckets from leaking
/// between test cases in one process.
pub fn reset_all_limiters() {
    IP_LIMITERS.clear();
    LIMITER_COUNT.store(0, Ordering::Relaxed);
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

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(pairs: &[(&str, &str)]) -> AHashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn parse_accepts_cidrs_and_bare_ips() {
        let proxies =
            TrustedProxies::parse(&["10.0.0.0/8".into(), "127.0.0.1".into(), "::1/128".into()])
                .unwrap();
        assert!(proxies.contains(&"10.1.2.3".parse().unwrap()));
        assert!(proxies.contains(&"127.0.0.1".parse().unwrap()));
        assert!(proxies.contains(&"::1".parse().unwrap()));
        assert!(!proxies.contains(&"11.0.0.1".parse().unwrap()));
        assert!(!proxies.contains(&"127.0.0.2".parse().unwrap()));
    }

    #[test]
    fn parse_rejects_bad_entries() {
        assert!(TrustedProxies::parse(&["not-an-ip".into()]).is_err());
        assert!(TrustedProxies::parse(&["10.0.0.0/33".into()]).is_err());
        assert!(TrustedProxies::parse(&["10.0.0.0/x".into()]).is_err());
    }

    #[test]
    fn contains_matches_ipv4_mapped_ipv6_peers() {
        let proxies = TrustedProxies::parse(&["127.0.0.1".into()]).unwrap();
        assert!(proxies.contains(&"::ffff:127.0.0.1".parse().unwrap()));
    }

    #[test]
    fn resolve_ignores_forwarding_headers_without_trusted_proxies() {
        let proxies = TrustedProxies::default();
        let h = headers(&[
            ("x-forwarded-for", "203.0.113.1"),
            ("x-real-ip", "203.0.113.2"),
        ]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("192.0.2.10")),
            Some("192.0.2.10".to_string())
        );
        assert_eq!(resolve_client_ip_with(&proxies, &h, None), None);
    }

    #[test]
    fn resolve_ignores_forwarding_headers_from_untrusted_peer() {
        let proxies = TrustedProxies::parse(&["10.0.0.0/8".into()]).unwrap();
        let h = headers(&[("x-forwarded-for", "203.0.113.1")]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("192.0.2.10")),
            Some("192.0.2.10".to_string())
        );
    }

    #[test]
    fn resolve_walks_forwarded_chain_right_to_left() {
        let proxies = TrustedProxies::parse(&["127.0.0.1".into(), "10.0.0.0/8".into()]).unwrap();
        // Peer trusted: rightmost untrusted entry wins, spoofed leftmost is ignored.
        let h = headers(&[("x-forwarded-for", "203.0.113.1, 198.51.100.9, 10.0.0.5")]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("127.0.0.1")),
            Some("198.51.100.9".to_string())
        );
        // Every entry trusted: the leftmost originated the chain.
        let h = headers(&[("x-forwarded-for", "10.0.0.9, 10.0.0.5")]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("127.0.0.1")),
            Some("10.0.0.9".to_string())
        );
        // A missing peer counts as trusted (in-process test requests).
        let h = headers(&[("x-forwarded-for", "203.0.113.7")]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, None),
            Some("203.0.113.7".to_string())
        );
    }

    #[test]
    fn resolve_falls_back_to_x_real_ip_then_peer_when_peer_trusted() {
        let proxies = TrustedProxies::parse(&["127.0.0.1".into()]).unwrap();
        let h = headers(&[("x-real-ip", "198.51.100.3")]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("127.0.0.1")),
            Some("198.51.100.3".to_string())
        );
        let h = headers(&[]);
        assert_eq!(
            resolve_client_ip_with(&proxies, &h, Some("127.0.0.1")),
            Some("127.0.0.1".to_string())
        );
    }
}
