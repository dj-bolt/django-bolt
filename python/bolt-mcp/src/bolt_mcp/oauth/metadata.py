"""Discovery metadata documents.

RFC 8414 Authorization Server Metadata is served here; RFC 9728 Protected Resource
Metadata is served by the transport from the internal ``ProtectedResource``.
"""

from __future__ import annotations

from typing import Any

from .config import AuthorizationServer


def authorization_server_metadata(server: AuthorizationServer) -> dict[str, Any]:
    """RFC 8414 document advertised at ``/.well-known/oauth-authorization-server``."""
    ep = server.endpoint_urls()
    md: dict[str, Any] = {
        "issuer": server.effective_issuer(),
        "authorization_endpoint": ep["authorize"],
        "token_endpoint": ep["token"],
        "revocation_endpoint": ep["revoke"],
        "scopes_supported": list(server.scopes_supported),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    if server.allow_dynamic_registration:
        md["registration_endpoint"] = ep["register"]
    return md
