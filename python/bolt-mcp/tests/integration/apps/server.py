from __future__ import annotations

from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("itest-server", "1.2.3")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
