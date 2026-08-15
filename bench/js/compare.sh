#!/usr/bin/env bash
# Compare Django-Bolt against Hono and Elysia on identical 1 KB / 10 KB JSON.
#
# All runtimes serve the exact same files (python/example/test_data/1K.json and
# 10K.json) and serialize them per request, so the numbers are comparable.
#
# Usage:
#   ./bench/js/compare.sh
#   RUNTIMES="bolt elysia-bun" ./bench/js/compare.sh
#   C=200 N=200000 PROCESSES=4 ./bench/js/compare.sh
#
# Requires: bombardier, lsof, node (bun only for the hono-bun runtime).
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
C="${C:-100}"
N="${N:-100000}"
PROCESSES="${PROCESSES:-1}"
WORKERS="${WORKERS:-1}"
WARMUP_N="${WARMUP_N:-5000}"
RUNTIMES="${RUNTIMES:-bolt hono-node hono-bun elysia-bun}"
ENDPOINTS="${ENDPOINTS:-/1k-json /10k-json}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
results="$(mktemp)"
SERVER_PGIDS=()

command -v bombardier >/dev/null || {
    echo "bombardier not found: go install github.com/codesenberg/bombardier@latest" >&2
    exit 1
}

# Every runtime binds the same port, so each one must be fully gone — and the
# port released — before the next starts, or we would benchmark the wrong server.
stop_servers() {
    for pgid in "${SERVER_PGIDS[@]:-}"; do
        [ -n "$pgid" ] && kill -TERM -- "-$pgid" 2>/dev/null || true
    done
    SERVER_PGIDS=()
    local waited=0
    while lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
        if [ "$waited" -ge 40 ]; then
            echo "ERROR: a listener still holds port $PORT 10s after shutdown;" \
                "aborting instead of benchmarking a foreign server." >&2
            exit 1
        fi
        sleep 0.25
        waited=$((waited + 1))
    done
}

cleanup() {
    stop_servers 2>/dev/null || true
    rm -f "$results"
}
trap cleanup EXIT INT TERM

# Launch in its own process group so multi-process runtimes (runbolt workers,
# node cluster children, bun reusePort siblings) all die with one signal.
spawn() {
    setsid "$@" >>"/tmp/bench-hono-compare.log" 2>&1 &
    local pid=$!
    local pgid
    pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')"
    if [ -z "$pgid" ] || [ "$pgid" != "$pid" ]; then
        echo "ERROR: server did not start in an isolated process group." >&2
        kill "$pid" 2>/dev/null || true
        exit 1
    fi
    SERVER_PGIDS+=("$pgid")
}

wait_ready() {
    local name="$1"
    for _ in $(seq 1 120); do
        curl -fsS -o /dev/null "http://$HOST:$PORT/" 2>/dev/null && return 0
        sleep 0.5
    done
    echo "$name did not become ready on port $PORT (see /tmp/bench-hono-compare.log)" >&2
    exit 1
}

start_server() {
    case "$1" in
    bolt)
        cd "$repo/python/example"
        DJANGO_BOLT_WORKERS="$WORKERS" spawn uv run python manage.py runbolt \
            --host "$HOST" --port "$PORT" --processes "$PROCESSES"
        ;;
    hono-node)
        cd "$here"
        HOST="$HOST" PORT="$PORT" PROCESSES="$PROCESSES" spawn node server-node-hono.mjs
        ;;
    hono-bun | elysia-bun)
        command -v bun >/dev/null || {
            echo "  bun not found; skipping" >&2
            return 1
        }
        cd "$here"
        # Bun has no cluster module: run one process per PROCESSES, each binding
        # the shared port with reusePort.
        for _ in $(seq 1 "$PROCESSES"); do
            HOST="$HOST" PORT="$PORT" spawn bun "server-bun-${1%-bun}.mjs"
        done
        ;;
    *)
        echo "unknown runtime: $1" >&2
        return 1
        ;;
    esac
    wait_ready "$1"
}

run_endpoint() {
    local name="$1" path="$2"
    local url="http://$HOST:$PORT$path"
    local bytes
    bytes="$(curl -fsS "$url" | wc -c | tr -d ' ')"

    bombardier -c "$C" -n "$WARMUP_N" -l "$url" >/dev/null 2>&1 || true
    local out
    out="$(bombardier -c "$C" -n "$N" -l --print result "$url" 2>&1)"

    local rps p99 tput bad
    rps="$(awk '/Reqs\/sec/{print $2; exit}' <<<"$out")"
    p99="$(awk '/^ *99%/{print $2; exit}' <<<"$out")"
    tput="$(awk '/Throughput/{print $2; exit}' <<<"$out")"
    # bombardier prints no "errors" line on a clean run; anything that is not a
    # 2xx (including "others", i.e. socket errors) counts as a failed request.
    bad="$(awk -F'[-,]' '/1xx - /{s=0; for(i=2;i<=NF;i+=2){gsub(/ /,"",$i); s+=$i}; print s-$4; exit}' <<<"$out")"
    bad="$((${bad:-0} + $(awk '/others - /{gsub(/[^0-9]/,"",$0); print $0+0; exit}' <<<"$out")))"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$path" "$name" "${rps:-?}" "${p99:-?}" "${tput:-?}" "${bytes:-?}" "$bad" >>"$results"
    printf '  %-10s rps=%-12s p99=%-10s tput=%-11s bytes=%-7s non-2xx=%s\n' \
        "$path" "${rps:-?}" "${p99:-?}" "${tput:-?}" "${bytes:-?}" "$bad"
}

# A foreign listener on $PORT would answer the readiness probe and get
# benchmarked in place of the runtime under test. Refuse to start.
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $PORT is already in use by:" >&2
    lsof -iTCP:"$PORT" -sTCP:LISTEN >&2
    echo "Free it or pass PORT=<other> so a foreign server is not benchmarked." >&2
    exit 1
fi

echo "Django-Bolt vs Hono vs Elysia | c=$C n=$N processes=$PROCESSES port=$PORT"
echo

for runtime in $RUNTIMES; do
    echo "== $runtime"
    if ! start_server "$runtime"; then
        stop_servers
        continue
    fi
    for path in $ENDPOINTS; do
        run_endpoint "$runtime" "$path"
    done
    stop_servers
    echo
done

echo "=== Summary"
{
    printf 'endpoint\truntime\treqs/sec\tp99\tthroughput\tbytes\tnon-2xx\n'
    sort -k1,1 -s "$results"
} | column -t -s $'\t'
