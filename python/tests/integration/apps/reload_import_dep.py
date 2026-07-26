"""Reload fixture whose response comes from an imported helper module.

Unlike the other app fixtures this one is deliberately not self-contained: it
imports ``reload_helper``, which the test installs as a sibling top-level
module in the temp project (from ``_reload_helper``; the import resolves
against the project root, not against this package). That cross-module edge is
the point — the ``--dev`` reloader must watch the api module's transitive
imports too.

Consequence: unlike every other fixture here, this module cannot be imported
in-process from the source tree, so ``test_apps_smoke`` only syntax-checks it.
"""

from __future__ import annotations

import reload_helper

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/helper-version")
async def helper_version():
    return {"version": reload_helper.HELPER_VERSION}
