use actix_web::HttpResponse;
use ahash::{AHashMap, RandomState};
use dashmap::DashMap;
use governor::clock::{Clock, DefaultClock, Reference};
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use once_cell::sync::Lazy;
use std::fmt;
use std::hash::{BuildHasher, Hash, Hasher};
use std::net::{IpAddr, Ipv6Addr};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::Duration;

use crate::metadata::{RateLimitConfig, RateLimitKey};
use crate::middleware::auth::AuthContext;
use crate::response_builder;
use crate::responses;

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;
type Instant = <DefaultClock as Clock>::Instant;

/// One bucket: its limiter, and the state of its 429 log line.
struct Bucket {
    limiter: Limiter,
    /// When the bucket was made. The log counts its seconds from here.
    created: Instant,
    /// The second of the last log line, or `u64::MAX` before the first line.
    logged_second: AtomicU64,
    /// The rejections since that line that wrote no line.
    unlogged: AtomicU64,
}

/// A rejected request: the wait for the next request, and whether to log it.
struct Rejection {
    wait: Duration,
    /// The number of rejections that wrote no line since the last line, or
    /// `None` when this rejection writes no line.
    log: Option<u64>,
}

impl Bucket {
    fn new(quota: Quota) -> Self {
        let limiter = RateLimiter::direct(quota);
        let created = limiter.clock().now();
        Self {
            limiter,
            created,
            logged_second: AtomicU64::new(u64::MAX),
            unlogged: AtomicU64::new(0),
        }
    }

    /// Take one request. The limiter reads its own clock, because a new clock
    /// runs `cpuid` to select its source, and a virtual machine traps that
    /// instruction.
    #[inline]
    fn take(&self) -> Result<(), Rejection> {
        self.limiter.check().map_err(|not_until| {
            let now = self.limiter.clock().now();
            Rejection {
                wait: not_until.wait_time_from(now),
                log: self.log_at(now),
            }
        })
    }

    /// Decide if a rejection at `now` writes a log line. The first rejection
    /// of each second does, so a flood writes one line per second for each
    /// bucket. The line counts the rejections before it that wrote none.
    fn log_at(&self, now: Instant) -> Option<u64> {
        let second = u64::from(now.duration_since(self.created)) / 1_000_000_000;
        let last = self.logged_second.load(Ordering::Relaxed);
        if last != second
            && self
                .logged_second
                .compare_exchange(last, second, Ordering::Relaxed, Ordering::Relaxed)
                .is_ok()
        {
            return Some(self.unlogged.swap(0, Ordering::Relaxed));
        }
        self.unlogged.fetch_add(1, Ordering::Relaxed);
        None
    }
}

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
    /// The network of an IPv6 client, with its prefix length.
    Ipv6Network(Ipv6Addr, u8),
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
            KeySource::Ipv6Network(network, _) => (4u8, network).hash(&mut hasher),
        }
        LimiterKey(hasher.finish())
    }
}

/// What the rejection log is allowed to name. Never the key material: the
/// `api_key` backend stores the raw credential in `AuthContext::user_id`
/// (`AuthContext::from_api_key`), and a header key may hold an `Authorization`
/// value. Both would otherwise reach stderr on a 429.
///
/// A bucket keeps one label for the life of the process, so an operator can
/// still see that the same caller keeps hitting the limit.
impl fmt::Display for KeySource<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            // An address is not a credential, and is the useful thing to read.
            KeySource::Ip(ip) => ip.fmt(f),
            KeySource::Ipv6Network(network, prefix) => write!(f, "{network}/{prefix}"),
            KeySource::Header(_) => write!(f, "header:{:016x}", self.bucket().0),
            KeySource::Identity(_) => write!(f, "identity:{:016x}", self.bucket().0),
            KeySource::Unknown => f.write_str("unknown"),
        }
    }
}

/// Per-key limiters of one server process. The quota is part of the
/// identity: handler ids are reused after a reload, and a stale limiter must
/// not keep an old quota alive.
///
/// `runbolt` makes one for all workers. A test app makes its own, because
/// each test app starts its handler ids at 0.
pub struct RateLimiters {
    /// Keyed on handler id, quota and client key. The client key is already
    /// a seeded hash, so the map hashes with ahash, not SipHash.
    limiters: DashMap<(usize, u32, u32, LimiterKey), Arc<Bucket>, RandomState>,
    /// The number of limiters, for cleanup.
    count: AtomicUsize,
    /// The prefix length of the network that an IPv6 client keys on, and its
    /// mask. See `client`.
    ipv6_prefix: u8,
    ipv6_mask: u128,
}

