"""Real, self-contained app modules for subprocess integration tests.

Each module here is ordinary Python — linted, type-checked, and navigable in an
IDE — that defines its own ``api = BoltAPI()``, a ``/health`` readiness route,
and the handlers under test. Tests install one into a ``runbolt`` subprocess via
``create_server_project(api_source=app_source("..."))`` (the file is copied
verbatim, so what you read is exactly what runs), and the same module can be
imported and driven in-process with ``TestClient`` for fast coverage.

This replaces the old pattern of authoring handlers inside triple-quoted source
strings (see issue #218): strings get no tooling and, in the worst cases, became
f-strings with ``{{}}``-escaped braces.

Modules must stay self-contained — no relative imports — so they copy cleanly
into the subprocess project as ``<package>/api.py``.
"""

from __future__ import annotations

from pathlib import Path

_APPS_DIR = Path(__file__).resolve().parent


def app_source(name: str) -> Path:
    """Return the path to the app module ``name`` (without the ``.py`` suffix).

    Pass the result as ``create_server_project(api_source=...)`` to install the
    module as the subprocess project's ``api.py``.
    """
    path = _APPS_DIR / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"No integration test app module named {name!r} at {path}")
    return path
