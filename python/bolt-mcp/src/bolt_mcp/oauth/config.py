"""The OAuth 2.1 Authorization Server — a Django class-based-view-style component.

Configure with class attributes; change behavior by overriding methods. Just like a
Django CBV (``ListView``/``LoginView``)::

    class MyMcpAuth(AuthorizationServer):
        issuer = "https://api.example.com"        # config = class attributes
        access_token_ttl = 1800

        def get_extra_claims(self, user, *, scopes, client_id):
            return {"tenant_id": user.profile.tenant_id, "roles": [g.name for g in user.groups.all()]}

        def render_consent(self, params, *, client_name, username):
            return my_template.render(...)

        async def authenticate(self, username, password):
            ...

    api.mount_mcp(mcp, oauth=MyMcpAuth())

For trivial cases you can set attributes inline (like ``View.as_view(**initkwargs)``)
instead of subclassing: ``AuthorizationServer(issuer="https://api.example.com")``.
"""

from __future__ import annotations

import warnings
from typing import Any

# Used only when ``issuer`` is left unset — the common local-dev case. Anything real MUST
# set ``issuer``: it is the stable identity clients compare byte-for-byte and the audience/
# issuer baked into every token. A wrong base breaks discovery and token validation.
DEFAULT_DEV_ISSUER = "http://localhost:8000"


class AuthorizationServer:
    """Built-in, Django-backed OAuth 2.1 Authorization Server for an MCP mount.

    Issues ``SECRET_KEY``-signed JWT access tokens (claims compatible with django-bolt's
    auth) + rotating refresh tokens, and serves the discovery / DCR / authorize / token
    endpoints Claude/ChatGPT need to link a connector once and refresh silently.

    Configuration — set as class attributes in a subclass or pass as constructor kwargs:
        issuer: absolute origin of this server (e.g. ``https://api.example.com``). REQUIRED
            in production; defaults to ``http://localhost:8000`` (with a warning) if unset.
        resource_url: OAuth resource id + token audience. Defaults to ``issuer``.
        scopes_supported / required_scopes: advertised vs enforced-on-``/mcp`` scopes.
        access_token_ttl / refresh_token_ttl / auth_code_ttl: lifetimes (seconds).
        jwt_secret / jwt_algorithm: signing key/alg (default Django ``SECRET_KEY`` / HS256).
        auto_consent: skip the explicit consent screen immediately after an interactive
            login at ``/authorize``. An already signed-in user who lands on ``/authorize``
            via GET still gets a consent screen — a code is only issued through the
            Origin-checked POST, never from a bare ambient-session GET (CSRF defense).
        allow_dynamic_registration: enable RFC 7591 ``/register``.
        endpoint_prefix: path prefix for authorize/token/register/revoke (default ``/oauth``).

    Behavior — override these methods in a subclass to customize:
        get_extra_claims, authenticate, resolve_user, load_user, render_login,
        render_consent, redirect_uri_allowed.
    """

    # ── configuration (override via subclass attributes or constructor kwargs) ──
    issuer: str | None = None
    resource_url: str | None = None
    scopes_supported: tuple[str, ...] = ("mcp",)
    required_scopes: tuple[str, ...] = ()
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 60 * 60 * 24 * 30
    auth_code_ttl: int = 300
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    auto_consent: bool = False
    allow_dynamic_registration: bool = True
    endpoint_prefix: str = "/oauth"

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            if not hasattr(type(self), key):
                raise TypeError(f"AuthorizationServer got an unexpected option {key!r}")
            setattr(self, key, value)
        self._warned_default_issuer = False

    # ── config helpers ──────────────────────────────────────────────────────────
    def effective_issuer(self) -> str:
        if self.issuer:
            return self.issuer.rstrip("/")
        if not self._warned_default_issuer:
            warnings.warn(
                f"AuthorizationServer.issuer is unset; defaulting to {DEFAULT_DEV_ISSUER!r}. "
                "Set an explicit issuer for anything beyond local development — it is the "
                "token audience/issuer and the discovery base clients compare exactly.",
                stacklevel=3,
            )
            self._warned_default_issuer = True
        return DEFAULT_DEV_ISSUER

    def effective_resource_url(self) -> str:
        return (self.resource_url or self.effective_issuer()).rstrip("/")

    def endpoint_urls(self) -> dict[str, str]:
        base = self.effective_issuer()
        return {name: base + self.path(name) for name in ("authorize", "token", "register", "revoke")}

    def path(self, name: str) -> str:
        """Local (host-relative) path for an endpoint, used for route registration."""
        return "/" + self.endpoint_prefix.strip("/") + f"/{name}"

    # ── overridable behavior (Django-style: subclass and override) ────────────────
    def get_extra_claims(self, user: Any, *, scopes: list[str], client_id: str) -> dict:
        """Extra claims merged into the access token (sync; ORM access is fine here).

        Override to add tenant/role/plan/etc., optionally varying by ``scopes``/``client_id``.
        """
        return {}

    async def authenticate(self, username: str, password: str) -> dict | None:
        """Verify credentials at ``/authorize``. Returns a session dict or ``None``.

        Default: Django's ``authenticate()`` + a new session. Override to use a different
        credential source or MFA.
        """
        from . import sessions  # noqa: PLC0415 — keep Django imports lazy / OAuth optional

        return await sessions.login(username, password)

    async def resolve_user(self, request: Any) -> dict | None:
        """Resolve the already-signed-in user from the request's session cookie."""
        from . import sessions  # noqa: PLC0415

        return await sessions.user_from_session(request.cookies.get(sessions.session_cookie_name()))

    async def load_user(self, user_id: str) -> Any | None:
        """Load the Django user the access token is minted for (at the token endpoint)."""
        from . import sessions  # noqa: PLC0415

        return await sessions.load_user(user_id)

    def render_login(self, params: dict, *, error: str | None = None) -> str:
        """HTML for the sign-in page. Override to restyle or use your own template."""
        from . import consent  # noqa: PLC0415

        return consent.login_page(self, params, error=error)

    def render_consent(self, params: dict, *, client_name: str, username: str) -> str:
        """HTML for the consent page. Override to restyle or use your own template."""
        from . import consent  # noqa: PLC0415

        return consent.consent_page(self, params, client_name=client_name, username=username)

    def redirect_uri_allowed(self, registered: list[str] | None, redirect_uri: str) -> bool:
        """Whether ``redirect_uri`` is permitted. Default: exact match against registered
        values (no open redirect). Override only with great care."""
        return redirect_uri in (registered or [])
