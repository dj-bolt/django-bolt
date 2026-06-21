"""Installed-app ``api.py`` for the autodiscovery test.

Loaded via ``app_text(...)`` into ``extraapp/api.py`` (not as the project's own
``api.py``) so the test can prove runbolt discovers Bolt routes in installed
Django apps, not just the project root.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/app-api")
async def app_api():
    return {"source": "app"}
