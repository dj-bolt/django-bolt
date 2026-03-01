#!/bin/bash
# Clean benchmark runner for Django-Bolt

P=${P:-2}
WORKERS=${WORKERS:-2}
C=${C:-50}
N=${N:-10000}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}
# Slow-op benchmark knobs
SLOW_MS=${SLOW_MS:-100}
SLOW_CONC=${SLOW_CONC:-50}
SLOW_DURATION=${SLOW_DURATION:-5}

# Check if bombardier is available
BOMBARDIER_BIN=""
if command -v bombardier &> /dev/null; then
    BOMBARDIER_BIN="bombardier"
elif [ -f "$HOME/go/bin/bombardier" ]; then
    BOMBARDIER_BIN="$HOME/go/bin/bombardier"
elif [ -f "$HOME/.local/bin/bombardier" ]; then
    BOMBARDIER_BIN="$HOME/.local/bin/bombardier"
fi

if [ -z "$BOMBARDIER_BIN" ]; then
    echo "ERROR: bombardier not installed. Install with: go install github.com/codesenberg/bombardier@latest"
    exit 1
fi

# Check if setsid is available (needed for process group management)
SETSID_BIN=""
if command -v setsid &> /dev/null; then
    SETSID_BIN="setsid"
elif [ -f "/opt/homebrew/opt/util-linux/bin/setsid" ]; then
    SETSID_BIN="/opt/homebrew/opt/util-linux/bin/setsid"
fi

if [ -z "$SETSID_BIN" ]; then
    echo "ERROR: setsid not installed. Install with: brew install util-linux (macOS) or apt install util-linux (Linux)"
    exit 1
fi

# Wait for server to respond with 200 on /, retrying up to MAX_WAIT seconds.
wait_for_server() {
  local max_wait=${MAX_WAIT:-15}
  local elapsed=0
  while [ $elapsed -lt $max_wait ]; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' http://$HOST:$PORT/)
    if [ "$CODE" = "200" ]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "Server not ready after ${max_wait}s (last status: $CODE); aborting." >&2
  kill -TERM -$SERVER_PID 2>/dev/null || true
  pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true
  exit 1
}

echo "# Django-Bolt Benchmark"
echo "Generated: $(date)"
echo "Config: $P processes × $WORKERS workers | C=$C N=$N"
echo ""

echo "## Root Endpoint Performance"
cd python/example
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/ 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "## 10kb JSON Response Performance"

printf "### 10kb JSON (Async) (/10k-json)\n"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/10k-json 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### 10kb JSON (Sync) (/sync-10k-json)\n"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/sync-10k-json 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "## Response Type Endpoints"

printf "### Header Endpoint (/header)\n"
$BOMBARDIER_BIN -c $C -n $N -l -H 'x-test: val' http://$HOST:$PORT/header 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### Cookie Endpoint (/cookie)\n"
$BOMBARDIER_BIN -c $C -n $N -l -H 'Cookie: session=abc' http://$HOST:$PORT/cookie 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### Exception Endpoint (/exc)\n"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/exc 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### HTML Response (/html)\n"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/html 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### Redirect Response (/redirect)\n"
$BOMBARDIER_BIN -c $C -n $N -l --no-redirect http://$HOST:$PORT/redirect 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

printf "### File Static via FileResponse (/file-static)\n"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/file-static 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "## Authentication & Authorization Performance"

# Create a Django user and JWT token for testing
TOKEN=$(uv run python << 'PYTHON_TOKEN_SCRIPT'
import os
import sys
import jwt
import time

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'testproject.settings')

try:
    import django
    django.setup()

    from django.conf import settings
    from django.contrib.auth.models import User

    # Get or create a test user
    user, created = User.objects.get_or_create(
        username='benchuser',
        defaults={'email': 'bench@example.com'}
    )

    # Create JWT token with correct user ID
    payload = {
        'sub': str(user.id),
        'exp': int(time.time()) + 3600,
        'iat': int(time.time()),
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        'username': user.username,
        'email': user.email
    }

    token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')
    print(token)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON_TOKEN_SCRIPT
2>/dev/null)

