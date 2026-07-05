from __future__ import annotations

from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI, IsAuthenticated, JWTAuthentication

SECRET = "itest-auth-secret-0123456789-abcdefgh"

api = BoltAPI()
mcp = MCP("auth-itest", "1.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp, auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
