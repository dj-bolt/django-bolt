# Rust Source Code Guidelines

## Performance is King

**Think like a high-frequency quant engineer optimizing for throughput.** Performance is the #1 priority. Every change must be evaluated for its impact on latency and throughput.

### Performance Principles

1. **Zero unnecessary allocations** - Reuse buffers, use `&str` over `String` where possible
2. **Minimize GIL contention** - Do as much work as possible in Rust before/after Python calls
3. **No blocking in async paths** - Use `spawn_blocking` for any potentially blocking operation
4. **Cache aggressively** - Pre-compute headers, pre-compile regexes, cache auth results
5. **Batch operations** - Combine multiple small operations into single passes
6. **Avoid unnecessary copies** - Use references, `Cow<str>`, zero-copy parsing
7. **Profile before optimizing** - Use `make save-bench` to measure actual impact

### Red Flags to Watch For

- `clone()` in hot paths - question every clone
- `String::from()` or `.to_string()` in loops
- `Vec::new()` without `with_capacity()`
- Multiple passes over the same data
- Holding locks longer than necessary
- Allocating in request handlers when pre-allocation is possible
- Any operation that scales with request count rather than being O(1)

### Benchmark Every Change

```bash
# Before making changes
make save-bench

# After changes - compare
cat BENCHMARK_BASELINE.md BENCHMARK_DEV.md
```

If RPS drops, the change needs optimization or rejection.

---

## testing.rs - Maximize Production Code Reuse

`testing.rs` provides the TestClient infrastructure for testing. It should **always reuse production code** instead of duplicating functionality.

### Why This Matters

- Ensures tests validate the same code paths as production
- Prevents bugs where tests pass but production fails (or vice versa)
- Reduces maintenance burden - fixes in one place apply everywhere

### Examples

**Response building**: Use `response_builder::build_response_with_headers()` instead of manually iterating headers:

```rust
// GOOD: Reuses production code
let http_response = crate::response_builder::build_response_with_headers(
    status,
    headers,
    skip_compression,
    body,
);

// BAD: Duplicates production logic
let mut response = HttpResponse::build(status);
for (name, value) in &headers {
    response.append_header((name.as_str(), value.as_str()));
}
```

**CORS handling**: Use shared functions from `cors.rs`:
- `add_cors_response_headers()` - for adding CORS headers to responses
- `add_preflight_headers_simple()` - for OPTIONS preflight responses

**Authentication/Guards**: Use shared functions from `middleware/auth.rs` and `permissions.rs`:
- `authenticate()` - for running auth backends
- `evaluate_guards()` - for checking permissions

**Settings**: Read each Django setting in `ServerConfig::from_django_settings` (`server.rs`). `runbolt` and the test client both use it. Never read a setting a second time in `testing.rs` or in the Python test client.

**File scopes and CORS injection**: Use `configure_file_scopes()` and `inject_global_cors()` (`server.rs`).

### When Adding New Functionality

1. First implement in production modules (`handler.rs`, `response_builder.rs`, etc.)
2. Then call those functions from `testing.rs`
3. Never copy-paste production code into testing.rs