# Only run auth tests if we have a valid token
if [ -n "$TOKEN" ] && [ ${#TOKEN} -gt 50 ]; then
    AUTH_HEADER="Authorization: Bearer $TOKEN"

    printf "### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query\n"
    $BOMBARDIER_BIN -c $C -n $N -l -H "$AUTH_HEADER" http://$HOST:$PORT/auth/no-user-access 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

    printf "### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query\n"
    $BOMBARDIER_BIN -c $C -n $N -l -H "$AUTH_HEADER" http://$HOST:$PORT/auth/me 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

    printf "### Get User via Dependency (/auth/me-dependency)\n"
    $BOMBARDIER_BIN -c $C -n $N -l -H "$AUTH_HEADER" http://$HOST:$PORT/auth/me-dependency 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

    printf "### Get Auth Context (/auth/context) validated jwt no db\n"
    $BOMBARDIER_BIN -c $C -n $N -l -H "$AUTH_HEADER" http://$HOST:$PORT/auth/context 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
else
    echo "Skipped auth benchmarks: Could not generate JWT token"
fi

# Additional endpoint: GET /items/{item_id}
echo ""
echo "## Items GET Performance (/items/1?q=hello)"
$BOMBARDIER_BIN -c $C -n $N -l "http://$HOST:$PORT/items/1?q=hello" 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

# Additional endpoint: PUT /items/{item_id} with JSON body
echo ""
echo "## Items PUT JSON Performance (/items/1)"
BODY_FILE=$(mktemp)
echo '{"name":"bench","price":1.23,"is_offer":true}' > "$BODY_FILE"

# Sanity check: ensure PUT returns 200 OK before benchmarking
PCODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' --data-binary @"$BODY_FILE" http://$HOST:$PORT/items/1)
if [ "$PCODE" != "200" ]; then
  echo "Expected 200 from PUT /items/1 but got $PCODE; skipping Items PUT benchmark." >&2
else
  $BOMBARDIER_BIN -c $C -n $N -l -m PUT -H 'Content-Type: application/json' -f "$BODY_FILE" http://$HOST:$PORT/items/1 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
fi
rm -f "$BODY_FILE"

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true
sleep 1

echo ""
echo "## ORM Performance"
uv run python manage.py makemigrations users --noinput >/dev/null 2>&1 || true
uv run python manage.py migrate --noinput >/dev/null 2>&1 || true

DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

# Sanity check
UCODE=$(curl -s -o /dev/null -w '%{http_code}' http://$HOST:$PORT/users/full10)
if [ "$UCODE" != "200" ]; then
  echo "Expected 200 from /users/full10 but got $UCODE; aborting ORM benchmark." >&2
  kill $SERVER_PID 2>/dev/null || true
  exit 1
fi

# Seed users for benchmarking (create 1000 test users)
echo "Seeding 1000 users for benchmark..."
SEED_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X GET http://$HOST:$PORT/users/seed?count=1000)
if [ "$SEED_CODE" != "200" ]; then
  echo "Warning: Failed to seed users (got $SEED_CODE), benchmarking with empty database" >&2
else
  echo "Successfully seeded users"

  # Validate users exist by checking /users/full10
  USERS_RESPONSE=$(curl -s http://$HOST:$PORT/users/full10)
  USER_COUNT=$(echo "$USERS_RESPONSE" | grep -o '"id"' | wc -l)
  if [ "$USER_COUNT" -eq 0 ]; then
    echo "Warning: No users found after seeding, benchmarking with empty database" >&2
  else
    echo "Validated: $USER_COUNT users exist in database"
  fi
fi

echo "### Users Full10 (Async) (/users/full10)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/users/full10 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo "### Users Full10 (Sync) (/users/sync-full10)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/users/sync-full10 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo "### Users Mini10 (Async) (/users/mini10)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/users/mini10 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo "### Users Mini10 (Sync) (/users/sync-mini10)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/users/sync-mini10 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

# Clean up: delete all users
echo "Cleaning up test users..."
curl -s -X POST http://$HOST:$PORT/users/delete >/dev/null 2>&1

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true

echo ""
echo "## Class-Based Views (CBV) Performance"

DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

echo "### Simple APIView GET (/cbv-simple)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/cbv-simple 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo "### Simple APIView POST (/cbv-simple)"
BODY_FILE=$(mktemp)
echo '{"name":"bench","price":1.23,"is_offer":true}' > "$BODY_FILE"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$BODY_FILE" http://$HOST:$PORT/cbv-simple 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$BODY_FILE"

echo "### Items100 ViewSet GET (/cbv-items100)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/cbv-items100 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "## CBV Items - Basic Operations"

echo "### CBV Items GET (Retrieve) (/cbv-items/1)"
GCODE=$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/cbv-items/1?q=test")
if [ "$GCODE" = "200" ]; then
  $BOMBARDIER_BIN -c $C -n $N -l "http://$HOST:$PORT/cbv-items/1?q=test" 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
else
  echo "Skipped: CBV Items GET returned $GCODE" >&2
fi

echo "### CBV Items PUT (Update) (/cbv-items/1)"
BODY_FILE=$(mktemp)
echo '{"name":"updated-item","price":79.99,"is_offer":true}' > "$BODY_FILE"
PCODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' --data-binary @"$BODY_FILE" http://$HOST:$PORT/cbv-items/1)
if [ "$PCODE" = "200" ]; then
  $BOMBARDIER_BIN -c $C -n $N -l -m PUT -H 'Content-Type: application/json' -f "$BODY_FILE" http://$HOST:$PORT/cbv-items/1 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
else
  echo "Skipped: CBV Items PUT returned $PCODE" >&2
fi
rm -f "$BODY_FILE"

echo ""
echo "## CBV Additional Benchmarks"

echo "### CBV Bench Parse (POST /cbv-bench-parse)"
BODY_FILE=$(mktemp)
cat > "$BODY_FILE" << 'JSON'
{
  "title": "bench",
  "count": 100,
  "items": [
    {"name": "a", "price": 1.0, "is_offer": true}
  ]
}
JSON
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$BODY_FILE" http://$HOST:$PORT/cbv-bench-parse 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$BODY_FILE"

echo "### CBV Response Types (/cbv-response)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/cbv-response 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

# ORM endpoints with CBV
echo ""
echo "## ORM Performance with CBV"

# Seed users for CBV benchmarking
echo "Seeding 1000 users for CBV benchmark..."
SEED_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X GET http://$HOST:$PORT/users/seed?count=1000)
if [ "$SEED_CODE" != "200" ]; then
  echo "Warning: Failed to seed users (got $SEED_CODE), benchmarking with empty database" >&2
else
  echo "Successfully seeded users"

  # Validate users exist by checking /users/cbv-mini10
  USERS_RESPONSE=$(curl -s http://$HOST:$PORT/users/cbv-mini10)
  USER_COUNT=$(echo "$USERS_RESPONSE" | grep -o '"id"' | wc -l)
  if [ "$USER_COUNT" -eq 0 ]; then
    echo "Warning: No users found after seeding, benchmarking with empty database" >&2
  else
    echo "Validated: $USER_COUNT users exist in database"
  fi
fi

# Sanity check
UCODE=$(curl -s -o /dev/null -w '%{http_code}' http://$HOST:$PORT/users/cbv-mini10)
if [ "$UCODE" != "200" ]; then
  echo "Expected 200 from /users/cbv-mini10 but got $UCODE; skipping CBV ORM benchmark." >&2
else
  echo "### Users CBV Mini10 (List) (/users/cbv-mini10)"
  $BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/users/cbv-mini10 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
fi

# Clean up: delete all users
echo "Cleaning up test users..."
curl -s -X POST http://$HOST:$PORT/users/delete >/dev/null 2>&1

echo ""

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true

echo ""
echo "## Form and File Upload Performance"

# Start server for form/file tests
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

echo "### Form Data (POST /form)"
# Create form data
FORM_FILE=$(mktemp)
echo "name=TestUser&age=25&email=test%40example.com" > "$FORM_FILE"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/x-www-form-urlencoded' -f "$FORM_FILE" http://$HOST:$PORT/form 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$FORM_FILE"

echo "### File Upload (POST /upload)"
# Create a multipart form data file with proper CRLF line endings
UPLOAD_FILE=$(mktemp)
BOUNDARY="----BoltBenchmark$(date +%s)"
# Use printf with \r\n for proper CRLF line endings (required by HTTP multipart/form-data spec)
printf -- "--%s\r\n" "$BOUNDARY" > "$UPLOAD_FILE"
printf "Content-Disposition: form-data; name=\"file\"; filename=\"test1.txt\"\r\n" >> "$UPLOAD_FILE"
printf "Content-Type: text/plain\r\n" >> "$UPLOAD_FILE"
printf "\r\n" >> "$UPLOAD_FILE"
printf "This is test file content 1\r\n" >> "$UPLOAD_FILE"
printf -- "--%s\r\n" "$BOUNDARY" >> "$UPLOAD_FILE"
printf "Content-Disposition: form-data; name=\"file\"; filename=\"test2.txt\"\r\n" >> "$UPLOAD_FILE"
printf "Content-Type: text/plain\r\n" >> "$UPLOAD_FILE"
printf "\r\n" >> "$UPLOAD_FILE"
printf "This is test file content 2\r\n" >> "$UPLOAD_FILE"
printf -- "--%s--\r\n" "$BOUNDARY" >> "$UPLOAD_FILE"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H "Content-Type: multipart/form-data; boundary=$BOUNDARY" -f "$UPLOAD_FILE" http://$HOST:$PORT/upload 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$UPLOAD_FILE"

# Mixed form with files benchmark
echo "### Mixed Form with Files (POST /mixed-form)"
MIXED_FILE=$(mktemp)
BOUNDARY="----BoltMixed$(date +%s)"
# Use printf with \r\n for proper CRLF line endings (required by HTTP multipart/form-data spec)
printf -- "--%s\r\n" "$BOUNDARY" > "$MIXED_FILE"
printf "Content-Disposition: form-data; name=\"title\"\r\n" >> "$MIXED_FILE"
printf "\r\n" >> "$MIXED_FILE"
printf "Test Title\r\n" >> "$MIXED_FILE"
printf -- "--%s\r\n" "$BOUNDARY" >> "$MIXED_FILE"
printf "Content-Disposition: form-data; name=\"description\"\r\n" >> "$MIXED_FILE"
printf "\r\n" >> "$MIXED_FILE"
printf "This is a test description\r\n" >> "$MIXED_FILE"
printf -- "--%s\r\n" "$BOUNDARY" >> "$MIXED_FILE"
printf "Content-Disposition: form-data; name=\"file\"; filename=\"attachment.txt\"\r\n" >> "$MIXED_FILE"
printf "Content-Type: text/plain\r\n" >> "$MIXED_FILE"
printf "\r\n" >> "$MIXED_FILE"
printf "File attachment content\r\n" >> "$MIXED_FILE"
printf -- "--%s--\r\n" "$BOUNDARY" >> "$MIXED_FILE"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H "Content-Type: multipart/form-data; boundary=$BOUNDARY" -f "$MIXED_FILE" http://$HOST:$PORT/mixed-form 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$MIXED_FILE"

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true

echo ""
echo "## Django Middleware Performance"

# Start server for middleware test
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

# Sanity check middleware endpoint
MCODE=$(curl -s -o /dev/null -w '%{http_code}' http://$HOST:$PORT/middleware/demo)
if [ "$MCODE" != "200" ]; then
  echo "Expected 200 from /middleware/demo but got $MCODE; skipping middleware benchmark." >&2
else
  echo "### Django Middleware + Messages Framework (/middleware/demo)"
  echo "Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering"
  $BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/middleware/demo 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
fi

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true

echo ""
echo "## Django Ninja-style Benchmarks"

# JSON Parsing/Validation

BODY_FILE=$(mktemp)
cat > "$BODY_FILE" << 'JSON'
{
  "title": "bench",
  "count": 100,
  "items": [
    {"name": "a", "price": 1.0, "is_offer": true}
  ]
}
JSON

echo "### JSON Parse/Validate (POST /bench/parse)"
# Start a fresh server for this test
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

# Sanity check
PCODE=$(curl -s -o /dev/null -w '%{http_code}' http://$HOST:$PORT/)
if [ "$PCODE" != "200" ]; then
  echo "Expected 200 from / before parse test but got $PCODE; skipping." >&2
else
  $BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$BODY_FILE" http://$HOST:$PORT/bench/parse 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
fi
rm -f "$BODY_FILE"

echo ""
echo "## Serializer Performance Benchmarks"

# Test raw msgspec (baseline)
SERIALIZER_RAW=$(mktemp)
cat > "$SERIALIZER_RAW" << 'JSON'
{
  "id": 1,
  "name": "John Doe",
  "email": "john@example.com",
  "bio": "Software developer"
}
JSON

echo "### Raw msgspec Serializer (POST /bench/serializer-raw)"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$SERIALIZER_RAW" http://$HOST:$PORT/bench/serializer-raw 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$SERIALIZER_RAW"

# Test with custom validators
SERIALIZER_VALIDATED=$(mktemp)
cat > "$SERIALIZER_VALIDATED" << 'JSON'
{
  "id": 1,
  "name": "  John Doe  ",
  "email": "JOHN@EXAMPLE.COM",
  "bio": "Software developer"
}
JSON

echo "### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)"
$BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$SERIALIZER_VALIDATED" http://$HOST:$PORT/bench/serializer-validated 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
rm -f "$SERIALIZER_VALIDATED"

# Test users endpoint with raw msgspec
USER_BENCH=$(mktemp)
cat > "$USER_BENCH" << 'JSON'
{
  "id": 1,
  "username": "testuser",
  "email": "test@example.com",
  "bio": "Test bio"
}
JSON

echo "### Users msgspec Serializer (POST /users/bench/msgspec)"
USCODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data-binary @"$USER_BENCH" http://$HOST:$PORT/users/bench/msgspec)
if [ "$USCODE" = "200" ]; then
  $BOMBARDIER_BIN -c $C -n $N -l -m POST -H 'Content-Type: application/json' -f "$USER_BENCH" http://$HOST:$PORT/users/bench/msgspec 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
else
  echo "Skipped: Users msgspec endpoint returned $USCODE" >&2
fi
rm -f "$USER_BENCH"

echo ""
echo "## Multi-Response Performance"

echo ""
echo "### Multi-response tuple return (/bench/multi/tuple)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/bench/multi/tuple 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Multi-response bare dict (/bench/multi/dict)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/bench/multi/dict 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true

echo ""
echo "## Latency Percentile Benchmarks"
echo "Measures p50/p75/p90/p99 latency for type coercion overhead analysis"

# Start server for latency tests
DJANGO_BOLT_WORKERS=$WORKERS $SETSID_BIN uv run python manage.py runbolt --host $HOST --port $PORT --processes $P >/dev/null 2>&1 &
SERVER_PID=$!
wait_for_server

echo ""
echo "### Baseline - No Parameters (/)"
$BOMBARDIER_BIN -c $C -n $N -l http://$HOST:$PORT/ 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Path Parameter - int (/items/12345)"
$BOMBARDIER_BIN -c $C -n $N -l "http://$HOST:$PORT/items/12345" 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Path + Query Parameters (/items/12345?q=hello)"
$BOMBARDIER_BIN -c $C -n $N -l "http://$HOST:$PORT/items/12345?q=hello" 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Header Parameter (/header)"
$BOMBARDIER_BIN -c $C -n $N -l -H "x-test: testvalue" http://$HOST:$PORT/header 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Cookie Parameter (/cookie)"
$BOMBARDIER_BIN -c $C -n $N -l -H "Cookie: session=abc123" http://$HOST:$PORT/cookie 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"

echo ""
echo "### Auth Context - JWT validated, no DB (/auth/context)"
if [ -n "$TOKEN" ] && [ ${#TOKEN} -gt 50 ]; then
    $BOMBARDIER_BIN -c $C -n $N -l -H "Authorization: Bearer $TOKEN" http://$HOST:$PORT/auth/context 2>&1 | tr '\r' '\n' | grep -E "(Reqs/sec|Latency|50%|75%|90%|99%)"
else
    echo "Skipped: No valid JWT token"
fi

kill -TERM -$SERVER_PID 2>/dev/null || true
pkill -TERM -f "manage.py runbolt --host $HOST --port $PORT" 2>/dev/null || true
