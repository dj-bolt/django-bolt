#!/usr/bin/env bash
# Measure Python and Rust coverage over the Rust tests and the Python suites.
#
# The script builds an instrumented debug extension with cargo-llvm-cov, so
# the Python tests also measure the Rust code that they reach. The runbolt
# servers of the integration tests write their own coverage data.
#
# Needs: cargo-llvm-cov and the llvm-tools-preview rustup component.
# Output: $COVERAGE_DIR (default: coverage/) with summaries and HTML reports.
# The extension stays instrumented. Run `just build` after this script.
set -euo pipefail

out="${COVERAGE_DIR:-coverage}"
rm -rf "$out" .coverage .coverage.*
mkdir -p "$out"

eval "$(cargo llvm-cov show-env --sh)"
cargo llvm-cov clean --workspace
uv run --no-sync maturin develop

# A test failure must not hide the report, so record the status and go on.
status=0
cargo test --workspace || status=1
uv run --no-sync pytest python/tests -m "not artifact_smoke" -n auto --dist loadfile \
    --cov --cov-report= || status=1
# bolt-mcp configures Django differently, so it runs in its own process.
uv run --no-sync pytest python/bolt-mcp/tests -m "not artifact_smoke" \
    --cov --cov-append --cov-report= || status=1

uv run --no-sync coverage report --format=markdown > "$out/python.md"
uv run --no-sync coverage html --directory "$out/python-html" --quiet
cargo llvm-cov report --workspace --summary-only > "$out/rust.txt"
cargo llvm-cov report --workspace --html --output-dir "$out/rust-html"

tail -n 1 "$out/python.md"
tail -n 1 "$out/rust.txt"
exit "$status"
