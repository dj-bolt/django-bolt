from __future__ import annotations

import asyncio
import time
from pathlib import Path

from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("tasks-itest", "1.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool(task=True, poll_interval_ms=20)
async def multiply(a: int, b: int, ctx: Context) -> dict:
    await ctx.report_progress(1, 2, "multiplying")
    await asyncio.sleep(0.3)
    return {"product": a * b, "task_id": ctx.task_id}


@mcp.tool(task=True)
async def confirm(ctx: Context) -> dict:
    answer = await ctx.elicit("Proceed?")
    return {"answer": answer["content"]}


@mcp.tool(task=True)
def sync_sleep(seconds: float, marker: str) -> dict:
    Path(marker, "started").touch()
    time.sleep(seconds)
    return {"slept": seconds}


@mcp.tool(task=True)
async def cleanup(marker: str) -> dict:
    Path(marker, "started").touch()
    try:
        await asyncio.sleep(30)
    finally:
        Path(marker, "finally").touch()
    return {"never": True}


mount_mcp(api, mcp)
