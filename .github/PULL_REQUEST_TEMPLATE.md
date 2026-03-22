## What does this PR do?

<!-- Describe the change in 2-3 sentences. Link to any related issue. -->

Fixes #

## Performance consideration

<!-- Django-Bolt is a performance-critical framework. Explain why this change does not regress the hot path, or show benchmark numbers if it touches dispatch/serialization/routing. -->



## How was this tested?

<!-- We expect you to have actually run your change, not just written unit tests. -->

- [ ] `just lint-lib` and `just test-py` pass
- [ ] Tested manually in the example project (`python/example/`) — describe what you did below
- [ ] If Rust was changed: `just rebuild` succeeds

**Manual verification steps:**

1.
2.

## Benchmark results (if applicable)

<!-- Run `just save-bench` before and after. Required if touching hot path code. -->

```
Before: X req/s
After:  X req/s
```
