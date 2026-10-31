"""App under test for a StreamingResponse that a middleware returns behind Django middleware.

A Python middleware answers ``/stream`` with a ``StreamingResponse``. A Django
middleware runs around it and adds a header. Other paths reach their handler.
"""

from __future__ import annotations

from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.middleware import DjangoMiddleware, Middleware
from django_bolt.responses import StreamingResponse


class SeenByDjango(MiddlewareMixin):
    """Django middleware that marks each response that reaches its process_response."""

    def process_response(self, request, response):
        response["X-Seen"] = "yes"
        return response


def _chunks():
    for i in range(3):
        yield f"chunk-{i}\n".encode()


class StreamAnswer(Middleware):
    """Python middleware that answers /stream with a StreamingResponse."""

    async def process_request(self, request):
        if request.path == "/stream":
            return StreamingResponse(_chunks(), media_type="text/plain")
        return await self.get_response(request)


api = BoltAPI(middleware=[DjangoMiddleware(SeenByDjango), StreamAnswer])


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/stream")
async def not_reached():
    return {"reached": True}