/// The default of `BOLT_RATE_LIMIT_IPV6_PREFIX`. A provider gives one client a
/// /64 or more, so a client can send from 2^64 addresses.
pub const DEFAULT_IPV6_PREFIX: u8 = 64;

impl Default for RateLimiters {
    fn default() -> Self {
        Self::new(DEFAULT_IPV6_PREFIX)
    }
}

// SECURITY: Maximum number of rate limiters to prevent memory exhaustion
const MAX_LIMITERS: usize = 100_000;

/// The check for address and header keys. Runs before authentication so a
/// flood never pays for token verification. Identity keys return `None` here.
#[inline]
pub fn check_before_auth(
    limiters: &RateLimiters,
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
    check_rate_limit(
        limiters, handler_id, headers, client_ip, None, config, method, path,
    )
}

/// The check for identity keys. Runs after authentication, with the
/// `AuthContext` of the request. Other keys return `None` here.
#[inline]
#[expect(
    clippy::too_many_arguments,
    reason = "the hot path passes borrowed request parts, with no struct to build"
)]
pub fn check_after_auth(
    limiters: &RateLimiters,
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
    check_rate_limit(
        limiters, handler_id, headers, client_ip, auth_ctx, config, method, path,
    )
}

#[expect(
    clippy::too_many_arguments,
    reason = "the hot path passes borrowed request parts, with no struct to build"
)]
pub fn check_rate_limit(
    limiters: &RateLimiters,
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
    let client = || client_ip.map(|ip| limiters.client(*ip));
    let source = match &config.key {
        RateLimitKey::Ip => client(),
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
        RateLimitKey::User => identity(auth_ctx, |_| true).or_else(client),
        RateLimitKey::ApiKey => identity(auth_ctx, |ctx| ctx.backend == "api_key").or_else(client),
    }
    .unwrap_or(KeySource::Unknown);

    // A known key takes a shard read lock, so requests of one client on many
    // workers do not wait for each other. The shard lock ends before the log
    // line of a rejection.
    let key = (handler_id, rps, burst, source.bucket());
    let outcome = match limiters.limiters.get(&key) {
        Some(bucket) => bucket.take(),
        None => {
            // SECURITY: Check if we've exceeded max limiters (prevent memory exhaustion)
            if limiters.count.load(Ordering::Relaxed) >= MAX_LIMITERS {
                // Trigger cleanup of old limiters (simple LRU-style)
                limiters.cleanup_old_limiters();
            }
            let bucket = limiters.limiters.entry(key).or_insert_with(|| {
                // Increment counter
                limiters.count.fetch_add(1, Ordering::Relaxed);

                // Use NonZero constructors properly
                let rps_nonzero = std::num::NonZeroU32::new(rps.max(1)).unwrap();
                let burst_nonzero = std::num::NonZeroU32::new(burst.max(1)).unwrap();
                let quota = Quota::per_second(rps_nonzero).allow_burst(burst_nonzero);
                Arc::new(Bucket::new(quota))
            });
            bucket.take()
        }
    };

    // Check rate limit
    match outcome {
        Ok(_) => None, // Request allowed
        Err(rejection) => {
            // Calculate retry after in seconds
            let retry_after = rejection.wait.as_secs().max(1);

            // Log the first rejection of each second for this bucket.
            if let Some(unlogged) = rejection.log {
                eprintln!(
                    "[django-bolt] Rate limit exceeded: {} {} | key: {} | limit: {} rps (burst: {}) | retry after: {}s | suppressed: {}",
                    method, path, source, rps, burst, retry_after, unlogged
                );
            }

            let response = response_builder::build_rate_limit_response(
                retry_after,
                rps,
                burst,
                responses::get_rate_limit_body(retry_after),
            );
            Some(crate::error::with_error_info(response, || {
                crate::error::ErrorInfo::Core {
                    kind: "rate_limited",
                    detail: format!("Rate limit exceeded. Try again in {} seconds.", retry_after)
                        .into(),
                    errors: None,
                    extra: Some(serde_json::json!({ "retry_after": retry_after })),
                    reason: None,
                }
            }))
        }
    }
}

