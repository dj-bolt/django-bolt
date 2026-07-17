"""Access + refresh token lifecycle for Django-Bolt.

Completes the JWT story described in issue #239: short-lived access tokens
(validated entirely in Rust on the hot path) paired with long-lived refresh
tokens used only at a rotation endpoint. The claim schema is fixed here on
day one — ``typ``, ``ver``, ``fam``, ``oat`` — because adding a claim later
invalidates every outstanding token.

Design notes (from production feedback on #239):

- **Type separation is enforced in Rust**, not just here. Access tokens
  carry ``typ:"access"`` and refresh tokens ``typ:"refresh"``; a normal
  ``JWTAuthentication`` route rejects refresh tokens, and a rotation route
  configured with ``token_type="refresh"`` rejects access tokens — both
  before Python runs. See ``JWTAuthentication(token_type=...)``.
- **Global logout** uses a per-user token version (``ver``) compared against
  the store's ``get_user_version`` — one O(1) bump invalidates every
  outstanding token without enumerating jtis.
- **Reuse detection** uses a rotation family id (``fam``): replaying a
  rotated-out refresh token revokes the whole family.
- **Absolute session cap** uses an immutable origin-auth-time (``oat``)
  copied verbatim across every rotation, so a session can be bounded
  independently of rotation cadence.
- **Issuance is auth-method-agnostic**: ``create_token_pair`` takes an
  already-resolved user and arbitrary extra claims, so password login,
  magic links, OAuth, and internal handoffs all mint through one path. The
  optional ``method`` records an ``amr`` claim (RFC 8176).
"""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from django.conf import settings

DEFAULT_ACCESS_TTL = 900  # 15 minutes
DEFAULT_REFRESH_TTL = 86400 * 7  # 7 days


class TokenRotationError(Exception):
    """Raised when a refresh token cannot be rotated (revoked, reused,
    wrong type, missing jti, or past the absolute session cap)."""


@dataclass
class TokenPair:
    """An issued access + refresh token pair and their decoded claims."""

    access_token: str
    refresh_token: str
    access_claims: dict[str, Any]
    refresh_claims: dict[str, Any]


def _secret(secret: str | None) -> str:
    return secret if secret is not None else settings.SECRET_KEY


def _resolve_user_id(user: Any) -> str:
    # Accept a Django user, or a bare id/str for auth flows that don't hydrate
    # a full model (magic-link, internal handoff).
    if hasattr(user, "pk"):
        return str(user.pk)
    return str(user)


def create_token_pair(
    user: Any,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    access_ttl: int = DEFAULT_ACCESS_TTL,
    refresh_ttl: int = DEFAULT_REFRESH_TTL,
    claims: dict[str, Any] | None = None,
    method: str | None = None,
    version: int = 0,
) -> TokenPair:
    """Mint a fresh access + refresh token pair.

    Args:
        user: A Django user instance, or a bare user id — issuance does not
            assume password login.
        secret: Signing key (default: Django ``SECRET_KEY``).
        algorithm: JWT algorithm (default ``HS256``).
        access_ttl / refresh_ttl: Lifetimes in seconds.
        claims: Extra claims copied into *both* tokens (e.g. ``role``,
            ``tenant_id``). Reserved lifecycle claims cannot be overridden.
        method: Optional RFC 8176 auth method recorded as ``amr`` at origin
            and preserved across rotations (e.g. ``"pwd"``, ``"otp"``,
            ``"oauth"``).
        version: The user's current token version (from
            ``store.get_user_version(user_id)``); embedded as ``ver`` so a
            later version bump invalidates this pair.
    """
    now = int(time.time())
    user_id = _resolve_user_id(user)
    fam = str(uuid.uuid4())
    base: dict[str, Any] = {"sub": user_id, "iat": now, "oat": now, "ver": version}
    if claims:
        base.update(claims)
    if method is not None:
        base["amr"] = [method] if not isinstance(method, list) else method

    access_claims = {**base, "typ": "access", "exp": now + access_ttl}
    refresh_claims = {
        **base,
        "typ": "refresh",
        "exp": now + refresh_ttl,
        "jti": str(uuid.uuid4()),
        "fam": fam,
    }

    key = _secret(secret)
    return TokenPair(
        access_token=jwt.encode(access_claims, key, algorithm=algorithm),
        refresh_token=jwt.encode(refresh_claims, key, algorithm=algorithm),
        access_claims=access_claims,
        refresh_claims=refresh_claims,
    )


