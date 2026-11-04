"""Ready-made cookie-based JWT lifecycle endpoints."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import msgspec
from django.contrib.auth import aauthenticate

from django_bolt.exceptions import HTTPException
from django_bolt.responses import JSON

from .backends import JWTAuthentication
from .guards import IsAuthenticated
from .tokens import (
    DEFAULT_REFRESH_TTL,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
    set_token_cookies,
)


class LoginCredentials(msgspec.Struct):
    """Default request body accepted by :class:`JWTAuthViews`."""

    username: str
    password: str


CredentialValidator = Callable[[Any, LoginCredentials], Awaitable[Any | None]]


class JWTAuthViews:
    """Register conventional login, refresh, logout, and logout-all routes.

    Applications with different credential requirements can replace
    ``credential_validator`` while retaining consistent cookie, rotation,
    revocation, error, and OpenAPI behavior.
    """

    def __init__(
        self,
        *,
        store: Any,
        secret: str | None = None,
        algorithm: str = "HS256",
        kid: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        prefix: str = "/auth",
        access_cookie: str = "access_token",
        refresh_cookie: str = "refresh_token",
        secure_cookies: bool = True,
        credential_validator: CredentialValidator | None = None,
    ) -> None:
        """Configure JWT auth routes, token options, and cookie settings."""
        self.store = store
        self.secret = secret
        self.algorithm = algorithm
        self.kid = kid
        self.issuer = issuer
        self.audience = audience
        self.prefix = prefix.rstrip("/")
        self.access_cookie = access_cookie
        self.refresh_cookie = refresh_cookie
        self.secure_cookies = secure_cookies
        self.credential_validator = credential_validator or self._authenticate

    async def _authenticate(self, request: Any, credentials: LoginCredentials) -> Any | None:
        return await aauthenticate(request=request, username=credentials.username, password=credentials.password)

    def _clear_cookies(self, response: JSON) -> JSON:
        response.delete_cookie(self.access_cookie, path="/")
        response.delete_cookie(self.refresh_cookie, path=self.prefix)
        return response

    async def _require_active_refresh(self, request: Any) -> dict[str, Any]:
        """Reject inactive refresh tokens before changing logout state.

        Redis stores can read JTI, family and version in one primary MGET.
        Other stores retain the existing individual checks.
        """
        claims = request["context"]["auth_claims"]
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise HTTPException(status_code=401, detail="Invalid token")

        fam = claims.get("fam")
        if not isinstance(fam, str) or not fam:
            raise HTTPException(status_code=401, detail="Invalid token")

        version = claims.get("ver", 0)
        if type(version) is not int or version < 0:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_id = request["context"]["user_id"]
        batch_reader = getattr(self.store, "get_refresh_state", None)
        if batch_reader is not None:
            state = await batch_reader(jti, fam, user_id)
            if state is not None:
                jti_revoked, family_revoked, current_version = state
                if jti_revoked or family_revoked or version != current_version:
                    raise HTTPException(status_code=401, detail="Invalid token")
                return claims

        if await self.store.is_revoked(jti):
            raise HTTPException(status_code=401, detail="Invalid token")
        if fam is not None and await self.store.is_family_revoked(fam):
            raise HTTPException(status_code=401, detail="Invalid token")
        if version != await self.store.get_user_version(user_id):
            raise HTTPException(status_code=401, detail="Invalid token")
        return claims

    def register(self, api: Any) -> None:
        """Attach the four lifecycle routes to a :class:`BoltAPI`."""
        token_claims = {"iss": self.issuer, "aud": self.audience}
        token_claims = {key: value for key, value in token_claims.items() if value is not None}
        refresh_auth = JWTAuthentication(
            secret=self.secret,
            algorithms=[self.algorithm],
            issuer=self.issuer,
            audience=self.audience,
            cookie=self.refresh_cookie,
            token_type="refresh",
            require_jti=True,
        )

        @api.post(f"{self.prefix}/login", tags=["auth"], summary="Log in")
        async def login(request: Any, credentials: LoginCredentials):
            """Validate credentials and issue a new pair of session cookies."""
            user = await self.credential_validator(request, credentials)
            if user is None or not getattr(user, "is_active", True):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            user_id = str(user.pk)
            version = await self.store.get_user_version(user_id)
            pair = create_token_pair(
                user,
                secret=self.secret,
                algorithm=self.algorithm,
                kid=self.kid,
                version=version,
                method="pwd",
                claims=token_claims or None,
            )
            return set_token_cookies(
                JSON({"ok": True}),
                pair,
                access_cookie=self.access_cookie,
                refresh_cookie=self.refresh_cookie,
                refresh_path=self.prefix,
                secure=self.secure_cookies,
            )

        @api.post(
            f"{self.prefix}/refresh",
            auth=[refresh_auth],
            guards=[IsAuthenticated()],
            tags=["auth"],
            summary="Refresh tokens",
        )
        async def refresh(request: Any):
            """Rotate the active refresh token and replace the session cookies."""
            try:
                pair = await rotate_refresh_token(
                    request["context"]["auth_claims"],
                    store=self.store,
                    secret=self.secret,
                    algorithm=self.algorithm,
                    kid=self.kid,
                    claims=token_claims or None,
                )
            except TokenRotationError:
                raise HTTPException(status_code=401, detail="Invalid token") from None
            return set_token_cookies(
                JSON({"ok": True}),
                pair,
                access_cookie=self.access_cookie,
                refresh_cookie=self.refresh_cookie,
                refresh_path=self.prefix,
                secure=self.secure_cookies,
            )

        @api.post(
            f"{self.prefix}/logout",
            auth=[refresh_auth],
            guards=[IsAuthenticated()],
            tags=["auth"],
            summary="Log out",
        )
        async def logout(request: Any):
            """Revoke an active refresh family without accepting stale tokens."""
            claims = request["context"]["auth_claims"]
            jti = claims.get("jti")
            fam = claims.get("fam")
            version = claims.get("ver", 0)
            if (
                not isinstance(jti, str)
                or not jti
                or not isinstance(fam, str)
                or not fam
                or type(version) is not int
                or version < 0
            ):
                raise HTTPException(status_code=401, detail="Invalid token")
            try:
                token_exp = int(claims["exp"]) if claims.get("exp") is not None else 0
            except (TypeError, ValueError):
                raise HTTPException(status_code=401, detail="Invalid token") from None
            # Cover JWT leeway and descendants created by refresh requests in flight.
            expires_at = max(token_exp, int(time.time()) + DEFAULT_REFRESH_TTL) + refresh_auth.leeway

            atomic_logout = getattr(self.store, "revoke_family_if_active", None)
            if (
                atomic_logout is not None
                and getattr(self._require_active_refresh, "__func__", None) is JWTAuthViews._require_active_refresh
            ):
                result = await atomic_logout(jti, fam, request["context"]["user_id"], version, exp=expires_at)
                if result is not None:
                    if not result:
                        raise HTTPException(status_code=401, detail="Invalid token")
                    return self._clear_cookies(JSON({"ok": True}))

            # Custom stores and custom cache policies keep the original path.
            await self._require_active_refresh(request)
            await self.store.revoke_family(fam, exp=expires_at)
            return self._clear_cookies(JSON({"ok": True}))

        @api.post(
            f"{self.prefix}/logout-all",
            auth=[refresh_auth],
            guards=[IsAuthenticated()],
            tags=["auth"],
            summary="Log out everywhere",
        )
        async def logout_all(request: Any):
            """Advance the user version only for an active refresh token."""
            claims = await self._require_active_refresh(request)
            try:
                updated = await self.store.bump_user_version_if_current(
                    request["context"]["user_id"], claims.get("ver", 0)
                )
            except (AttributeError, NotImplementedError) as exc:
                raise HTTPException(status_code=503, detail="Atomic global logout unavailable") from exc
            if not updated:
                raise HTTPException(status_code=401, detail="Invalid token")
            return self._clear_cookies(JSON({"ok": True}))
