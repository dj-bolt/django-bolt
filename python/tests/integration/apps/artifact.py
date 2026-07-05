"""Minimal app under test for the installed-artifact smoke tests.

Installed into a project that runs from a pip-installed wheel/sdist (not the
source tree) to prove the packaged ``runbolt`` boots and serves a request.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/artifact")
async def artifact():
    return {"mode": "ok"}
