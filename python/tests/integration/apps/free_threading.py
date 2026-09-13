"""App under test for free-threaded CPython (PEP 703) support.

``/gil`` reports the interpreter build and whether the GIL is enabled, so a
``runbolt`` subprocess can prove that importing the Rust extension did not
re-enable the GIL. The other routes cover every dispatch shape - sync,
trivially-async, awaiting async, and streaming - so many client threads can
drive them at once. On a free-threaded interpreter the Actix worker threads
run these handlers in parallel with no GIL to serialize them.
"""

from __future__ import annotations

import asyncio
import sys
import sysconfig

from django_bolt import BoltAPI
from django_bolt.responses import StreamingResponse

api = BoltAPI()


def gil_state() -> dict[str, bool]:
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    return {
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": True if is_gil_enabled is None else bool(is_gil_enabled()),
    }


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/gil")
async def gil():
    return gil_state()


@api.get("/sync/{n}")
def sync_route(n: int):
    return {"n": n, "kind": "sync"}


@api.get("/trivial/{n}")
async def trivial_route(n: int):
    """No await: runs through the sync-dispatch bypass on the worker thread."""
    return {"n": n, "kind": "trivial"}


async def _double(value: int) -> int:
    await asyncio.sleep(0)
    return value * 2


@api.get("/awaiting/{n}")
async def awaiting_route(n: int):
    """Real suspensions: the task starts on the worker thread and resumes on the WorkerLoop pump."""
    await asyncio.sleep(0)
    parts = await asyncio.gather(*(_double(i) for i in range(3)))
    return {"n": n, "kind": "awaiting", "parts": list(parts)}


@api.get("/stream/{n}")
async def stream_route(n: int):
    async def chunks():
        for i in range(3):
            await asyncio.sleep(0)
            yield f"{n}:{i}\n"

    return StreamingResponse(chunks(), media_type="text/plain")