/// The authenticated identity when `accept` admits the backend.
#[inline]
fn identity(
    auth_ctx: Option<&AuthContext>,
    accept: impl Fn(&AuthContext) -> bool,
) -> Option<KeySource<'_>> {
    auth_ctx
        .filter(|ctx| accept(ctx))
        .and_then(|ctx| ctx.user_id.as_deref())
        .map(KeySource::Identity)
}

impl RateLimiters {
    /// Limiters that key an IPv6 client on its first `ipv6_prefix` bits. A
    /// prefix of 128 or more keys on the full address.
    pub fn new(ipv6_prefix: u8) -> Self {
        Self {
            limiters: DashMap::default(),
            count: AtomicUsize::new(0),
            ipv6_prefix,
            // The host bits are `MAX >> prefix`. A prefix over 128 keeps all bits.
            ipv6_mask: !(u128::MAX.checked_shr(u32::from(ipv6_prefix)).unwrap_or(0)),
        }
    }

    /// The key of a client address. An IPv6 client keys on its network, so it
    /// cannot get a fresh bucket for each address of that network. An IPv4
    /// client keys on its address.
    #[inline]
    fn client(&self, ip: IpAddr) -> KeySource<'static> {
        match ip {
            IpAddr::V6(v6) if self.ipv6_prefix < 128 => KeySource::Ipv6Network(
                Ipv6Addr::from(u128::from(v6) & self.ipv6_mask),
                self.ipv6_prefix,
            ),
            ip => KeySource::Ip(ip),
        }
    }

    /// Cleanup old rate limiters when limit is reached
    /// Simple strategy: remove 20% of limiters to make room for new ones
    fn cleanup_old_limiters(&self) {
        let to_remove = (MAX_LIMITERS as f64 * 0.2) as usize;
        let mut removed = 0;

        // Remove first N entries (simple cleanup, not LRU)
        self.limiters.retain(|_, _| {
            if removed < to_remove {
                removed += 1;
                self.count.fetch_sub(1, Ordering::Relaxed);
                false // Remove this entry
            } else {
                true // Keep this entry
            }
        });
    }
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
    fn the_log_label_never_carries_key_material() {
        // `AuthContext::from_api_key` stores `apikey:<raw key>` in `user_id`,
        // and a header key may be an `Authorization` value. Neither may reach
        // the rejection log.
        let secret = "sk-live-DO-NOT-LOG-4f2b9c";

        let identity = KeySource::Identity(&format!("apikey:{secret}")).to_string();
        assert!(
            !identity.contains(secret),
            "identity label leaked: {identity}"
        );
        assert!(
            identity.starts_with("identity:"),
            "unexpected label: {identity}"
        );

        let header = KeySource::Header(&format!("Bearer {secret}")).to_string();
        assert!(!header.contains(secret), "header label leaked: {header}");
        assert!(header.starts_with("header:"), "unexpected label: {header}");
    }

    #[test]
    fn the_log_label_still_identifies_one_caller() {
        // An operator has to see that the same caller keeps being limited.
        let one = KeySource::Identity("apikey:aaa").to_string();
        let same = KeySource::Identity("apikey:aaa").to_string();
        let other = KeySource::Identity("apikey:bbb").to_string();
        assert_eq!(one, same);
        assert_ne!(one, other);

        // An address is not a credential, and is the useful thing to read.
        assert_eq!(KeySource::Ip(ip("203.0.113.7")).to_string(), "203.0.113.7");
        assert_eq!(KeySource::Unknown.to_string(), "unknown");
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

    #[test]
    fn a_bucket_logs_the_first_rejection_of_each_second() {
        use governor::nanos::Nanos;

        let quota = Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let bucket = Bucket::new(quota);
        let at = |nanos: u64| bucket.created + Nanos::from(nanos);
        assert_eq!(bucket.log_at(at(5_200_000_000)), Some(0));
        assert_eq!(bucket.log_at(at(5_700_000_000)), None);
        assert_eq!(bucket.log_at(at(5_900_000_000)), None);
        // The next second logs, and counts the two rejections before it.
        assert_eq!(bucket.log_at(at(6_100_000_000)), Some(2));
        assert_eq!(bucket.log_at(at(6_200_000_000)), None);
        assert_eq!(bucket.log_at(at(8_000_000_000)), Some(1));
        // The first second of a new bucket logs too.
        let fresh = Bucket::new(quota);
        assert_eq!(fresh.log_at(fresh.created), Some(0));
        assert_eq!(fresh.log_at(fresh.created), None);
    }

    #[test]
    fn a_full_map_drops_a_fifth_of_its_limiters() {
        let limiters = RateLimiters::default();
        let config = RateLimitConfig {
            rps: 100,
            burst: 100,
            key: RateLimitKey::Ip,
        };
        // Fill the map. The entries share one limiter, so the fill stays fast.
        let quota = Quota::per_second(std::num::NonZeroU32::new(100).unwrap());
        let shared = Arc::new(Bucket::new(quota));
        for n in 0..MAX_LIMITERS as u64 {
            limiters
                .limiters
                .insert((1, 100, 100, LimiterKey(n)), Arc::clone(&shared));
        }
        limiters.count.store(MAX_LIMITERS, Ordering::Relaxed);

        let address = ip("203.0.113.7");
        let response = check_before_auth(
            &limiters,
            0,
            &AHashMap::new(),
            Some(&address),
            &config,
            "GET",
            "/",
        );
        assert!(response.is_none());
        // The new key found the map full. The cleanup dropped 20 000 limiters first.
        let expected = MAX_LIMITERS - MAX_LIMITERS / 5 + 1;
        assert_eq!(limiters.limiters.len(), expected);
        assert_eq!(limiters.count.load(Ordering::Relaxed), expected);
    }

    /// The context of a request that `backend` authenticated as `user_id`.
    fn identity(backend: &'static str, user_id: &str) -> AuthContext {
        AuthContext {
            user_id: Some(user_id.to_string()),
            is_staff: false,
            is_superuser: false,
            backend: std::borrow::Cow::Borrowed(backend),
            backend_index: Some(0),
            claims: None,
            permissions: std::collections::HashSet::new(),
            cookie_csrf: false,
        }
    }

    /// One request from `address` through both checks of a route that allows
    /// one request, as the handler runs them. Returns the rejection.
    fn request(
        limiters: &RateLimiters,
        key: &RateLimitKey,
        address: &str,
        auth_ctx: Option<&AuthContext>,
    ) -> Option<HttpResponse> {
        let config = RateLimitConfig {
            rps: 1,
            burst: 1,
            key: key.clone(),
        };
        let headers = AHashMap::new();
        let address = ip(address);
        check_before_auth(limiters, 0, &headers, Some(&address), &config, "GET", "/").or_else(
            || {
                check_after_auth(
                    limiters,
                    0,
                    &headers,
                    Some(&address),
                    auth_ctx,
                    &config,
                    "GET",
                    "/",
                )
            },
        )
    }

    #[test]
    fn a_user_key_limits_each_identity_after_auth() {
        let limiters = RateLimiters::default();
        let alice = identity("jwt", "7");
        let bob = identity("jwt", "8");
        let key = RateLimitKey::User;
        assert!(request(&limiters, &key, "203.0.113.7", Some(&alice)).is_none());

        let rejected = request(&limiters, &key, "203.0.113.7", Some(&alice))
            .expect("the second request of one identity is limited");
        assert_eq!(rejected.status(), 429);
        for (name, value) in [
            ("retry-after", "1"),
            ("x-ratelimit-limit", "1"),
            ("x-ratelimit-burst", "1"),
        ] {
            assert_eq!(rejected.headers().get(name).unwrap(), value, "{name}");
        }

        // Another identity from the same address has its own bucket.
        assert!(request(&limiters, &key, "203.0.113.7", Some(&bob)).is_none());
        // The bucket of an identity does not depend on the address.
        assert!(request(&limiters, &key, "203.0.113.9", Some(&alice)).is_some());
        // With no identity, the address is the key.
        assert!(request(&limiters, &key, "203.0.113.9", None).is_none());
        assert!(request(&limiters, &key, "203.0.113.9", None).is_some());
        assert!(request(&limiters, &key, "203.0.113.10", None).is_none());
    }

    #[test]
    fn each_check_runs_only_its_own_keys() {
        let limiters = RateLimiters::default();
        let config = |key| RateLimitConfig {
            rps: 1,
            burst: 1,
            key,
        };
        let headers = AHashMap::new();
        let address = ip("203.0.113.7");
        let alice = identity("jwt", "7");
        for _ in 0..3 {
            // An address key runs before authentication, so the check after
            // it never counts the request a second time.
            let by_address = config(RateLimitKey::Ip);
            assert!(
                check_after_auth(
                    &limiters,
                    0,
                    &headers,
                    Some(&address),
                    Some(&alice),
                    &by_address,
                    "GET",
                    "/"
                )
                .is_none()
            );
            // An identity key needs the context, so the check before
            // authentication leaves it alone.
            let by_user = config(RateLimitKey::User);
            assert!(
                check_before_auth(&limiters, 1, &headers, Some(&address), &by_user, "GET", "/")
                    .is_none()
            );
        }
        assert_eq!(limiters.limiters.len(), 0);
    }

    #[test]
    fn an_ipv6_client_keys_on_its_network() {
        let limiters = RateLimiters::default();
        let key = RateLimitKey::Ip;
        assert!(request(&limiters, &key, "2001:db8:1:2::1", None).is_none());
        // Another address of the same /64 shares the bucket.
        assert!(request(&limiters, &key, "2001:db8:1:2:ffff::9", None).is_some());
        // The next /64 is another network.
        assert!(request(&limiters, &key, "2001:db8:1:3::1", None).is_none());

        // A caller with no identity falls back to its network too.
        let user = RateLimitKey::User;
        assert!(request(&limiters, &user, "2001:db8:1:4::1", None).is_none());
        assert!(request(&limiters, &user, "2001:db8:1:4::2", None).is_some());
    }

    #[test]
    fn the_ipv6_prefix_sets_the_network() {
        let key = RateLimitKey::Ip;
        // 128 keys on the full address.
        let full = RateLimiters::new(128);
        assert!(request(&full, &key, "2001:db8::1", None).is_none());
        assert!(request(&full, &key, "2001:db8::2", None).is_none());
        // 48 puts the /64 networks of one /48 in one bucket.
        let wide = RateLimiters::new(48);
        assert!(request(&wide, &key, "2001:db8:1:1::1", None).is_none());
        assert!(request(&wide, &key, "2001:db8:1:2::1", None).is_some());
        assert!(request(&wide, &key, "2001:db8:2:1::1", None).is_none());
        // 0 puts all IPv6 clients in one bucket. IPv4 still keys on the address.
        let none = RateLimiters::new(0);
        assert!(request(&none, &key, "2001:db8::1", None).is_none());
        assert!(request(&none, &key, "3fff::1", None).is_some());
        assert!(request(&none, &key, "203.0.113.7", None).is_none());
        assert!(request(&none, &key, "203.0.113.8", None).is_none());
    }

    #[test]
    fn the_log_label_names_the_ipv6_network() {
        let limiters = RateLimiters::default();
        let label = |limiters: &RateLimiters, address| limiters.client(ip(address)).to_string();
        assert_eq!(
            label(&limiters, "2001:db8:1:2:3:4:5:6"),
            "2001:db8:1:2::/64"
        );
        assert_eq!(label(&limiters, "203.0.113.7"), "203.0.113.7");
        assert_eq!(label(&RateLimiters::new(128), "2001:db8::1"), "2001:db8::1");
    }

    #[test]
    fn an_api_key_route_counts_only_api_key_identities() {
        let limiters = RateLimiters::default();
        let key = RateLimitKey::ApiKey;
        // Two keys from one address have two buckets.
        let first = identity("api_key", "apikey:aaa");
        let second = identity("api_key", "apikey:bbb");
        assert!(request(&limiters, &key, "203.0.113.7", Some(&first)).is_none());
        assert!(request(&limiters, &key, "203.0.113.7", Some(&second)).is_none());
        assert!(request(&limiters, &key, "203.0.113.7", Some(&first)).is_some());
        // The bucket of an API key does not depend on the address.
        assert!(request(&limiters, &key, "203.0.113.9", Some(&first)).is_some());

        // A JWT is not an API key. Two JWT users from one address share the
        // bucket of that address.
        let alice = identity("jwt", "7");
        let bob = identity("jwt", "8");
        assert!(request(&limiters, &key, "203.0.113.9", Some(&alice)).is_none());
        assert!(request(&limiters, &key, "203.0.113.9", Some(&bob)).is_some());
    }
}
