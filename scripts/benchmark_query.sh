#!/bin/bash
# Focused benchmark for the HTTP QUERY method.
#
# QUERY is a safe, body-bearing request method. /bench/query does byte-identical
# work to /bench/parse (POST), so running both through the same httpx generator
# isolates the method's routing cost from everything else.
#
# bombardier can't send QUERY ("Unknown HTTP method"), so this uses the httpx
# generator in scripts/benchmark_query.py for both methods.

set -e

P=${P:-1}
WORKERS=${WORKERS:-1}
C=${C:-50}
N=${N:-10000}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8001}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SETSID_BIN=""
if command -v setsid &> /dev/null; then
    SETSID_BIN="setsid"
elif [ -f "/opt/homebrew/opt/util-linux/bin/setsid" ]; then
    SETSID_BIN="/opt/homebrew/opt/util-linux/bin/setsid"
fi
if [ -z "$SETSID_BIN" ]; then
    echo "ERROR: setsid not installed. Install with: apt install util-linux (Linux) or brew install util-linux (macOS)" >&2
    exit 1
fi

echo "# Django-Bolt HTTP QUERY Method Benchmark"
echo "Generated: $(date)"
echo "Config: $P processes x $WORKERS workers | C=$C N=$N"
echo ""

cd "$REPO_ROOT/python/example"
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host "$HOST" --port "$PORT" --processes "$P" >/dev/null 2>&1 &
SERVER_PID=$!

cleanup() {
    kill -TERM -"$SERVER_PID" 2>/dev/null || true
    pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for the server to answer 200 on /.
elapsed=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/")" = "200" ]; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 15 ]; then
        echo "Server not ready after 15s; aborting." >&2
        exit 1
    fi
done

BODY='{"title":"bench","count":100,"items":[{"name":"a","price":1.0,"is_offer":true}]}'

# Sanity check both endpoints return 200 before benchmarking.
QCODE=$(curl -s -o /dev/null -w '%{http_code}' -X QUERY -H 'Content-Type: application/json' --data-binary "$BODY" "http://$HOST:$PORT/bench/query")
if [ "$QCODE" != "200" ]; then
    echo "Expected 200 from QUERY /bench/query but got $QCODE; aborting." >&2
    exit 1
fi

run_bench() {
    local name="$1" method="$2" path="$3"
    printf "### %s\n" "$name"
    uv run --with httpx python "$SCRIPT_DIR/benchmark_query.py" \
        "http://$HOST:$PORT$path" -m "$method" -c "$C" -n "$N" --body "$BODY"
    echo ""
}

echo "## Body-bearing request throughput (identical work, different method)"
echo ""
run_bench "QUERY /bench/query" "QUERY" "/bench/query"
run_bench "POST /bench/parse (comparison)" "POST" "/bench/parse"

echo "## Summary"
echo ""
echo "Diff Reqs/sec between the two to isolate the QUERY method's routing cost."
echo "Key metrics: Reqs/sec (higher=better), p50/p90/p99 latency (lower=better)"
