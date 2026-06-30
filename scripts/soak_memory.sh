#!/usr/bin/env bash
# Memory soak: drive several endpoint *types* under sustained load and fail
# (exit 1) if any one of them grows the server's RSS past a threshold.
#
# Covering multiple path categories catches leaks that are specific to a code
# path (body reading, param coercion, header/cookie extraction, error building,
# streaming generators) — a single-endpoint soak would miss them. RSS growth is
# measured WITHIN each endpoint's window, so legitimate steady-state differences
# between endpoints don't cause false positives.
#
# This is intentionally NOT part of `just test-py` (needs bombardier + minutes).
# Queue/allocation *behavior* is unit-tested in python/tests/test_logging.py;
# this guards whole-server RSS *stability*.
#
# Env knobs (defaults): HOST=127.0.0.1 PORT=8011 P=1 WORKERS=1 CONC=100
#   DUR=20            # seconds of load per endpoint
#   THRESHOLD_MIB=50  # fail if an endpoint grows RSS more than this
set -u

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8011}
P=${P:-1}
WORKERS=${WORKERS:-1}
CONC=${CONC:-100}
DUR=${DUR:-20}
THRESHOLD_MIB=${THRESHOLD_MIB:-50}
JSON_BODY='{"name":"soak","price":1.5,"is_offer":true}'

# Endpoint phases: "label|kind|method|path". `kind` selects extra request flags.
PHASES=(
    "no-body GET        |plain |GET|/"
    "path+query params  |plain |GET|/items/1?q=hello"
    "JSON body (PUT)    |json  |PUT|/items/1"
    "header extraction  |header|GET|/header"
    "cookie parsing     |cookie|GET|/cookie"
    "error/exception    |plain |GET|/exc"
    "HTML response      |plain |GET|/html"
    "streaming response |plain |GET|/stream"
)

BOMBARDIER_BIN=""
if command -v bombardier &>/dev/null; then BOMBARDIER_BIN="bombardier"
elif [ -f "$HOME/go/bin/bombardier" ]; then BOMBARDIER_BIN="$HOME/go/bin/bombardier"
elif [ -f "$HOME/.local/bin/bombardier" ]; then BOMBARDIER_BIN="$HOME/.local/bin/bombardier"
fi
if [ -z "$BOMBARDIER_BIN" ]; then
    echo "ERROR: bombardier not installed. Install with: go install github.com/codesenberg/bombardier@latest" >&2
    exit 2
fi

PROC_MATCH="manage.py runbolt --host $HOST --port $PORT"
cleanup() { pkill -f "$PROC_MATCH" 2>/dev/null || true; }
trap cleanup EXIT

cd "$(dirname "$0")/../python/example" || { echo "ERROR: python/example not found" >&2; exit 2; }
uv run python manage.py collectstatic --noinput >/dev/null 2>&1 || true

DJANGO_BOLT_WORKERS=$WORKERS uv run python manage.py runbolt \
    --host "$HOST" --port "$PORT" --processes "$P" >/tmp/bolt-soak-server.log 2>&1 &

code=""
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/" || true)
    [ "$code" = "200" ] && break
    sleep 1
done
[ "$code" = "200" ] || { echo "ERROR: server not ready (last status: $code)" >&2; exit 2; }

# Sum RSS (KiB) across all runbolt processes.
sample_rss() {
    local rss=0 r
    for p in $(pgrep -f "$PROC_MATCH"); do
        r=$(ps -o rss= -p "$p" 2>/dev/null | tr -d ' ')
        [ -n "$r" ] && rss=$((rss + r))
    done
    echo "$rss"
}

# Run one endpoint phase; echoes "PASS"/"FAIL" line, returns 1 on failure.
run_phase() {
    local label="$1" kind="$2" method="$3" path="$4"
    local url="http://$HOST:$PORT$path"
    local args=(-c "$CONC" -d "${DUR}s" -m "$method")
    case "$kind" in
        json)   args+=(-H 'Content-Type: application/json' -b "$JSON_BODY") ;;
        header) args+=(-H 'x-test: soak') ;;
        cookie) args+=(-H 'Cookie: session=abc') ;;
    esac

    # Warm this endpoint first so one-time steady-state allocation (e.g. streaming
    # task/buffer setup on first use) is NOT counted as growth — we want to detect
    # per-request leaks, i.e. growth from steady-state to steady-state.
    "$BOMBARDIER_BIN" -c "$CONC" -d 4s -m "$method" "${args[@]:6}" "$url" >/dev/null 2>&1
    sleep 1
    local start_rss end_rss
    start_rss=$(sample_rss)
    "$BOMBARDIER_BIN" "${args[@]}" "$url" >/dev/null 2>&1
    end_rss=$(sample_rss)

    THRESHOLD_MIB="$THRESHOLD_MIB" python3 - "$label" "$start_rss" "$end_rss" <<'PY'
import os, sys
label, start_kb, end_kb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
start, end = start_kb/1024, end_kb/1024
growth = end - start
thr = float(os.environ["THRESHOLD_MIB"])
status = "FAIL" if growth > thr else "PASS"
print(f"  [{status}] {label.strip():22} start={start:6.1f}  end={end:6.1f}  growth={growth:+6.1f} MiB")
sys.exit(1 if status == "FAIL" else 0)
PY
}

echo "Memory soak: ${DUR}s/endpoint, C=$CONC, P=$P, threshold=${THRESHOLD_MIB} MiB"
# Warm up so allocator/caches settle before the first measured phase.
"$BOMBARDIER_BIN" -c 50 -d 8s "http://$HOST:$PORT/" >/dev/null 2>&1

overall=0
for phase in "${PHASES[@]}"; do
    IFS='|' read -r label kind method path <<< "$phase"
    run_phase "$label" "$(echo "$kind" | tr -d ' ')" "$(echo "$method" | tr -d ' ')" "$(echo "$path" | tr -d ' ')" || overall=1
done

echo ""
if [ "$overall" -eq 0 ]; then
    echo "SOAK PASS: all endpoints stable (no RSS growth > ${THRESHOLD_MIB} MiB)"
else
    echo "SOAK FAIL: one or more endpoints grew RSS past ${THRESHOLD_MIB} MiB — possible leak"
fi
exit "$overall"
