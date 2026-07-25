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
  the store's ``get_user_version`` — one O(1) bump, no jti enumeration.
  Refresh tokens minted before the bump are rejected at rotation; already
  issued *access* tokens are not re-checked against the store (that is the
  point of stateless access tokens), so they remain valid until ``exp`` —
  keep access TTLs short.
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

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from django.conf import settings

DEFAULT_ACCESS_TTL = 900  # 15 minutes
DEFAULT_REFRESH_TTL = 86400 * 7  # 7 days

# Lifecycle claims minted by this module. User-supplied ``claims`` may not
# override them: ``ver`` would bypass global logout, ``oat`` the session cap,
# ``sub`` the token's identity, and so on.
RESERVED_CLAIMS = frozenset({"sub", "iat", "exp", "typ", "jti", "fam", "oat", "ver"})


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


def _check_reserved(claims: dict[str, Any] | None) -> None:
    if claims:
        reserved = RESERVED_CLAIMS.intersection(claims)
        if reserved:
            raise ValueError(f"claims must not override reserved lifecycle claims: {sorted(reserved)}")


def create_token_pair(
    user: Any,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    kid: str | None = None,
    access_ttl: int = DEFAULT_ACCESS_TTL,
    refresh_ttl: int = DEFAULT_REFRESH_TTL,
    claims: dict[str, Any] | None = None,
    method: str | None = None,
    version: int = 0,
    oat: int | None = None,
) -> TokenPair:
    """Mint a fresh access + refresh token pair.

    Args:
        user: A Django user instance, or a bare user id — issuance does not
            assume password login.
        secret: Signing key (default: Django ``SECRET_KEY``).
        algorithm: JWT algorithm (default ``HS256``).
        kid: Optional signing-key identifier added to both JWT headers. This
            lets verifiers select the matching public key during asymmetric
            signing-key rotation.
        access_ttl / refresh_ttl: Lifetimes in seconds.
        claims: Extra claims copied into *both* tokens (e.g. ``role``,
            ``tenant_id``). Overriding a reserved lifecycle claim
            (``sub``/``iat``/``exp``/``typ``/``jti``/``fam``/``oat``/``ver``)
            raises ``ValueError``.
        method: Optional RFC 8176 auth method recorded as ``amr`` at origin
            and preserved across rotations (e.g. ``"pwd"``, ``"otp"``,
            ``"oauth"``).
        version: The user's current token version (from
            ``store.get_user_version(user_id)``); embedded as ``ver`` so a
            later version bump invalidates this pair.
        oat: Origin auth time. Defaults to now; ``rotate_refresh_token``
            passes the original value through so the absolute session cap
            survives rotation.
    """
    _check_reserved(claims)
    now = int(time.time())
    user_id = _resolve_user_id(user)
    fam = str(uuid.uuid4())
    base: dict[str, Any] = {
        "sub": user_id,
        "iat": now,
        "oat": now if oat is None else int(oat),
        "ver": version,
    }
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
    headers = {"kid": kid} if kid is not None else None
    return TokenPair(
        access_token=jwt.encode(access_claims, key, algorithm=algorithm, headers=headers),
        refresh_token=jwt.encode(refresh_claims, key, algorithm=algorithm, headers=headers),
        access_claims=access_claims,
        refresh_claims=refresh_claims,
    )


