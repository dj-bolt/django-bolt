"""Ready-made cookie-based JWT lifecycle endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import msgspec
from django.contrib.auth import aauthenticate

from django_bolt.exceptions import HTTPException
from django_bolt.responses import JSON

from .backends import JWTAuthentication
from .guards import IsAuthenticated
from .tokens import TokenRotationError, create_token_pair, rotate_refresh_token, set_token_cookies


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
        prefix: str = "/auth",
        access_cookie: str = "access_token",
        refresh_cookie: str = "refresh_token",
        secure_cookies: bool = True,
        credential_validator: CredentialValidator | None = None,
    ) -> None:
        self.store = store
        self.secret = secret
        self.algorithm = algorithm
        self.kid = kid
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

    def register(self, api: Any) -> None:
        """Attach the four lifecycle routes to a :class:`BoltAPI`."""
        refresh_auth = JWTAuthentication(
            secret=self.secret,
            algorithms=[self.algorithm],
            cookie=self.refresh_cookie,
            token_type="refresh",
            require_jti=True,
        )

        @api.post(f"{self.prefix}/login", tags=["auth"], summary="Log in")
        async def login(request: Any, credentials: LoginCredentials):
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
            try:
                pair = await rotate_refresh_token(
                    request["context"]["auth_claims"],
                    store=self.store,
                    secret=self.secret,
                    algorithm=self.algorithm,
                    kid=self.kid,
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
            claims = request["context"]["auth_claims"]
            await self.store.revoke_family(claims["fam"], exp=claims.get("exp"))
            return self._clear_cookies(JSON({"ok": True}))

        @api.post(
            f"{self.prefix}/logout-all",
            auth=[refresh_auth],
            guards=[IsAuthenticated()],
            tags=["auth"],
            summary="Log out everywhere",
        )
        async def logout_all(request: Any):
            await self.store.bump_user_version(request["context"]["user_id"])
            return self._clear_cookies(JSON({"ok": True}))
