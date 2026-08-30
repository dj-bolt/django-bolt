"""App under test for layered ``include_in_schema``.

A hidden sub-API, a hidden view class, and a hidden route. One route in the
hidden sub-API opts back in. All routes are still served.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.views import APIView

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/public")
async def public():
    return {"public": True}


@api.get("/route-hidden", include_in_schema=False)
async def route_hidden():
    return {"hidden": "route"}


@api.view("/view-hidden")
class HiddenView(APIView):
    include_in_schema = False

    async def get(self, request):
        return {"hidden": "view"}


internal = BoltAPI(include_in_schema=False)


@internal.get("/secret")
async def secret():
    return {"hidden": "api"}


@internal.get("/shown", include_in_schema=True)
async def shown():
    return {"shown": True}


api.mount("/internal", internal)
