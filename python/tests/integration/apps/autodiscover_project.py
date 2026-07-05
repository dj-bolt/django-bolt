"""Project-root app for the autodiscovery test.

Installed as the project package's ``api.py``; runbolt should discover it
alongside the separate ``extraapp`` app (see ``autodiscover_extraapp``).
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/project-api")
async def project_api():
    return {"source": "project"}
