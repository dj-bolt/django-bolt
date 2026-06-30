#!/usr/bin/env bash
# Memory soak: drive a body-reading endpoint under sustained load, sample the
# server's RSS over time, and fail (exit 1) if memory grows beyond a threshold.
#
# This catches per-request leaks that unit tests miss. It is intentionally NOT
# part of `just test-py` (it needs bombardier + a few minutes); run it manually
# or on a schedule. Queue/allocation *behavior* is unit-tested in
# python/tests/test_logging.py — this guards whole-server RSS *stability*.
#
# Env knobs (with defaults):
#   HOST=127.0.0.1 PORT=8011 P=1 WORKERS=1 CONC=100 DUR=120
#   METHOD=PUT ENDPOINT=/items/1 BODY='{"name":"soak","price":1.5,"is_offer":true}'
#   THRESHOLD_MIB=50   # fail if RSS grows more than this from the post-warmup baseline
set -u

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8011}
P=${P:-1}
WORKERS=${WORKERS:-1}
CONC=${CONC:-100}
DUR=${DUR:-120}
METHOD=${METHOD:-PUT}
ENDPOINT=${ENDPOINT:-/items/1}
BODY=${BODY:-'{"name":"soak","price":1.5,"is_offer":true}'}
THRESHOLD_MIB=${THRESHOLD_MIB:-50}

# --- tool discovery (mirrors scripts/benchmark.sh) ---
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

# Wait for readiness.
code=""
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/" || true)
    [ "$code" = "200" ] && break
    sleep 1
done
[ "$code" = "200" ] || { echo "ERROR: server not ready (last status: $code)" >&2; exit 2; }

# Warm up so allocator/caches settle before the baseline sample.
"$BOMBARDIER_BIN" -c 50 -d 10s -m "$METHOD" -H 'Content-Type: application/json' \
    -b "$BODY" "http://$HOST:$PORT$ENDPOINT" >/dev/null 2>&1

RSS_CSV="${TMPDIR:-/tmp}/bolt-soak-rss-$$.csv"
echo "elapsed_s,rss_kb" > "$RSS_CSV"
(
    t=0
    while [ "$t" -le "$DUR" ]; do
        rss=0
        for p in $(pgrep -f "$PROC_MATCH"); do
            r=$(ps -o rss= -p "$p" 2>/dev/null | tr -d ' ')
            [ -n "$r" ] && rss=$((rss + r))
        done
        echo "$t,$rss" >> "$RSS_CSV"
        sleep 2; t=$((t + 2))
    done
) &
SAMPLER=$!

echo "Soaking $METHOD $ENDPOINT for ${DUR}s (C=$CONC, P=$P, WORKERS=$WORKERS)..."
"$BOMBARDIER_BIN" -c "$CONC" -d "${DUR}s" -m "$METHOD" -H 'Content-Type: application/json' \
    -b "$BODY" -l "http://$HOST:$PORT$ENDPOINT" 2>&1 | tr '\r' '\n' | grep -E "Reqs/sec|2xx|5xx|errors" || true
wait "$SAMPLER" 2>/dev/null || true

# Analyze: fail if RSS grew more than THRESHOLD_MIB from the post-warmup baseline.
THRESHOLD_MIB="$THRESHOLD_MIB" python3 - "$RSS_CSV" <<'PY'
import csv, os, statistics, sys
rows = list(csv.DictReader(open(sys.argv[1])))
rss = [int(r["rss_kb"]) / 1024 for r in rows]
t = [int(r["elapsed_s"]) for r in rows]
n = len(rss)
if n < 3:
    print("SOAK INCONCLUSIVE: too few samples"); sys.exit(2)
base = rss[1]                       # skip the very first sample (warmup tail)
end, mx = rss[-1], max(rss)
mt, mr = statistics.mean(t), statistics.mean(rss)
slope = sum((t[i]-mt)*(rss[i]-mr) for i in range(n)) / (sum((t[i]-mt)**2 for i in range(n)) or 1)
growth = end - base
thr = float(os.environ["THRESHOLD_MIB"])
print(f"RSS: start={base:.1f} end={end:.1f} max={mx:.1f} MiB | growth={growth:+.1f} MiB | "
      f"slope={slope*60:+.3f} MiB/min over {t[-1]}s")
if growth > thr:
    print(f"SOAK FAIL: RSS grew {growth:.1f} MiB (> {thr:.0f} MiB threshold) — possible leak"); sys.exit(1)
print(f"SOAK PASS: RSS stable (growth {growth:+.1f} MiB <= {thr:.0f} MiB threshold)")
PY
