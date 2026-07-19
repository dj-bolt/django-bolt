"""Contextvar propagation across Bolt's bounded executor helpers.

Request-scoped state (e.g. a tenant-aware database router selecting the
database per request) lives in ContextVars. Executor hops must carry the
caller's context into the worker thread, or ORM evaluation runs without the
tenant/database selection established by the request.
"""

from __future__ import annotations

import asyncio
import contextvars

from django_bolt.concurrency import run_in_orm_executor, sync_to_thread

_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant", default=None)


def test_run_in_orm_executor_propagates_contextvars():
    async def main():
        _tenant.set("acme")
        return await run_in_orm_executor(_tenant.get)

    assert asyncio.run(main()) == "acme"


def test_sync_to_thread_propagates_contextvars():
    def read_with_suffix(suffix: str) -> str:
        return f"{_tenant.get()}-{suffix}"

    async def main():
        _tenant.set("globex")
        positional = await sync_to_thread(_tenant.get)
        with_kwargs = await sync_to_thread(read_with_suffix, suffix="kw")
        return positional, with_kwargs

    assert asyncio.run(main()) == ("globex", "globex-kw")
