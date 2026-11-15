"""Ready-made cookie-based JWT lifecycle endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import msgspec
from django.contrib.auth import aauthenticate
from django.core.exceptions import ImproperlyConfigured

from django_bolt.exceptions import HTTPException
from django_bolt.responses import JSON

from .backends import JWTAuthentication, www_authenticate_challenge
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

    Args:
        secret: The key that signs the tokens. The default is Django's
            ``SECRET_KEY``. With an asymmetric ``algorithm``, this is the PEM
            private key.
        algorithm: The JWT algorithm (default ``HS256``).
        public_key: The PEM public key that verifies the tokens of an
            asymmetric ``algorithm``. ``secret`` is then the private key that
            signs them. Do not set it with an HMAC algorithm (``HS*``).
        kid: An optional key identifier for the header of each token.

    Raises:
        ImproperlyConfigured: The keys do not match ``algorithm``.
    """

    def __init__(
        self,
        *,
        store: Any,
        secret: str | None = None,
        algorithm: str = "HS256",
        public_key: str | None = None,
        kid: str | None = None,
        prefix: str = "/auth",
        access_cookie: str = "access_token",
        refresh_cookie: str = "refresh_token",
        secure_cookies: bool = True,
        credential_validator: CredentialValidator | None = None,
    ) -> None:
        if algorithm.startswith("HS"):
            if public_key is not None:
                raise ImproperlyConfigured(
                    "JWTAuthViews accepts 'public_key' only with an asymmetric algorithm. "
                    f"{algorithm!r} signs and verifies with 'secret'. "
                    "Remove 'public_key', or set 'algorithm' to an asymmetric algorithm such as 'RS256'."
                )
        elif secret is None or public_key is None:
            raise ImproperlyConfigured(
                f"JWTAuthViews with the asymmetric algorithm {algorithm!r} requires 'secret' and 'public_key'. "
                "Pass the PEM private key as 'secret' and the PEM public key as 'public_key'."
            )
        self.store = store
        self.secret = secret
        self.algorithm = algorithm
        self.public_key = public_key
        self.kid = kid
        self.prefix = prefix.rstrip("/")
        # The path of the refresh cookie. Routes at the root have an empty prefix.
        self._refresh_path = self.prefix or "/"
        self.access_cookie = access_cookie
        self.refresh_cookie = refresh_cookie
        self.secure_cookies = secure_cookies
        self.credential_validator = credential_validator or self._authenticate

    async def _authenticate(self, request: Any, credentials: LoginCredentials) -> Any | None:
        return await aauthenticate(request=request, username=credentials.username, password=credentials.password)

    def _clear_cookies(self, response: JSON) -> JSON:
        response.delete_cookie(self.access_cookie, path="/")
        response.delete_cookie(self.refresh_cookie, path=self._refresh_path)
        return response

    async def _require_live_refresh(self, request: Any) -> dict[str, Any]:
        """Reject a refresh token that ``/refresh`` also rejects.

        A used, revoked or old-version token must not end the sessions of live tokens.
        The refresh route cannot rotate a token with no family, so this token gets the same 401.
        """
        claims = request["context"]["auth_claims"]
        # The Rust layer gives ``fam`` as a str and ``ver`` as an int, or leaves the claim out.
        fam = claims.get("fam")
        if (
            not fam
            or await self.store.is_revoked(claims["jti"])
            or await self.store.is_family_revoked(fam)
            or claims.get("ver", 0) != await self.store.get_user_version(request["context"]["user_id"])
        ):
            raise HTTPException(status_code=401, detail="Invalid token")
        return claims

    def register(self, api: Any) -> None:
        """Attach the four lifecycle routes to a :class:`BoltAPI`."""
        # JWTAuthentication accepts one key: the public key of an asymmetric algorithm, or the HMAC secret.
        verification_key = {"secret": self.secret} if self.public_key is None else {"public_key": self.public_key}
        refresh_auth = JWTAuthentication(
            **verification_key,
            algorithms=[self.algorithm],
            cookie=self.refresh_cookie,
            token_type="refresh",
            require_jti=True,
        )

        # The login route has no auth backend, so Rust has no challenge for its 401.
        # The 401 names the scheme of the tokens that the route gives (RFC 9110 section 15.5.2).
        login_challenge = {"WWW-Authenticate": www_authenticate_challenge([refresh_auth])}

        # A custom credential_validator can return a user that is not a row of
        # the Django user model. Bolt cannot load that user from the token.
        check_user = self.credential_validator == self._authenticate

        @api.post(f"{self.prefix}/login", tags=["auth"], summary="Log in")
        async def login(request: Any, credentials: LoginCredentials):
            user = await self.credential_validator(request, credentials)
            if user is None or not getattr(user, "is_active", True):
                raise HTTPException(status_code=401, detail="Invalid credentials", headers=login_challenge)
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
                refresh_path=self._refresh_path,
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
            # The default login accepts only an active Django user. Thus, refresh
            # gives no new tokens when that user is inactive or has no row.
            if check_user and await request.auser() is None:
                raise HTTPException(status_code=401, detail="Invalid token")
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
                refresh_path=self._refresh_path,
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
            claims = await self._require_live_refresh(request)
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
            await self._require_live_refresh(request)
            await self.store.bump_user_version(request["context"]["user_id"])
            return self._clear_cookies(JSON({"ok": True}))
