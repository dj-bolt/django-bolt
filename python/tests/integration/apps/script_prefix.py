"""App under test for ``FORCE_SCRIPT_NAME`` behind a proxy that removes the sub-path.

Each route gives the script prefix that user code sees at one place: a
handler, a thread hop, a dependency, a streaming body, a WebSocket handler, a
lifespan and an error formatter. The routes under ``/dj`` run behind Django
middleware, on request lanes.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Annotated

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.urls import get_script_prefix

from django_bolt import BoltAPI, Depends, StreamingResponse, WebSocket
from django_bolt.concurrency import run_in_orm_executor, sync_to_thread
from django_bolt.responses import JSON

_LIFESPAN: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app):
    _LIFESPAN["startup"] = get_script_prefix()
    yield


def formatter(error, request) -> JSON:
    return JSON({"prefix": get_script_prefix(), "path": request.path}, status_code=error.status)


class HeaderMiddleware:
    """A sync Django middleware: the routes behind it run on request lanes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["x-django-prefix"] = get_script_prefix()
        response["x-django-path"] = request.path
        return response


def _prefix() -> str:
    return get_script_prefix()


async def _async_prefix() -> str:
    return get_script_prefix()


def _add_routes(target: BoltAPI) -> None:
    @target.get("/sync")
    def sync_handler(request) -> dict:
        return {"prefix": _prefix(), "path": request.path, "path_info": request.path_info}

    @target.get("/async")
    async def async_handler() -> dict:
        before = _prefix()
        await asyncio.sleep(0.001)
        return {"prefix": before, "after_await": _prefix()}

    @target.get("/orm")
    def orm_handler() -> dict:
        User.objects.exists()
        return {"prefix": _prefix()}

    @target.get("/async-orm")
    async def async_orm_handler() -> dict:
        await User.objects.aexists()
        return {"prefix": _prefix()}

    @target.get("/hops")
    async def hops(sync: Annotated[str, Depends(_prefix)], other: Annotated[str, Depends(_async_prefix)]) -> dict:
        return {
            "sync_dependency": sync,
            "async_dependency": other,
            "sync_to_thread": await sync_to_thread(_prefix),
            "run_in_orm_executor": await run_in_orm_executor(_prefix),
            "sync_to_async": await sync_to_async(_prefix)(),
        }

    @target.get("/stream-sync")
    def stream_sync():
        def body():
            yield json.dumps({"prefix": _prefix()})

        return StreamingResponse(body(), media_type="application/json")

    @target.get("/stream-async")
    async def stream_async():
        async def body():
            await asyncio.sleep(0)
            yield json.dumps({"prefix": _prefix()})

        return StreamingResponse(body(), media_type="application/json")


api = BoltAPI(lifespan=lifespan, error_formatter=formatter)
django_routes = BoltAPI(django_middleware=["tests.integration.apps.script_prefix.HeaderMiddleware"])


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/lifespan")
async def lifespan_prefix() -> dict:
    return {"prefix": _LIFESPAN["startup"]}


@api.websocket("/ws")
async def websocket(ws: WebSocket, dependency: Annotated[str, Depends(_prefix)]):
    await ws.accept()
    await ws.send_json({"handler": _prefix(), "dependency": dependency})
    await ws.close()


_add_routes(api)
_add_routes(django_routes)
api.mount("/dj", django_routes)
