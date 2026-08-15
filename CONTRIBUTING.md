# Contributing to Django-Bolt

Thank you for your interest in contributing! Django-Bolt is a Rust + Python project, so the setup has a couple more steps than a pure-Python package. This guide walks through everything you need.

## Table of contents

- [Prerequisites](#prerequisites)
- [Development setup](#development-setup)
- [Common commands](#common-commands)
- [Project layout](#project-layout)
- [Making changes](#making-changes)
- [Testing](#testing)
- [Code style](#code-style)
- [Benchmarking](#benchmarking)
- [Documentation](#documentation)
- [Submitting a pull request](#submitting-a-pull-request)
- [Areas where help is welcome](#areas-where-help-is-welcome)
- [Getting help](#getting-help)

## Prerequisites

- **Python 3.12+** (3.12, 3.13, and 3.14 are supported)
- **Rust toolchain** — install via [rustup](https://rustup.rs/)
- **[uv](https://docs.astral.sh/uv/)** — Python package and environment manager
- **[just](https://github.com/casey/just)** — command runner used for all project recipes
- **[bombardier](https://github.com/codesenberg/bombardier)** *(optional)* — only needed for benchmarks: `go install github.com/codesenberg/bombardier@latest`

## Development setup

```bash
# 1. Fork and clone
git clone https://github.com/<your-username>/django-bolt.git
cd django-bolt

# 2. Install Python dependencies
uv sync

# 3. Build the Rust extension (required after any Rust change)
just build            # or: maturin develop --release

# 4. Verify everything works
just test-py
just lint-lib
```

## Common commands

| Command | What it does |
| --- | --- |
| `just build` | Build the Rust extension in-place |
| `just rebuild` | Clean and rebuild from scratch |
| `just clean` | Remove build artifacts |
| `just test-py` | Run the Python test suite |
| `just lint` | Run ruff on library, tests, and examples |
| `just lint-lib` | Lint library code only (must always pass) |
| `just ruff-fix` | Auto-fix lint errors |
| `just format` | Format code with ruff |
| `just save-bench` | Run the full benchmark suite and save results |
| `just smoke` | Quick endpoint smoke test against a running server |
| `just kill` | Kill any running dev servers |

Run the example project from `python/example/`:

```bash
cd python/example
python manage.py migrate
python manage.py runbolt --dev
```

## Project layout

```
src/                     Rust extension entry crate (PyO3 module, Actix server, dispatch)
crates/                  Rust workspace crates (bolt-loop, bolt-core, bolt-asgi, bolt-websocket, bolt-mcp)
python/django_bolt/      Python framework (BoltAPI, serializers, responses, auth, viewsets, ...)
python/bolt-mcp/         Optional MCP add-on package
python/tests/            Python test suite
python/tests/integration/apps/   Real app modules used by server-integration tests
python/example/          Example Django project (also used for benchmarks)
bench/                   Benchmark results and JS-runtime comparison
docs/                    Documentation site source
```

See [`CLAUDE.md`](CLAUDE.md) for a deeper architecture overview, request flow, and hot-path performance rules.

## Making changes

1. **Create a branch** from `master`: `git checkout -b feature/my-change`
2. **Rust changes** live in `src/` and `crates/`. Run `just build` after every edit — Python tests use the compiled extension.
3. **Python changes** live in `python/django_bolt/`. No rebuild needed.
4. **Keep changes focused.** Small, surgical PRs are reviewed and merged faster than sweeping ones.
5. **Never silently swallow errors.** Raise or log — silent failures make bugs hard to trace.
6. **Imports at the top of the file**, and prefer `from __future__ import annotations`.

### Performance-sensitive code

Anything on the per-request path (`api.py:_dispatch`, `serialization.py`, `_kwargs/`, `dependencies.py`, `src/handler.rs`, `src/response_builder.rs`) follows one rule: **do it once at registration, reuse forever at runtime.** Avoid per-request allocations, `meta.get()` with defaults, `hasattr()` checks, and string dispatch in loops. If you touch these files, run `
## Testing

```bash
just test-py                                                    # everything
uv run --with pytest pytest python/tests/test_syntax.py -s -vv  # one file
uv run --with pytest pytest python/tests/test_syntax.py::test_name -s -vv
```

Guidelines:

- **Red-Green.** Write the test first and confirm it fails without your change. A test that passes without the fix is not testing the fix.
- **Use `TestClient`** for integration tests. It runs requests through the full Rust pipeline in-process — fast and deterministic:

  ```python
  from django_bolt import BoltAPI
  from django_bolt.testing import TestClient

  api = BoltAPI()

  @api.get("/hello")
  async def hello():
      return {"message": "world"}

  with TestClient(api) as client:
      assert client.get("/hello").status_code == 200
  ```

- **Subprocess (`runbolt`) tests** are only for behavior `TestClient` cannot exercise: startup wiring, auto-reload, multi-process, real TCP, streaming, WebSocket handshakes. Mark them `@pytest.mark.server_integration` and author the app as a real module in `python/tests/integration/apps/`.
- **Test behavior, not implementation.** Assert on HTTP responses and observable side effects.
- **Don't delete or skip failing asserts** to make a test pass — investigate the root cause.

## Code style

- Python is linted and formatted with [ruff](https://docs.astral.sh/ruff/). `just lint-lib` must pass with zero errors for library code.
- Rust follows `cargo fmt` / `cargo clippy` defaults.
- Run `just format` before committing.

## Benchmarking

```bash
just save-bench          # full suite → python/benchmark/BENCHMARK.md; git diff shows the delta
```

Always publish the conditions (processes, concurrency, request count, machine) next to any number you report. See [`docs/PROFILING.md`](docs/PROFILING.md) for the layered measurement strategy (micro → in-process → macro → flamegraphs).

## Documentation

Docs live in `docs/src/` and are published at [bolt.farhana.li](https://bolt.farhana.li/). If your change adds or alters user-facing behavior, update the relevant topic guide in `docs/src/topics/` and, for notable changes, add an entry to `CHANGELOG.md`.

## Submitting a pull request

1. Make sure `just test-py` and `just lint-lib` pass.
2. If you touched Rust, make sure `just build` succeeds from clean (`just rebuild`).
3. If you touched hot paths, include before/after numbers from `4. Push your branch and open a PR against `master`. Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md).
5. Keep the PR description focused on **what** changed and **why**; link related issues.

A maintainer will review your PR. Please be patient — and feel free to ping on Discord if it has been quiet for a while.

## Areas where help is welcome

- Testing and fixing bugs
- Extension points (lifecycle events, richer dependency injection)
- Code cleanup and simplification
- Examples, tutorials, and documentation

## Getting help

- 💬 [Discord](https://discord.gg/4xErptXK82)
- 🐛 [GitHub Issues](https://github.com/dj-bolt/django-bolt/issues)
- 📖 [Documentation](https://bolt.farhana.li/)

By contributing, you agree that your contributions will be licensed under the project's MIT License.
