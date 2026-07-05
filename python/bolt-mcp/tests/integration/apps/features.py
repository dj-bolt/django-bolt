from __future__ import annotations

from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("features-itest", "2.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool
async def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {"sum": a + b}


@mcp.tool
async def boom() -> dict:
    """Always raises - exercises in-band tool errors."""
    raise ValueError("kaboom")


@mcp.tool
async def crunch(n: int, ctx: Context) -> dict:
    """Stream progress, then return - exercises Context notifications over SSE."""
    for i in range(n):
        await ctx.report_progress(i, n, message=f"step {i}")
    await ctx.info("almost done")
    return {"done": n}


@mcp.resource(
    "config://app",
    name="app-config",
    mime_type="application/json",
    description="Static app configuration",
)
async def app_config() -> str:
    return '{"env": "itest"}'


@mcp.resource("users://{user_id}/profile", name="user-profile", mime_type="application/json")
async def user_profile(user_id: int) -> str:
    # user_id is extracted from the URI as a string and must arrive coerced to int.
    return f'{{"id": {user_id}, "type": "{type(user_id).__name__}"}}'


@mcp.prompt
async def summarize(topic: str) -> str:
    """Summarize a topic."""
    return f"Please summarize: {topic}"


@api.get("/double/{n}")
async def double(n: int) -> dict:
    """Double a number."""
    return {"result": n * 2}


mount_mcp(api, mcp, expose=[double])
