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
# Remove only the reports that this script writes. COVERAGE_DIR can name a
# directory with other files, and those files stay.
rm -rf .coverage .coverage.* "$out/python.md" "$out/python-html" "$out/rust.txt" "$out/rust-html"
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

# A process that is killed while it writes its coverage data leaves a data
# file that is not a valid database. The suites kill runbolt processes on
# purpose, and the slow instrumented build makes a kill during that write
# possible. coverage then cannot combine the file, and it leaves a database
# connection open. pytest-cov combines all data files at the end of a run, so
# the ResourceWarning fails the next pytest run, although each test passed.
# Remove such a file before the next combine, and say which one.
drop_broken_coverage_data() {
    uv run --no-sync python - <<'PY'
import contextlib
import glob
import os
import sqlite3
import sys

for path in sorted(glob.glob(".coverage.*")):
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            result = db.execute("PRAGMA quick_check").fetchone()[0]
            db.execute("SELECT count(*) FROM meta").fetchone()
        if result == "ok":
            continue
        reason = result
    except sqlite3.DatabaseError as exc:
        reason = str(exc)
    os.remove(path)
    message = f"Removed coverage data file {path} that coverage cannot read ({reason}). A process stopped during its write."
    print(f"::warning::{message}" if os.environ.get("GITHUB_ACTIONS") else message, file=sys.stderr)
PY
}

# A test failure or a failed report must not hide the other reports, so
# record the status and go on.
status=0
# `uv run` makes PyO3 build against the project environment, not an older
# python3 on PATH. The Rust test binaries link the libpython of that
# environment. A uv-managed Python keeps it in its own lib directory, which
# the Linux loader does not search, so add that directory.
pylib="$(uv run --no-sync python -c 'import os, sys; print(os.path.join(sys.base_prefix, "lib"))')"
export LD_LIBRARY_PATH="$pylib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# The Rust tests build first: the report reads the profiles of the Python runs
# against the last objects that the build wrote. A fixed proptest seed gives
# each run the same cases, as in CI, so two reports can be compared.
PROPTEST_RNG_SEED=0 uv run --no-sync cargo test --workspace || status=1
uv run --no-sync maturin develop
# A test that runs longer than 5 minutes writes the stack of each thread, so a
# hung run shows where it waits.
uv run --no-sync pytest python/tests -m "not artifact_smoke" -n auto --dist loadfile \
    -o faulthandler_timeout=300 --cov --cov-report= || status=1
drop_broken_coverage_data
# bolt-mcp configures Django differently, so it runs in its own process.
uv run --no-sync pytest python/bolt-mcp/tests -m "not artifact_smoke" \
    -o faulthandler_timeout=300 --cov --cov-append --cov-report= || status=1
drop_broken_coverage_data

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
