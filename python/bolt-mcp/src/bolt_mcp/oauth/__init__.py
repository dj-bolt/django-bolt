"""Built-in OAuth 2.1 Authorization Server for bolt-mcp.

Enables the standard MCP OAuth handshake (Claude.ai / ChatGPT custom connectors):
``/mcp`` 401 → RFC 9728 Protected Resource Metadata → RFC 8414 Authorization Server
Metadata → Dynamic Client Registration → Authorization Code + PKCE → access + refresh
tokens (silent refresh). State is persisted in Django ORM models, the human is
authenticated via Django sessions, and access tokens are ``SECRET_KEY``-signed JWTs
whose claims match what django-bolt's ``JWTAuthentication``/guards already read.

Enable it by adding ``"bolt_mcp.oauth"`` to ``INSTALLED_APPS``, running
``manage.py migrate``, and mounting with the built-in AS::

    from bolt_mcp import MCP
    from bolt_mcp.oauth import AuthorizationServer

    api.mount_mcp(mcp, oauth=AuthorizationServer(issuer="https://api.example.com"))
"""

from __future__ import annotations

from .config import AuthorizationServer

__all__ = ["AuthorizationServer"]
