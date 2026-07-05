from __future__ import annotations

from bolt_mcp import MCP, mount_mcp, principal
from bolt_mcp.oauth import AuthorizationServer

from django_bolt import BoltAPI, HasPermission, IsAuthenticated, Request

ISSUER = "http://testserver"

api = BoltAPI()
mcp = MCP("oauth-itest", "1.0")


@api.get("/health")
async def health():
    return {"status": "ok"}


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


@mcp.tool(guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    return principal(request)


@mcp.tool(guards=[HasPermission("reports:read")])
async def read_report() -> dict:
    return {"report": "Q3 up 42%"}


class ITestAuth(AuthorizationServer):
    issuer = ISSUER
    auto_consent = True

    def get_extra_claims(self, user, *, scopes, client_id):
        return {"permissions": ["reports:read"] if user.is_staff else []}


mount_mcp(api, mcp, oauth=ITestAuth())
