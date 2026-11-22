"""App under test for an admin site behind a path parameter (issue #872).

The project URLconf has ``path("<slug:tenant>/admin/", site.urls)``. ``runbolt``
mounts Django at the root path, so the admin answers for each tenant.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}
