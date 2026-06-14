"""Token minting + verification.

Access tokens are short-lived ``SECRET_KEY``-signed JWTs (reusing django-bolt's
``create_jwt_for_user``) so the existing Rust ``JWTAuthentication`` and per-tool guards
validate them with no extra code. Refresh tokens and authorization codes are opaque
high-entropy strings stored only as SHA-256 hashes (see ``store``/``models``).
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import jwt
from django.conf import settings

from django_bolt.auth.jwt_utils import create_jwt_for_user

from .config import AuthorizationServer


def _secret(server: AuthorizationServer) -> str:
    return server.jwt_secret or settings.SECRET_KEY


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    """A URL-safe, cryptographically random secret (~256 bits at the default size)."""
    return secrets.token_urlsafe(nbytes)


def mint_access_token(server: AuthorizationServer, user: Any, *, scope: str, client_id: str) -> str:
    """Sign a JWT access token for ``user`` carrying ``scope`` + identity claims."""
    extra: dict[str, Any] = {
        "scope": scope,
        "jti": secrets.token_urlsafe(16),
        "iss": server.effective_issuer(),
        "aud": server.effective_resource_url(),
    }
    hook_claims = server.get_extra_claims(user, scopes=scope.split(), client_id=client_id)
    if hook_claims:
        extra.update(hook_claims)
    return create_jwt_for_user(
        user,
        secret=_secret(server),
        algorithm=server.jwt_algorithm,
        expires_in=server.access_token_ttl,
        extra_claims=extra,
    )


def make_token_verifier(server: AuthorizationServer):
    """Build the ``/mcp`` bearer verifier: decode + validate signature/exp/aud/iss.

    Returns the claims dict on success or ``None`` (the transport then issues the 401
    challenge). Pure JWT validation — no DB hit, so ``/mcp`` stays fast.
    """
    secret = _secret(server)
    algorithms = [server.jwt_algorithm]
    audience = server.effective_resource_url()
    issuer = server.effective_issuer()

    def verify(token: str) -> dict | None:
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=algorithms,
                audience=audience,
                issuer=issuer,
            )
        except jwt.PyJWTError:
            return None

    return verify
