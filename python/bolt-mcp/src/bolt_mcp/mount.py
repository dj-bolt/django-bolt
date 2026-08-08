"""``mount_mcp``: compile the registry and hand the mount to the Rust core.

The MCP protocol itself (dual-era Streamable HTTP: 2026-07-28 stateless +
legacy sessions) is served by django-bolt's embedded rmcp core. Python's job
ends at registration: compile the catalog + dispatch surface into a mount
definition, register the OAuth well-known route, and stash the definition on
the API for the runbolt registrar / TestClient to pass to Rust.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from django_bolt import JSON, BoltAPI, Request

from ._catalog import build_mount_definition
from ._dispatch import make_verify_token
from .autoexpose import expose_routes
from .server import MCP

WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"


@dataclass
class ProtectedResource:
    """OAuth 2.1 Resource Server configuration (Tier 2 auth)."""

    resource_url: str
    authorization_servers: list[str] = field(default_factory=list)
    required_scopes: tuple[str, ...] = ()
    token_verifier: Any = None


def _protected_resource_metadata(oauth: ProtectedResource) -> dict[str, Any]:
    return {
        "resource": oauth.resource_url,
        "authorization_servers": list(oauth.authorization_servers),
        "scopes_supported": list(oauth.required_scopes),
        "bearer_methods_supported": ["header"],
    }


def _resolve_oauth(api: BoltAPI, oauth: Any, mount_path: str) -> ProtectedResource | None:
    """Normalize the ``oauth=`` argument to a ``ProtectedResource``.

    A built-in ``AuthorizationServer`` additionally registers the AS discovery/
    DCR/authorize/token endpoints and yields a ProtectedResource that validates
    the JWTs it issues. A plain ``ProtectedResource`` (external IdP) is
    returned unchanged.
    """
    if oauth is None:
        return None
    from .oauth.config import AuthorizationServer  # noqa: PLC0415 — keep OAuth deps optional/lazy

    if isinstance(oauth, AuthorizationServer):
        from .oauth.endpoints import register_oauth_endpoints  # noqa: PLC0415
        from .oauth.tokens import make_token_verifier  # noqa: PLC0415

        bound_path = getattr(oauth, "_mcp_mount_path", None)
        if bound_path is not None and bound_path != mount_path:
            raise ValueError(
                f"This AuthorizationServer is already bound to the MCP mount at {bound_path!r}; "
                "use a separate AuthorizationServer for a different mount path"
            )
        if oauth.resource_url is None:
            # The canonical MCP resource URI is the endpoint URL (issuer +
            # mount path). Strict clients canonicalize the URL they connect
            # to and compare it against the advertised ``resource`` — a bare
            # origin would mismatch. Set before minting/verifier wiring so
            # the token audience uses the same value.
            oauth.resource_url = oauth.effective_issuer() + mount_path
        resource_url = oauth.effective_resource_url()
        bound_resource_url = getattr(oauth, "_mcp_resource_url", None)
        if bound_resource_url is not None and bound_resource_url != resource_url:
            raise ValueError(
                f"This AuthorizationServer is already bound to the MCP resource {bound_resource_url!r}; "
                f"its current resource URL is {resource_url!r}"
            )
        oauth._mcp_mount_path = mount_path
        oauth._mcp_resource_url = resource_url
        register_oauth_endpoints(api, oauth)
        return ProtectedResource(
            resource_url=resource_url,
            authorization_servers=[oauth.effective_issuer()],
            required_scopes=oauth.required_scopes,
            token_verifier=make_token_verifier(oauth),
        )
    if isinstance(oauth, ProtectedResource):
        return oauth
    raise TypeError(f"oauth must be a ProtectedResource or an oauth.AuthorizationServer, got {type(oauth).__name__}")


def mount_mcp(
    api: BoltAPI,
    mcp: MCP,
    path: str = "/mcp",
    *,
    auth: list[Any] | None = None,
    guards: list[Any] | None = None,
    oauth: Any | None = None,
    expose: Sequence[Callable] | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
) -> None:
    """Serve ``mcp`` over the MCP Streamable HTTP transport at ``path``.

    This is the implementation behind ``api.mount_mcp(mcp, ...)``; calling
    either is equivalent. The endpoint speaks both protocol eras: 2026-07-28
    requests are served statelessly (multi-worker safe); legacy clients get
    the ``initialize``/session flow unless ``MCP(stateless=True)``.

    By default only native ``@mcp.tool``/``@mcp.resource``/``@mcp.prompt``
    components are served — existing REST routes are NEVER exposed implicitly.
    Exposing a route makes it callable by any MCP client, so it is an
    explicit, per-handler opt-in::

        mount_mcp(api, mcp, expose=[get_item, list_users])

    There is intentionally no "expose everything" switch. For deliberate
    glob/method bulk selection, call :func:`expose_routes` directly before
    mounting.

    Tier 1 auth: pass ``auth=``/``guards=`` — enforced in Rust before any
    protocol handling; per-tool ``guards=`` on ``@mcp.tool`` are evaluated in
    Rust too. Tier 2 auth: pass ``oauth=...`` (a ``ProtectedResource`` for an
    external IdP, or an ``oauth.AuthorizationServer`` for the built-in
    Django-backed OAuth 2.1 server). ``oauth`` and ``guards`` are mutually
    exclusive on the mount.

    DNS-rebinding protection defaults to localhost-only Host validation.
    Deployed servers must pass their public authorities in ``allowed_hosts``;
    ``allowed_origins`` optionally enables Origin validation too. Passing an
    empty ``allowed_hosts`` list explicitly disables Host validation when a
    trusted proxy already enforces it.
    """
    if any(existing["path"] == path for existing in api._mcp_mounts):
        raise ValueError(f"An MCP server is already mounted at {path!r} on this API")

    from django_bolt import _core  # noqa: PLC0415 — verify the Rust core at mount time

    if not hasattr(_core, "register_mcp_mounts"):
        raise ImportError(
            "bolt-mcp >= 0.2 requires django-bolt >= 0.10 (its Rust extension embeds the "
            "MCP protocol core). Upgrade django-bolt, or pin bolt-mcp < 0.2."
        )
    if expose is True:
        raise TypeError(
            "mount_mcp(expose=...) takes an explicit list of route handlers, not True. "
            "Exposing routes to MCP clients is a security-sensitive, per-handler opt-in "
            "with no expose-everything switch — pass e.g. expose=[get_item]. For "
            "deliberate bulk selection use expose_routes(mcp, api, ...)."
        )
    if expose:
        expose_routes(mcp, api, handlers=expose)  # explicit handler allowlist

    oauth_resource = _resolve_oauth(api, oauth, path)

    # Tier-1 backends follow the same default resolution as routes: an
    # explicit list wins, otherwise the API-wide default authentication
    # classes apply (harmless context enrichment when no guards are set).
    from django_bolt.auth import get_default_authentication_classes  # noqa: PLC0415 — Django must be configured first

    backends = auth if auth is not None else (get_default_authentication_classes() or [])
    auth_metadata = [b.to_metadata() for b in backends] or None
    mount_guards = [] if oauth_resource is not None else list(guards or [])
    guards_metadata = [g.to_metadata() for g in mount_guards]

    verify_token = None
    www_authenticate = None
    if oauth_resource is not None:
        verify_token = make_verify_token(oauth_resource)
        # RFC 9728 metadata location: the well-known segment goes between the
        # origin and the resource's path — for resource "https://host/mcp" the
        # document lives at "https://host/.well-known/oauth-protected-resource/mcp".
        parts = urlsplit(oauth_resource.resource_url)
        resource_path = parts.path.rstrip("/")
        origin = f"{parts.scheme}://{parts.netloc}"
        metadata_url = f"{origin}{WELL_KNOWN_PROTECTED_RESOURCE}{resource_path}"
        www_authenticate = f'Bearer resource_metadata="{metadata_url}"'

        # Static discovery document: resource config is fixed at mount.
        protected_resource_doc = _protected_resource_metadata(oauth_resource)

        async def _mcp_protected_resource_metadata(request: Request):
            return JSON(protected_resource_doc)

        # RFC 9728 location derived from the resource identifier: path-aware
        # for a resource with a path, root for a path-less resource. The
        # WWW-Authenticate challenge points at the same URL.
        api.get(f"{WELL_KNOWN_PROTECTED_RESOURCE}{resource_path}")(_mcp_protected_resource_metadata)

    secret_key = getattr(settings, "SECRET_KEY", "")
    if not secret_key:
        raise RuntimeError("mount_mcp requires Django settings.SECRET_KEY (it keys MRTR request state)")

    definition = build_mount_definition(
        mcp,
        path=path,
        secret_key=secret_key,
        max_body_bytes=getattr(settings, "BOLT_MAX_UPLOAD_SIZE", 1024 * 1024),
        auth_metadata=auth_metadata,
        guards_metadata=guards_metadata,
        verify_token=verify_token,
        www_authenticate=www_authenticate,
        allowed_hosts=list(allowed_hosts) if allowed_hosts is not None else None,
        allowed_origins=list(allowed_origins) if allowed_origins is not None else None,
    )
    api._mcp_mounts.append(definition)