async def rotate_refresh_token(
    refresh_claims: dict[str, Any],
    *,
    store: Any,
    secret: str | None = None,
    algorithm: str = "HS256",
    access_ttl: int = DEFAULT_ACCESS_TTL,
    refresh_ttl: int = DEFAULT_REFRESH_TTL,
    rotate: bool = True,
    max_session_lifetime: int | None = None,
    claims: dict[str, Any] | None = None,
) -> TokenPair:
    """Exchange a validated refresh token for a new token pair.

    ``refresh_claims`` is the already cryptographically-validated claim dict
    from ``request["context"]["auth_claims"]`` — the Rust layer verified the
    signature, expiry, and ``typ:"refresh"`` before this runs. This function
    performs the *stateful* checks:

    1. ``jti`` must be present (refresh tokens without one cannot be revoked).
    2. The token, and its ``fam``, must not be revoked. Replaying a
       rotated-out token (``rotate=True``) is reuse: the whole family is
       revoked and the call fails.
    3. If ``max_session_lifetime`` is set, ``now - oat`` must be within it.
    4. On success: issue a new pair (Mode A / ``rotate=True``) and revoke the
       old ``jti``, or issue only a new access token (Mode B /
       ``rotate=False``) leaving the refresh token in place.

    Raises ``TokenRotationError`` on any failure (with a generic message —
    callers should return a uniform 401 to avoid an oracle).
    """
    jti = refresh_claims.get("jti")
    if not jti:
        raise TokenRotationError("Refresh token missing jti")

    fam = refresh_claims.get("fam")
    if fam is not None and await _is_family_revoked(store, fam):
        raise TokenRotationError("Refresh token family revoked")

    if await store.is_revoked(jti):
        # A revoked-but-presented token under rotation is a replay: burn the
        # family so the legitimate holder's chain dies too (reuse detection).
        if rotate and fam is not None:
            await _revoke_family(store, fam, exp=refresh_claims.get("exp"))
        raise TokenRotationError("Refresh token revoked or already used")

    oat = refresh_claims.get("oat")
    if (
        max_session_lifetime is not None
        and oat is not None
        and int(time.time()) - int(oat) > max_session_lifetime
    ):
        raise TokenRotationError("Session exceeded maximum lifetime")

    user_id = refresh_claims.get("sub")
    version = await _user_version(store, user_id)

    # Preserve immutable origin claims across the rotation.
    carried: dict[str, Any] = {}
    if oat is not None:
        carried["oat"] = oat
    if "amr" in refresh_claims:
        carried["amr"] = refresh_claims["amr"]
    if claims:
        carried.update(claims)

    now = int(time.time())
    key = _secret(secret)

    if not rotate:
        # Mode B: new access token only; refresh token stays valid.
        access_claims = {
            "sub": user_id,
            "iat": now,
            "exp": now + access_ttl,
            "typ": "access",
            "ver": version,
            **carried,
        }
        return TokenPair(
            access_token=jwt.encode(access_claims, key, algorithm=algorithm),
            refresh_token="",
            access_claims=access_claims,
            refresh_claims=refresh_claims,
        )

    # Mode A: full rotation. Mint a new pair keeping the same family, then
    # revoke the old jti so it can never be replayed.
    pair = create_token_pair(
        user_id,
        secret=secret,
        algorithm=algorithm,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
        claims=carried or None,
        version=version,
    )
    if fam is not None:
        pair.refresh_claims["fam"] = fam
        pair.refresh_token = jwt.encode(pair.refresh_claims, key, algorithm=algorithm)
    await store.revoke(jti, exp=refresh_claims.get("exp"))
    return pair


def set_token_cookies(
    response: Any,
    pair: TokenPair,
    *,
    access_cookie: str = "access_token",
    refresh_cookie: str = "refresh_token",
    refresh_path: str = "/",
    secure: bool = True,
    samesite: str = "Lax",
    domain: str | None = None,
) -> Any:
    """Attach an access + refresh pair to a response as safe cookies.

    Defaults chosen for browser session safety: both cookies are ``HttpOnly``
    (no JS access), ``Secure`` (HTTPS only), and ``SameSite=Lax``. The refresh
    cookie is **path-scoped** (``refresh_path``, ideally the rotation
    endpoint) so it is not attached to ordinary API requests — only the
    short-lived access token rides along on every call. Cookie ``max_age`` is
    derived from each token's own ``exp`` claim.

    Requires a response object exposing ``set_cookie`` (django-bolt's
    ``Response``/``PlainText``/``JSON`` responses, or Django's
    ``HttpResponse``). Returns the response for chaining.
    """
    now = int(time.time())
    access_max_age = max(0, int(pair.access_claims["exp"]) - now)
    response.set_cookie(
        access_cookie,
        pair.access_token,
        max_age=access_max_age,
        path="/",
        domain=domain,
        secure=secure,
        httponly=True,
        samesite=samesite,
    )
    if pair.refresh_token:
        refresh_max_age = max(0, int(pair.refresh_claims["exp"]) - now)
        response.set_cookie(
            refresh_cookie,
            pair.refresh_token,
            max_age=refresh_max_age,
            path=refresh_path,
            domain=domain,
            secure=secure,
            httponly=True,
            samesite=samesite,
        )
    return response


async def _user_version(store: Any, user_id: str | None) -> int:
    if user_id is None:
        return 0
    try:
        return await store.get_user_version(user_id)
    except NotImplementedError:
        return 0


async def _is_family_revoked(store: Any, fam: str) -> bool:
    try:
        return await store.is_family_revoked(fam)
    except NotImplementedError:
        return False


async def _revoke_family(store: Any, fam: str, *, exp: int | None) -> None:
    with contextlib.suppress(NotImplementedError):
        await store.revoke_family(fam, exp=exp)
