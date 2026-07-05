from __future__ import annotations

from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("bidi-itest", "1.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool
async def echo_via_llm(text: str, ctx: Context) -> dict:
    reply = await ctx.sample(text)
    return {"reply": reply["content"]["text"]}


@mcp.tool
async def confirm(ctx: Context) -> dict:
    answer = await ctx.elicit("Proceed?")
    return {"answer": answer["content"]}


mount_mcp(api, mcp)
