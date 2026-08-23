//! Allocations per rate-limited request.
//!
//! The RPS difference from hashing the bucket key is smaller than the noise
//! of an HTTP benchmark. This counts the mechanism instead, which is what the
//! change is about. See `docs/PROFILING.md`, "Allocation counting".

use ahash::AHashMap;
use bolt_core::metadata::{RateLimitConfig, RateLimitKey};
use bolt_core::middleware::rate_limit::check_before_auth;
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

thread_local! {
    // Per thread, so tests running in parallel do not count each other.
    // Const-initialized: a lazy one would allocate inside the allocator.
    static ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
}

struct Counting;

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // `try_with` because TLS is gone late in thread teardown.
        let _ = ALLOCATIONS.try_with(|count| count.set(count.get() + 1));
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

/// Allocations made on this thread while `body` runs.
fn count(body: impl FnOnce()) -> usize {
    let before = ALLOCATIONS.with(|count| count.get());
    body();
    ALLOCATIONS.with(|count| count.get()) - before
}

fn headers(value: &str) -> AHashMap<String, String> {
    let mut map = AHashMap::new();
    map.insert("x-tenant".to_string(), value.to_string());
    map
}

#[test]
fn a_header_keyed_request_allocates_nothing() {
    let config = RateLimitConfig {
        rps: 1_000_000,
        burst: 1_000_000,
        key: RateLimitKey::Header("x-tenant".to_string()),
    };
    let map = headers("acme");

    // The first request creates the limiter for this bucket, which allocates.
    // Steady state is what a served request costs.
    check_before_auth(9_001, &map, None, &config, "GET", "/limited");

    let allocations = count(|| {
        for _ in 0..1_000 {
            assert!(check_before_auth(9_001, &map, None, &config, "GET", "/limited").is_none());
        }
    });

    assert_eq!(
        allocations, 0,
        "expected no allocation per request, got {allocations} over 1000 requests"
    );
}

#[test]
fn a_long_header_value_allocates_nothing_either() {
    // The old code copied the value into the map key, so cost grew with it.
    // It also refused anything over 256 bytes.
    let config = RateLimitConfig {
        rps: 1_000_000,
        burst: 1_000_000,
        key: RateLimitKey::Header("x-tenant".to_string()),
    };
    let map = headers(&"t".repeat(4096));

    check_before_auth(9_002, &map, None, &config, "GET", "/limited");

    let allocations = count(|| {
        for _ in 0..1_000 {
            assert!(check_before_auth(9_002, &map, None, &config, "GET", "/limited").is_none());
        }
    });

    assert_eq!(
        allocations, 0,
        "expected no allocation per request, got {allocations} over 1000 requests"
    );
}
