"""Real, self-contained app modules for subprocess integration tests.

Each module here is ordinary Python — linted, type-checked, and navigable in an
IDE — that defines its own ``api = BoltAPI()``, a ``/health`` readiness route,
and the handlers under test. Tests usually point a ``runbolt`` subprocess at one
by import path via ``create_server_project(api_module=app_module("..."))`` (which
sets ``settings.BOLT_API``), so the subprocess imports the *same* module object
that the in-process ``TestClient`` uses — no file copy. ``app_source(...)`` is
the copy-into-``api.py`` fallback for the few tests that need an on-disk file
(autodiscovery, ``--dev``/reload, artifact).

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


def app_text(name: str) -> str:
    """Return the source text of app module ``name``.

    Use this when the module must be installed somewhere other than the
    project's own ``api.py`` — e.g. as a secondary app's ``api.py`` passed via
    ``create_server_project(extra_files=...)``.
    """
    return app_source(name).read_text()


def app_module(name: str, attr: str = "api") -> str:
    """Return the ``"package.module:attr"`` path for ``api_module=``.

    This points runbolt at the module via ``settings.BOLT_API`` (no file copy),
    so the subprocess imports the exact module object the in-process
    ``TestClient`` uses. Prefer this over ``app_source`` except where a test
    needs an on-disk ``api.py`` — autodiscovery, ``--dev``/reload (the watcher
    needs a file in the temp project), or artifact tests (which must run from
    the installed wheel, not the source tree).
    """
    app_source(name)  # validate the module exists
    return f"{__name__}.{name}:{attr}"
