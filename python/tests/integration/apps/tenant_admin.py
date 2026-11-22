"""App under test for an admin site behind a path parameter (issue #872).

The project URLconf has ``path("<slug:tenant>/admin/", site.urls)``. ``runbolt``
mounts Django at the root path, so the admin answers for each tenant, and the
Bolt route of this module answers first.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/items/{item_id}")
async def item(item_id: int):
    return {"item_id": item_id}
