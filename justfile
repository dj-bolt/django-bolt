# Django-Bolt Development Commands

# Default values for parameters
host := "127.0.0.1"
port := "8001"
c := "100"
# 100k requests ≈ 0.3-1s per endpoint at typical RPS. 10k finished in ~40ms on
# fast endpoints — mostly connection-ramp transient, giving ±25% run-to-run
# noise that made bench comparisons meaningless.
n := "100000"
p := "8"
workers := "1"
# Host port of the throwaway PostgreSQL server that `just test-pg` starts
pg_port := "55432"

# List available recipes
default:
    @just --list

# Build Rust extension in release mode
build:
    uv run maturin develop

# Build Rust extension in release mode
build-release:
    uv run maturin develop --release

# Free-threaded CPython (python3.14t, no GIL) lives in its own environment so
# the default .venv stays untouched. `uv sync` installs the interpreter and
# the dev dependencies; maturin builds the extension into that environment.
ft_env := ".venv-ft"

# Create the free-threaded environment and build the extension into it
build-ft:
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv sync --python 3.14t
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv run maturin develop --release
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv run python -c "import sys; print(sys.version); print('GIL enabled:', sys._is_gil_enabled())"

# Stop when `.venv-ft` does not hold a free-threaded interpreter. `uv run` can
# create it with a standard interpreter, because the project accepts Python >=3.12.
_require-ft:
    @{{ft_env}}/bin/python -c "import sys; sys.exit(sys._is_gil_enabled())" 2>/dev/null || (echo "No free-threaded build in {{ft_env}}. Run 'just build-ft' first." && exit 1)

# Run Python tests on the free-threaded build
test-ft: _require-ft
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv run --with pytest --with pytest-xdist pytest python/tests -s -vv -n auto

# Run the example project on the free-threaded build (one process, all worker threads)
run-dev-ft: _require-ft
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv run python python/example/manage.py runbolt --dev --port 8001

# Run any command inside the free-threaded environment: just ft python -c "..."
ft +args: _require-ft
    UV_PROJECT_ENVIRONMENT={{ft_env}} uv run {{args}}

# Benchmark the free-threaded build: one process, one worker thread per CPU
# (the GIL benchmark uses `p` processes with one thread each). Results go to
# python/benchmark/BENCHMARK-FT.md so the GIL numbers stay untouched.
save-bench-ft host=host port=port c=c n=n p="1" workers=`nproc`: _require-ft
    #!/usr/bin/env bash
    mkdir -p python/benchmark
    UV_PROJECT_ENVIRONMENT={{ft_env}} P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark.sh > python/benchmark/BENCHMARK-FT.md
    echo "✅ Results saved to python/benchmark/BENCHMARK-FT.md"
    echo ""
    echo "=== ROOT RPS ==="
    grep "Reqs/sec" python/benchmark/BENCHMARK-FT.md | head -2


# Kill any servers on PORT
kill port=port:
    #!/usr/bin/env bash
    # Supervisors first: runbolt respawns killed workers, so sweeping only the
    # port listeners lets the supervisor bring them straight back. Workers are
    # forked from the supervisor and share its argv, so one pattern catches both.
    sups=$(pgrep -f "runbolt .*--port {{port}}( |$)" 2>/dev/null || true)
    if [ -n "$sups" ]; then
        echo "terminating runbolt processes: $sups"
        kill $sups 2>/dev/null || true
        for _ in $(seq 1 20); do
            pgrep -f "runbolt .*--port {{port}}( |$)" >/dev/null 2>&1 || break
            sleep 0.25
        done
        left=$(pgrep -f "runbolt .*--port {{port}}( |$)" 2>/dev/null || true)
        if [ -n "$left" ]; then
            echo "force-killing runbolt processes: $left"
            kill -9 $left 2>/dev/null || true
        fi
    fi
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
    uv run --with pytest --with pytest-xdist pytest python/tests -s -vv -n auto

