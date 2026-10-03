from __future__ import annotations

import asyncio

from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("tasks-itest", "1.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool(task=True, poll_interval=20)
async def multiply(a: int, b: int, ctx: Context) -> dict:
    await ctx.report_progress(1, 2, "multiplying")
    await asyncio.sleep(0.2)
    return {"product": a * b, "task_id": ctx.task_id}


@mcp.tool(task=True)
async def confirm(ctx: Context) -> dict:
    answer = await ctx.elicit("Proceed?")
    return {"answer": answer["content"]}


mount_mcp(api, mcp)
