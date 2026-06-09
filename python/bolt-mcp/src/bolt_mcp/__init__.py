"""bolt-mcp: build MCP servers on django-bolt.

    from django_bolt import BoltAPI
    from bolt_mcp import MCP

    api = BoltAPI()
    mcp = MCP("my-server")

    @mcp.tool
    async def greet(name: str) -> dict:
        return {"hello": name}

    api.mount_mcp(mcp)   # serves the MCP Streamable HTTP endpoint at /mcp

The free function ``mount_mcp(api, mcp)`` is the underlying implementation, equivalent
to the ``api.mount_mcp(mcp)`` method.
"""

from __future__ import annotations

from .autoexpose import expose_as_tool, expose_routes
from .context import Context
from .oauth import AuthorizationServer
from .server import MCP, principal
from .transport import ProtectedResource, mount_mcp

__all__ = [
    "MCP",
    "mount_mcp",
    "ProtectedResource",
    "AuthorizationServer",
    "principal",
    "expose_routes",
    "expose_as_tool",
    "Context",
]
