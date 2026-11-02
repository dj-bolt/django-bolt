#!/usr/bin/env bash
# Compare optimized upstream and working-tree snapshots without creating commits.
# Usage: scripts/bench_websocket_origin_ab.sh [new_results_dir]
# Set AFTER_REF to a commit to exclude working-tree changes intentionally.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
BEFORE_REF="${BEFORE_REF:-upstream/master}"
AFTER_REF="${AFTER_REF:-WORKING_TREE}"
RESULTS_DIR="${1:-$REPO/websocket-origin-ab-results/release-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/bolt-origin-ab.XXXXXXXX")"
BEFORE="$TMP/before"
AFTER="$TMP/after"

cleanup() {
    git -C "$REPO" worktree remove --force "$BEFORE" >/dev/null 2>&1 || true
    git -C "$REPO" worktree remove --force "$AFTER" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT

before_commit="$(git -C "$REPO" rev-parse --verify "$BEFORE_REF^{commit}")"
if [ "$AFTER_REF" = WORKING_TREE ]; then
    results_relative=""
    if [[ "$RESULTS_DIR" == "$REPO/"* ]]; then
        results_relative="${RESULTS_DIR#"$REPO"/}"
    fi
    while IFS= read -r -d '' untracked_file; do
        # Existing and newly written benchmark outputs are not source inputs.
        case "$untracked_file" in
            websocket-origin-ab-results/*) continue ;;
        esac
        if [[ -n "$results_relative" && "$untracked_file" == "$results_relative/"* ]]; then
            continue
        fi
        printf 'Untracked source not included in WORKING_TREE benchmark: %s\n' "$untracked_file" >&2
        exit 1
    done < <(git -C "$REPO" ls-files --others --exclude-standard -z)
    after_commit="$(git -C "$REPO" rev-parse HEAD)"
    git -C "$REPO" diff --binary HEAD > "$RESULTS_DIR/final.patch"
else
    after_commit="$(git -C "$REPO" rev-parse --verify "$AFTER_REF^{commit}")"
    : > "$RESULTS_DIR/final.patch"
fi
git -C "$REPO" worktree add --detach "$BEFORE" "$before_commit"
git -C "$REPO" worktree add --detach "$AFTER" "$after_commit"
if [ -s "$RESULTS_DIR/final.patch" ]; then
    git -C "$AFTER" apply "$RESULTS_DIR/final.patch"
fi

# Use the same measurement code with both implementations.
for tree in "$BEFORE" "$AFTER"; do
    mkdir -p "$tree/python/benchmark"
    cp "$REPO/python/benchmark/websocket_origin_benchmark.py" \
       "$tree/python/benchmark/websocket_origin_benchmark.py"
done
printf 'before=%s\nafter_base=%s\nafter_mode=%s\n' \
    "$before_commit" "$after_commit" "$AFTER_REF" > "$RESULTS_DIR/refs.txt"
sha256sum "$RESULTS_DIR/final.patch" > "$RESULTS_DIR/patch.sha256"
sha256sum "$REPO/python/benchmark/websocket_origin_benchmark.py" > "$RESULTS_DIR/measurement.sha256"
rustc -vV > "$RESULTS_DIR/rustc.txt"
# Hash every tracked snapshot file before builds write generated files.
for name in before after; do
    tree="$TMP/$name"
    (cd "$tree" && git ls-files -z | sort -z | xargs -0 sha256sum) > "$RESULTS_DIR/$name-source.sha256"
done

build_case() {
    local tree="$1"
    local name="$2"
    (
        cd "$tree"
        uv sync --frozen --no-install-project --python "$REPO/.venv/bin/python"
        CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR="$REPO/target/ws-origin-ab/$name" \
            uv run --frozen --no-sync maturin build --release --locked --out "$tree/.bench-wheels"
        uv pip install --python .venv/bin/python --no-deps "$tree"/.bench-wheels/*.whl
        .venv/bin/python -c 'import importlib.metadata as m; import json; print(json.dumps(sorted((d.metadata["Name"], d.version) for d in m.distributions())))' > "$RESULTS_DIR/$name-packages.json"
        .venv/bin/python -c 'import django_bolt._core as c; import hashlib; from pathlib import Path; p=Path(c.__file__); print(p); print(hashlib.sha256(p.read_bytes()).hexdigest())'
    )
}
build_case "$BEFORE" before > "$RESULTS_DIR/before-build.log" 2>&1 &
before_pid=$!
build_case "$AFTER" after > "$RESULTS_DIR/after-build.log" 2>&1 &
after_pid=$!
build_status=0
wait "$before_pid" || build_status=1
wait "$after_pid" || build_status=1
if [ "$build_status" != 0 ]; then
    echo "Release build failed. Inspect the build logs in $RESULTS_DIR" >&2
    exit 1
fi

run_case() {
    local name="$1"
    local iteration="$2"
    (
        cd "$TMP/$name"
        .venv/bin/python python/benchmark/websocket_origin_benchmark.py \
            --count "${BENCH_COUNT:-10000}" --repeats "${BENCH_REPEATS:-31}" \
            --roundtrips "${BENCH_ROUNDTRIPS:-250}" \
            --network-count "${BENCH_NETWORK_COUNT:-300}" --network-repeats "${BENCH_NETWORK_REPEATS:-5}" \
            --output "$RESULTS_DIR/$name-$iteration.json"
    ) > "$RESULTS_DIR/$name-$iteration.log" 2>&1
}
# Alternate order across three pairs. Build work finishes before measurements.
for iteration in 1 2 3; do
    if [ "$iteration" = 2 ]; then
        run_case after "$iteration"
        run_case before "$iteration"
    else
        run_case before "$iteration"
        run_case after "$iteration"
    fi
done
printf 'Results: %s\n' "$RESULTS_DIR"
