#!/usr/bin/env bash
# Measure Python and Rust coverage over the Rust tests and the Python suites.
#
# The script builds an instrumented debug extension with cargo-llvm-cov, so
# the Python tests also measure the Rust code that they reach. The runbolt
# servers of the integration tests write their own coverage data.
#
# Needs: cargo-llvm-cov and the llvm-tools-preview rustup component.
# Output: $COVERAGE_DIR (default: coverage/) with summaries and HTML reports.
set -euo pipefail
shopt -s nullglob

out="${COVERAGE_DIR:-coverage}"
rm -rf "$out" .coverage .coverage.*
mkdir -p "$out"

# maturin develop writes the extension into the source tree, and each
# environment uses that file. Keep the current build and put it back at exit,
# so that a later test run or benchmark does not use the instrumented build.
saved="$(mktemp -d)"
for built in python/django_bolt/_core*.so python/django_bolt/_core*.pyd; do
    mv "$built" "$saved/"
done
# shellcheck disable=SC2329 # The EXIT trap below calls it.
restore() {
    rm -f python/django_bolt/_core*.so python/django_bolt/_core*.pyd
    for built in "$saved"/*; do
        mv "$built" python/django_bolt/
    done
    rmdir "$saved"
}
trap restore EXIT

# A separate target directory. cargo does not rebuild when only the coverage
# rustc wrapper changes, so a shared target/ would give later `cargo test` and
# `maturin develop` runs instrumented artifacts.
export CARGO_TARGET_DIR="$PWD/target/llvm-cov-target"
eval "$(cargo llvm-cov show-env --sh)"
cargo llvm-cov clean --workspace

# A test failure or a failed report must not hide the other reports, so
# record the status and go on.
status=0
# `uv run` makes PyO3 build against the project environment, not an older
# python3 on PATH. The Rust tests build first: the report reads the profiles
# of the Python runs against the last objects that the build wrote.
uv run --no-sync cargo test --workspace || status=1
uv run --no-sync maturin develop
uv run --no-sync pytest python/tests -m "not artifact_smoke" -n auto --dist loadfile \
    --cov --cov-report= || status=1
# bolt-mcp configures Django differently, so it runs in its own process.
uv run --no-sync pytest python/bolt-mcp/tests -m "not artifact_smoke" \
    --cov --cov-append --cov-report= || status=1

uv run --no-sync coverage report --format=markdown > "$out/python.md" || status=1
uv run --no-sync coverage html --directory "$out/python-html" --quiet || status=1
cargo llvm-cov report --workspace --summary-only > "$out/rust.txt" || status=1
cargo llvm-cov report --workspace --html --output-dir "$out/rust-html" || status=1

for summary in "$out/python.md" "$out/rust.txt"; do
    if [ -s "$summary" ]; then
        tail -n 1 "$summary"
    fi
done
exit "$status"