async def rotate_refresh_token(
    refresh_claims: dict[str, Any],
    *,
    store: Any,
    secret: str | None = None,
    algorithm: str = "HS256",
    kid: str | None = None,
    access_ttl: int = DEFAULT_ACCESS_TTL,
    refresh_ttl: int = DEFAULT_REFRESH_TTL,
    rotate: bool = True,
    max_session_lifetime: int | None = None,
    leeway: int = 60,
    claims: dict[str, Any] | None = None,
) -> TokenPair:
    """Exchange a validated refresh token for a new token pair.

    ``kid`` optionally identifies the signing key in every newly issued JWT
    header, matching the parameter accepted by :func:`create_token_pair`.

    ``refresh_claims`` is the already cryptographically-validated claim dict
    from ``request["context"]["auth_claims"]`` — the Rust layer verified the
    signature, expiry, and ``typ:"refresh"`` before this runs. This function
    performs the *stateful* checks:

    1. ``jti`` must be present (refresh tokens without one cannot be revoked).
    2. The token, and its ``fam``, must not be revoked. Replaying a
       rotated-out token (``rotate=True``) is reuse: the whole family is
       revoked and the call fails.
    3. If ``max_session_lifetime`` is set, ``oat`` must be present (a token
       without one fails closed — it cannot prove its session age) and
       ``now - oat`` must be within the cap. The new access token's expiry
       is clamped to the session's remaining lifetime so it cannot outlive
       the cap.
    4. The token's ``ver`` must equal the store's current user version — a
       version bumped by ``bump_user_version`` ("log out everywhere")
       invalidates every earlier refresh token here.
    5. On success: issue a new pair (Mode A / ``rotate=True``) and revoke the
       old ``jti``, or issue only a new access token (Mode B /
       ``rotate=False``) leaving the refresh token in place.

    Only ``oat`` and ``amr`` are carried across the rotation automatically.
    Custom claims minted into the original pair (e.g. ``role``) are **not**
    copied from the old token — they may have gone stale since issuance —
    so re-derive and pass them via ``claims=`` on every rotation.

    Full rotation requires the store's atomic ``consume`` primitive. The
    replacement is minted only after the old token has been consumed; if a
    concurrent caller already consumed it, the family is burned and the
    replay is rejected. ``leeway`` must match the validating
    ``JWTAuthentication`` backend's clock-skew tolerance so a consumed token
    remains blocked for the entire period in which that backend accepts it.

    Raises ``TokenRotationError`` on any failure (with a generic message —
    callers should return a uniform 401 to avoid an oracle).
    """
    _check_reserved(claims)
    jti = refresh_claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise TokenRotationError("Refresh token missing jti")

    fam = refresh_claims.get("fam")
    if rotate and (not isinstance(fam, str) or not fam):
        raise TokenRotationError("Rotating refresh token missing family id")
    # A revoked family blocks both modes: family revocation is the "kill this
    # session" switch, and a reusable (Mode B) token must not outlive it.
    if isinstance(fam, str) and fam and await _is_family_revoked(store, fam):
        raise TokenRotationError("Refresh token family revoked")

    now = int(time.time())
    if leeway < 0:
        raise ValueError("leeway must be >= 0")

    # Mode B deliberately keeps the refresh token reusable, so it checks only
    # revocation state (jti here, family above). Mode A consumes atomically
    # below, immediately before minting, after all other validation has
    # succeeded.
    if not rotate and await store.is_revoked(jti):
        raise TokenRotationError("Refresh token revoked")

    user_id = refresh_claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise TokenRotationError("Refresh token missing subject")

    oat = refresh_claims.get("oat")
    parsed_oat: int | None = None
    if oat is not None:
        try:
            parsed_oat = int(oat)
        except (TypeError, ValueError) as exc:
            raise TokenRotationError("Refresh token has invalid origin auth time") from exc
        if parsed_oat > now:
            raise TokenRotationError("Refresh token origin auth time is in the future")

    effective_access_ttl = access_ttl
    if max_session_lifetime is not None:
        if max_session_lifetime < 0:
            raise ValueError("max_session_lifetime must be >= 0")
        if parsed_oat is None:
            # Fail closed: without an origin-auth time the cap cannot be
            # enforced, and re-minting with a fresh ``oat`` would silently
            # grant the session a brand-new full lifetime.
            raise TokenRotationError("Refresh token missing origin auth time")
        remaining_session = parsed_oat + max_session_lifetime - now
        if remaining_session <= 0:
            raise TokenRotationError("Session exceeded maximum lifetime")
        # Access tokens are never re-checked against the cap after issuance,
        # so clamp the new one to the session's remaining lifetime.
        effective_access_ttl = min(access_ttl, remaining_session)

    version = await _user_version(store, user_id)
    try:
        token_version = int(refresh_claims.get("ver", 0))
    except (TypeError, ValueError) as exc:
        raise TokenRotationError("Refresh token has invalid version") from exc

    # Require equality, not merely "not stale": a token from an ahead or
    # inconsistent version store must not survive later logout bumps.
    if token_version != version:
        raise TokenRotationError("Refresh token version does not match current version")

    token_exp: int | None = None
    if rotate:
        try:
            token_exp = int(refresh_claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenRotationError("Refresh token has invalid expiry") from exc

    # The Rust validator accepts tokens for `leeway` seconds after exp. Keep
    # the consumed marker for that same window or a near-expiry token could be
    # replayed after its marker disappeared but while signature validation
    # still accepts it.
    consume_exp = None if token_exp is None else max(token_exp, now + leeway)
    if rotate and not await _consume(store, jti, exp=consume_exp):
        # A revoked-but-presented token under rotation is a replay: burn the
        # family so the legitimate holder's chain dies too (reuse detection).
        if fam is not None:
            # The marker must outlive every descendant, not just this replayed
            # token: a descendant minted from a later rotation can expire up to
            # refresh_ttl from now, so retain the marker at least that long.
            family_exp = max(now + refresh_ttl, consume_exp or 0)
            await _revoke_family(store, fam, exp=family_exp)
        raise TokenRotationError("Refresh token revoked or already used")

    # Preserve immutable origin claims across the rotation. Custom claims
    # are deliberately NOT copied from the old token (see docstring).
    carried: dict[str, Any] = {}
    if "amr" in refresh_claims:
        carried["amr"] = refresh_claims["amr"]
    if claims:
        carried.update(claims)

    key = _secret(secret)
    headers = {"kid": kid} if kid is not None else None

    if not rotate:
        # Mode B: new access token only; refresh token stays valid.
        # Reserved claims come after `carried` so they always win.
        access_claims = {
            **carried,
            "sub": user_id,
            "iat": now,
            "exp": now + effective_access_ttl,
            "typ": "access",
            "ver": version,
        }
        if parsed_oat is not None:
            access_claims["oat"] = parsed_oat
        return TokenPair(
            access_token=jwt.encode(access_claims, key, algorithm=algorithm, headers=headers),
            refresh_token="",
            access_claims=access_claims,
            refresh_claims=refresh_claims,
        )

    # Mode A: the old JTI has already been consumed atomically. Mint a new
    # pair that remains in the same rotation family.
    pair = create_token_pair(
        user_id,
        secret=secret,
        algorithm=algorithm,
        kid=kid,
        access_ttl=effective_access_ttl,
        refresh_ttl=refresh_ttl,
        claims=carried or None,
        version=version,
        oat=parsed_oat,
    )
    if fam is not None:
        pair.refresh_claims["fam"] = fam
        pair.refresh_token = jwt.encode(pair.refresh_claims, key, algorithm=algorithm, headers=headers)
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
    except (AttributeError, NotImplementedError) as exc:
        raise TokenRotationError("Revocation store does not support refresh-token families") from exc


async def _consume(store: Any, jti: str, *, exp: int | None) -> bool:
    consume = getattr(store, "consume", None)
    if consume is None:
        raise TokenRotationError("Revocation store does not support atomic refresh rotation")
    try:
        return bool(await consume(jti, exp=exp))
    except NotImplementedError as exc:
        raise TokenRotationError("Revocation store does not support atomic refresh rotation") from exc


async def _revoke_family(store: Any, fam: str, *, exp: int | None) -> None:
    try:
        await store.revoke_family(fam, exp=exp)
    except (AttributeError, NotImplementedError) as exc:
        raise TokenRotationError("Revocation store does not support refresh-token families") from exc
