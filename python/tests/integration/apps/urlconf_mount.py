"""App under test for a BoltAPI that the URLconf mounts.

``api`` serves ``/health`` and ``/link`` at their own paths. The project URLconf mounts
``missions`` with ``path("missions/", missions.urls)``, so runbolt serves
the routes of ``missions`` under ``/missions/`` only.
"""

from __future__ import annotations

from django.urls import reverse

from django_bolt import BoltAPI

api = BoltAPI()
missions = BoltAPI(namespace="missions")


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/link")
async def link():
    return {"url": reverse("missions:detail", kwargs={"mission_id": 42})}


@missions.get("/{mission_id}", name="detail")
async def mission_detail(mission_id: int):
    return {"id": mission_id}
