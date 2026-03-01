# Django-Bolt Development Commands

# Default values for parameters
host := "127.0.0.1"
port := "8001"
c := "100"
n := "10000"
p := "8"
workers := "1"

# List available recipes
default:
    @just --list

# Build Rust extension in release mode
build:
    uv run maturin develop 

# Build Rust extension in release mode
build-release:
    uv run maturin develop --release


# Kill any servers on PORT
kill port=port:
    #!/usr/bin/env bash
    pids=$(lsof -tiTCP:{{port}} -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "killing: $pids"
        kill $pids 2>/dev/null || true
        sleep 0.3
        p2=$(lsof -tiTCP:{{port}} -sTCP:LISTEN 2>/dev/null || true)
        [ -n "$p2" ] && echo "force-killing: $p2" && kill -9 $p2 2>/dev/null || true
    fi
    [ -f /tmp/django-bolt-test.pid ] && kill $(cat /tmp/django-bolt-test.pid) 2>/dev/null || true
    rm -f /tmp/django-bolt-test.pid /tmp/django-bolt-test.log

# Clean build artifacts
clean:
    cargo clean
    rm -rf target/
    rm -f python/django_bolt/*.so

# Full rebuild
rebuild: (kill port) clean build

# Run development server with auto-reload
run-dev:
    uv run python python/example/manage.py runbolt --dev --port 8001

# Run Python tests (verbose)
test-py:
    uv run --with pytest pytest python/tests -s -vv

# Run ruff linter (checks all code)
lint:
    uv run ruff check .

# Run ruff linter and fix issues
lint-fix:
    uv run ruff check . --fix

# Alias for lint
ruff: lint

# Check only library code (excludes tests and examples)
lint-lib:
    uv run ruff check python/django_bolt

# Check only library code and fix issues
lint-lib-fix:
    uv run ruff check python/django_bolt --fix

# Fix ruff errors automatically
ruff-fix:
    uv run ruff check . --fix

# Format code with ruff
format:
    uv run ruff format .

# Find unused code (functions, classes, variables) with vulture
dead-code:
    uv run vulture python/django_bolt --min-confidence 80

# Find unused code including tests (more false positives)
dead-code-all:
    uv run vulture python/ --min-confidence 60

# Seed database with test data
seed-data host=host port=port:
    #!/usr/bin/env bash
    echo "Seeding database..."
    curl -s http://{{host}}:{{port}}/users/seed | head -1

# Save baseline vs dev benchmark comparison
save-bench host=host port=port c=c n=n p=p workers=workers:
    #!/usr/bin/env bash
    mkdir -p bench
    if [ ! -f bench/BENCHMARK_BASELINE.md ]; then
        echo "Creating baseline benchmark..."
        P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark.sh > bench/BENCHMARK_BASELINE.md
        echo "✅ Baseline saved to bench/BENCHMARK_BASELINE.md"
    elif [ ! -f bench/BENCHMARK_DEV.md ]; then
        echo "Creating dev benchmark..."
        P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark.sh > bench/BENCHMARK_DEV.md
        echo "✅ Dev version saved to bench/BENCHMARK_DEV.md"
        echo ""
        echo "=== PERFORMANCE COMPARISON ==="
        echo "Baseline:"
        grep "Requests per second" bench/BENCHMARK_BASELINE.md | head -2
        echo "Dev:"
        grep "Requests per second" bench/BENCHMARK_DEV.md | head -2
        echo ""
        echo "Streaming (Plain) RPS - Dev:"
        awk '/### Streaming Plain/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_DEV.md || true
        echo "Streaming (SSE) RPS - Dev:"
        awk '/### Server-Sent Events/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_DEV.md || true
    else
        echo "Rotating benchmarks: dev -> baseline, new -> dev"
        mv bench/BENCHMARK_DEV.md bench/BENCHMARK_BASELINE.md
        P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark.sh > bench/BENCHMARK_DEV.md
        echo "✅ New dev version saved, old dev moved to baseline"
        echo ""
        echo "=== PERFORMANCE COMPARISON ==="
        echo "Baseline (old dev):"
        grep "Requests per second" bench/BENCHMARK_BASELINE.md | head -2
        echo "Dev (current):"
        grep "Requests per second" bench/BENCHMARK_DEV.md | head -2
        echo ""
        echo "Streaming (Plain) RPS - Baseline:"
        awk '/### Streaming Plain/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_BASELINE.md || true
        echo "Streaming (SSE) RPS - Baseline:"
        awk '/### Server-Sent Events/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_BASELINE.md || true
        echo "Streaming (Plain) RPS - Dev:"
        awk '/### Streaming Plain/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_DEV.md || true
        echo "Streaming (SSE) RPS - Dev:"
        awk '/### Server-Sent Events/{flag=1;next} /###/{flag=0} flag && /Requests per second/{print}' bench/BENCHMARK_DEV.md || true
    fi

# Build and run benchmark
build-bench: build save-bench

# Focused parameter and form parsing benchmark (fast iteration)
bench-params host=host port=port c=c n=n p=p workers=workers:
    P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark_params.sh

# Release new version
# Usage: just release 0.2.2
# Usage: just release 0.3.0-alpha1 (for pre-releases)
# Usage: just release 0.2.2 --dry-run (for testing)
release version dry_run="":
    #!/usr/bin/env bash
    if [ -z "{{version}}" ]; then
        echo "Error: VERSION is required"
        echo "Usage: just release 0.2.2"
        echo "       just release 0.3.0-alpha1"
        echo "       just release 0.2.2 --dry-run"
        exit 1
    fi
    if [ "{{dry_run}}" = "--dry-run" ]; then
        ./scripts/release.sh {{version}} --dry-run
    else
        ./scripts/release.sh {{version}}
    fi

# Delete git tag locally and remotely
# Usage: just delete-tag v0.2.2
delete-tag tag:
    #!/usr/bin/env bash
    if [ -z "{{tag}}" ]; then
        echo "Error: TAG is required"
        echo "Usage: just delete-tag v0.2.2"
        exit 1
    fi
    echo "Deleting tag {{tag}} locally..."
    git tag -d {{tag}} || echo "Tag {{tag}} not found locally"
    echo "Deleting tag {{tag}} from remote..."
    git push origin :refs/tags/{{tag}} || echo "Tag {{tag}} not found on remote"
    echo "✅ Tag {{tag}} deleted successfully"

# Serve documentation locally
docs: docs-serve

# Serve documentation locally
docs-serve:
    cd docs && uv run zensical serve -a localhost:8080

# Build documentation
docs-build:
    cd docs && uv run zensical build --clean