# Run the tests that need PostgreSQL (`@pytest.mark.postgres`) against a
# throwaway Docker server. These skip in `test-py` unless
# DJANGO_BOLT_TEST_POSTGRES_DSN is set.
test-pg pg_port=pg_port:
    #!/usr/bin/env bash
    set -euo pipefail
    name=django-bolt-test-pg
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --name "$name" -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:{{pg_port}}:5432 postgres:18-alpine >/dev/null
    trap 'docker rm -f "$name" >/dev/null' EXIT
    for _ in $(seq 1 60); do
        docker exec "$name" pg_isready -U postgres >/dev/null 2>&1 && break
        sleep 0.5
    done
    DJANGO_BOLT_TEST_POSTGRES_DSN="postgresql://postgres:postgres@127.0.0.1:{{pg_port}}/postgres" \
        uv run --with pytest pytest python/tests -m postgres -s -vv

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

# Run the full benchmark suite; overwrites python/benchmark/BENCHMARK.md (git diff shows the change vs the committed run)
save-bench host=host port=port c=c n=n p=p workers=workers:
    #!/usr/bin/env bash
    mkdir -p python/benchmark
    P={{p}} WORKERS={{workers}} C={{c}} N={{n}} HOST={{host}} PORT={{port}} ./scripts/benchmark.sh > python/benchmark/BENCHMARK.md
    echo "✅ Results saved to python/benchmark/BENCHMARK.md"
    echo ""
    echo "=== ROOT RPS ==="
    grep "Reqs/sec" python/benchmark/BENCHMARK.md | head -2

# Build and run benchmark
build-bench: build save-bench

# Build with profiling profile (release + debug symbols) for flamegraphs
build-profiling:
    uv run maturin develop --profile profiling

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

# Release the bolt-mcp add-on (pure-python, separate PyPI project).
# Version is derived from the git tag (hatch-vcs) — nothing to bump in pyproject.
# No version arg → auto-bump the patch from the last bolt-mcp tag.
# Usage: just release-mcp              (auto patch bump, e.g. 0.1.0 -> 0.1.1)
# Usage: just release-mcp 0.2.0        (explicit, e.g. for a minor/major)
# Usage: just release-mcp "" --dry-run (show the computed version, don't tag/push)
release-mcp version="" dry_run="":
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">> Running bolt-mcp tests"
    uv run pytest python/bolt-mcp/tests -q
    if [ -n "{{version}}" ]; then
        NEW="{{version}}"
    else
        LAST=$(git tag --list 'bolt-mcp-v*' --sort=-v:refname | head -n1)
        if [ -z "$LAST" ]; then
            NEW="0.1.0"
        else
            BASE=${LAST#bolt-mcp-v}
            IFS=. read -r MAJOR MINOR PATCH <<< "$BASE"
            NEW="${MAJOR}.${MINOR}.$((PATCH + 1))"
        fi
        echo ">> Last tag: ${LAST:-none} → auto-bumped to $NEW (pass a version to override)"
    fi
    TAG="bolt-mcp-v$NEW"
    if git rev-parse "$TAG" >/dev/null 2>&1; then echo "Error: tag $TAG already exists"; exit 1; fi
    if [ "{{dry_run}}" = "--dry-run" ]; then
        echo "[dry-run] would tag $TAG and push (CI then builds version $NEW from the tag)"
        exit 0
    fi
    git tag -a "$TAG" -m "bolt-mcp v$NEW"
    git push --follow-tags
    echo ">> Pushed $TAG — the 'bolt-mcp' workflow will build (version $NEW, from the tag) & publish to PyPI."

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
    cd docs && uv run python build_llms_full.py && uv run --group docs zensical serve -a localhost:8080

# Build documentation (also regenerates llms-full.txt for AI crawlers)
docs-build:
    cd docs && uv run python build_llms_full.py && uv run --group docs zensical build --clean
