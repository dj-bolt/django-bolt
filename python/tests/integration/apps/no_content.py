"""Routes that answer 204 with each response type a handler can return.

The real-TCP test pipelines a second request on the same connection. A 204
that writes a body corrupts that next response.
"""

from __future__ import annotations

from django.http import HttpResponse

from django_bolt import BoltAPI
from django_bolt.responses import JSON, Response

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.delete("/response", status_code=204)
async def delete_response() -> None:
    return Response(status_code=204)


@api.delete("/json", status_code=204)
async def delete_json() -> None:
    return JSON({"deleted": True}, status_code=204)


@api.delete("/django", status_code=204)
def delete_django():
    return HttpResponse(b"gone", status=204)


@api.delete("/none", status_code=204)
async def delete_none() -> None:
    return None
