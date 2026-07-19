"""Dispatch-path probe app: one route per Rust↔Python dispatch mechanism.

Used two ways:
- Correctness: the eager loop-thread dispatch integration tests drive every
  async-bridge shape through a real ``runbolt`` server (TestClient uses a
  different bridge, so only a subprocess exercises the production path).
- Measurement: point a ``runbolt`` server at this module and time these routes
  over a keepalive connection to price each dispatch stage (see
  docs/PROFILING.md). ``t_ready - t_trivial`` is the pure async-bridge cost;
  ``t_sleep0 - t_ready`` is one real suspend.

Probe map:
- /t-sync     sync def                    → sync-dispatch bypass
- /t-trivial  async def, no await         → trivially-async sync dispatch
- /t-ready    awaits, never suspends      → eager dispatch completes inline
- /t-sleep0   await asyncio.sleep(0)      → eager start + bare-yield reschedule
- /t-thread   await sync_to_thread(...)   → eager start + real Future suspension
- /t-exc      HTTPException after await   → exception through the driver Task
- /t-deps     two async Depends           → asyncio.gather in the first segment
- /t-task     create_task in first segment → loop APIs during eager execution
- /t-stream   async generator (NDJSON)    → streaming wire through async path
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from django_bolt import BoltAPI
from django_bolt.concurrency import sync_to_thread
from django_bolt.exceptions import HTTPException
from django_bolt.params import Depends

api = BoltAPI()
PAYLOAD = {"message": "hello", "n": 1}


async def _noop():
    return None


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/t-sync")
def t_sync():
    return PAYLOAD


@api.get("/t-trivial")
async def t_trivial():
    return PAYLOAD


@api.get("/t-ready")
async def t_ready():
    await _noop()
    return PAYLOAD


@api.get("/t-sleep0")
async def t_sleep0():
    await asyncio.sleep(0)
    return PAYLOAD


@api.get("/t-thread")
async def t_thread():
    value = await sync_to_thread(lambda: "from-thread")
    return {"value": value}


@api.get("/t-exc")
async def t_exc():
    await asyncio.sleep(0)
    raise HTTPException(status_code=418, detail="teapot-after-await")


async def _dep_a() -> str:
    await asyncio.sleep(0)
    return "a"


async def _dep_b() -> str:
    await asyncio.sleep(0)
    return "b"


@api.get("/t-deps")
async def t_deps(a: Annotated[str, Depends(_dep_a)], b: Annotated[str, Depends(_dep_b)]):
    return {"a": a, "b": b}


@api.get("/t-task")
async def t_task():
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(_dep_a())
    return {"loop_running": loop.is_running(), "task_result": await task}


@api.get("/t-stream")
async def t_stream():
    async def gen():
        for i in range(3):
            await asyncio.sleep(0)
            yield f'{{"i": {i}}}\n'.encode()

    return gen()
