"""
Authentication system for Django-Bolt.

Provides DRF-inspired authentication classes that are compiled to Rust types
for zero-GIL performance in the hot path.

The authentication flow:
1. Python defines auth backends (JWT, API key)
2. Backends compile to metadata dicts via to_metadata()
3. Rust parses metadata at registration time
4. Rust validates tokens/keys without GIL on each request
5. AuthContext is populated and passed to Python handlers

Performance: ~60k+ RPS with JWT validation happening entirely in Rust.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import InterfaceError, OperationalError

from .pk_loader import load_user_by_pk_sync
from .revocation import create_revocation_handler

# (jti) -> True if the token is revoked, False otherwise.
RevokedTokenHandler = Callable[[str], Awaitable[bool]]


@dataclass
class AuthContext:
    """
    Authentication context returned by authentication backends.

    This is populated in Rust and passed to Python handlers via request.context.
    """

    user_id: str | None = None
    is_staff: bool = False
    is_superuser: bool = False
    backend: str = "none"
    claims: dict[str, Any] | None = None
    permissions: set[str] | None = None


class BaseAuthentication(ABC):
    """
    Base class for authentication backends.

    Authentication happens in Rust for performance. These classes compile
    their configuration into metadata that Rust uses to validate tokens/keys.
    """

    @property
    @abstractmethod
    def scheme_name(self) -> str:
        """Return the authentication scheme name (e.g., 'jwt', 'api_key')"""
        pass

    @abstractmethod
    def to_metadata(self) -> dict[str, Any]:
        """
        Compile this authentication backend into metadata for Rust.

        Returns a dict that will be parsed by Rust into typed enums.
        """
        pass

    async def get_user(self, user_id: str | None, auth_context: dict[str, Any]) -> Any | None:
        """
        Resolve a User instance from the authentication context.

        This method is called when request.user is awaited. Override this method
        to provide custom user resolution logic for your authentication backend.

        Args:
            user_id: The user identifier from the auth context
            auth_context: The full authentication context dict containing:
                - user_id: User identifier
                - is_staff: Whether user is staff
                - is_superuser: Whether user is a superuser
                - auth_backend: Backend name (jwt, api_key, session)
                - permissions: Set of permission strings
                - auth_claims: JWT claims dict (if JWT backend)

        Returns:
            User instance or None if user not found or backend doesn't support user loading
        """
        # Default: no user resolution
        return None


class JWTAuthentication(BaseAuthentication):
    """
    JWT token authentication.

    Validates JWT tokens using the configured secret and algorithms.
    Tokens should be provided in the Authorization header as "Bearer <token>",
    or in a cookie when `cookie` is set.

    Args:
        secret: HMAC secret for JWT validation. If None (and no public_key),
            uses Django's SECRET_KEY.
        algorithms: List of allowed JWT algorithms (default: ["HS256"]). The
            token's ``alg`` header must name one of these; all must share one
            key family.
        header: Header name to extract token from (default: "authorization")
        cookie: Optional cookie name to extract the token from. When set, the
            token is read from the named cookie only (the header is ignored).
            The cookie value is the raw token (no "Bearer " prefix needed).
        audience: Optional JWT audience claim to validate. Required with
            ``jwks_url`` to prevent cross-application token substitution.
        issuer: Optional JWT issuer claim to validate
        public_key: PEM-encoded public key for asymmetric algorithms
            (RS*/PS*/ES*/EdDSA). Preferred over passing a PEM via ``secret``
            (which also works, for compatibility). Mutually exclusive with
            ``secret``.
        leeway: Clock-skew tolerance in seconds applied to ``exp``/``nbf``
            validation (default: 60).
        token_type: Expected ``typ`` claim. When set (e.g. ``"refresh"`` for
            a token-rotation endpoint), tokens must carry exactly that
            ``typ``. When left as None (normal access routes), tokens
            carrying ``typ: "refresh"`` are rejected so refresh tokens can
            never authenticate a regular endpoint.
        csrf: When the token is read from a ``cookie``, enforce a cross-site
            origin check on unsafe (state-changing) HTTP methods, since bolt
            bypasses Django's ``CsrfViewMiddleware`` and cookies are attached
            automatically by the browser. Default True. The check applies
            only when that cookie backend supplies the accepted credential.
            Requests credentialed via another backend on the same route are
            exempt, even if a stale cookie is also attached. Ignored for
            header-sourced tokens (not auto-attached). Note that non-browser clients that
            send the cookie without any origin signal (``Sec-Fetch-Site``,
            ``Origin``, ``Referer``) are rejected with 403; have them send
            ``Sec-Fetch-Site: none``, use header tokens, or set False for
            API-only cookie deployments that provide their own protection.
        jwks_url: Absolute HTTPS URL of a JWKS endpoint (e.g. a provider's
            ``/.well-known/jwks.json``). The key set is parsed into per-``kid``
            decoding keys in Rust, refreshed periodically and immediately for
            an unknown ``kid``. Use with an asymmetric ``algorithms`` list
            (e.g. ``["RS256"]``), plus ``issuer`` and ``audience``.
        jwks: A JWKS document (dict or JSON string) supplied directly instead
            of fetching a ``jwks_url`` — useful for tests or air-gapped
            deployments. Mutually exclusive with ``secret``/``public_key``.
        oidc_issuer: HTTPS issuer URL used for OpenID Connect discovery. The
            discovery document supplies ``issuer``, ``jwks_uri``, and the
            signing algorithm allowlist. Requires ``audience``.
        jwks_refresh_interval: Seconds between runtime refresh attempts for a
            remote JWKS (default: 300). An unknown ``kid`` refreshes
            immediately; failed refreshes retain the last known-good keys.
    """

    # Class-level cached User model - resolved once on first use
    _user_model: type | None = None

    @classmethod
    def _get_user_model(cls) -> type:
        """Get User model, caching at class level after first call."""
        if cls._user_model is None:
            cls._user_model = get_user_model()
        return cls._user_model

    def __init__(
        self,
        secret: str | None = None,
        algorithms: list[str] | None = None,
        header: str = "authorization",
        cookie: str | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        revoked_token_handler: RevokedTokenHandler | None = None,
        revocation_store: Any | None = None,
        require_jti: bool = False,
        public_key: str | None = None,
        leeway: int = 60,
        token_type: str | None = None,
        csrf: bool = True,
        jwks_url: str | None = None,
        jwks: dict[str, Any] | str | None = None,
        oidc_issuer: str | None = None,
        jwks_refresh_interval: int = 300,
    ):
        provided_keys = [k for k in (secret, public_key) if k is not None]
        if len(provided_keys) > 1:
            raise ImproperlyConfigured(
                "JWTAuthentication accepts either 'secret' (HMAC) or 'public_key' (asymmetric), not both."
            )
        if oidc_issuer is not None and (jwks_url is not None or jwks is not None or issuer is not None):
            raise ImproperlyConfigured(
                "JWTAuthentication oidc_issuer is mutually exclusive with 'issuer', 'jwks_url', and 'jwks'."
            )
        if jwks_url is not None and jwks is not None:
            raise ImproperlyConfigured("JWTAuthentication accepts either 'jwks_url' or 'jwks', not both.")
        if (jwks_url is not None or jwks is not None) and provided_keys:
            raise ImproperlyConfigured(
                "JWTAuthentication accepts JWKS (jwks_url/jwks) or a static key (secret/public_key), not both."
            )
        if jwks_refresh_interval <= 0:
            raise ImproperlyConfigured("JWTAuthentication jwks_refresh_interval must be > 0 seconds.")
        if oidc_issuer is not None:
            parsed_oidc_issuer = urlsplit(oidc_issuer)
            if parsed_oidc_issuer.scheme.lower() != "https" or not parsed_oidc_issuer.hostname:
                raise ImproperlyConfigured("JWTAuthentication oidc_issuer must be an absolute HTTPS URL.")
            if audience is None:
                raise ImproperlyConfigured("JWTAuthentication with oidc_issuer requires 'audience'.")
            discovery_url = f"{oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
            discovery_response = httpx.get(discovery_url, timeout=10.0)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            discovered_issuer = discovery.get("issuer")
            discovered_jwks_url = discovery.get("jwks_uri")
            discovered_algorithms = discovery.get("id_token_signing_alg_values_supported")
            if not isinstance(discovered_issuer, str) or not isinstance(discovered_jwks_url, str):
                raise ImproperlyConfigured("OIDC discovery document must contain string 'issuer' and 'jwks_uri'.")
            if discovered_issuer.rstrip("/") != oidc_issuer.rstrip("/"):
                raise ImproperlyConfigured("OIDC discovery issuer does not match oidc_issuer.")
            issuer = discovered_issuer
            jwks_url = discovered_jwks_url
            if algorithms is None and isinstance(discovered_algorithms, list):
                algorithms = [value for value in discovered_algorithms if isinstance(value, str) and value != "none"]
        if jwks_url is not None:
            parsed_jwks_url = urlsplit(jwks_url)
            if parsed_jwks_url.scheme.lower() != "https" or not parsed_jwks_url.hostname:
                raise ImproperlyConfigured("JWTAuthentication jwks_url must be an absolute HTTPS URL.")
            if audience is None or issuer is None:
                raise ImproperlyConfigured(
                    "JWTAuthentication with jwks_url requires both 'issuer' and 'audience' "
                    "to prevent cross-application token substitution."
                )
        if leeway < 0:
            raise ImproperlyConfigured("JWTAuthentication leeway must be >= 0 seconds.")
        self.secret = secret
        self.public_key = public_key
        self.algorithms = ["HS256"] if algorithms is None else algorithms
        if not self.algorithms:
            raise ImproperlyConfigured("JWTAuthentication algorithms must not be empty.")
        self.header = header
        self.cookie = cookie
        self.audience = audience
        self.issuer = issuer
        self.leeway = leeway
        self.token_type = token_type
        self.csrf = csrf
        self.jwks_url = jwks_url
        self._jwks = jwks
        self.oidc_issuer = oidc_issuer
        self.jwks_refresh_interval = jwks_refresh_interval
        self._jwks_refresh_lock = threading.Lock()
        self._jwks_refreshed_at = 0.0

        # If no key material provided at all (and no JWKS), fall back to
        # Django's SECRET_KEY.
        if self.secret is None and self.public_key is None and jwks_url is None and jwks is None:
            try:
                if not hasattr(settings, "SECRET_KEY"):
                    raise ImproperlyConfigured(
                        "JWTAuthentication requires a 'secret' parameter or Django's SECRET_KEY setting. "
                        "Neither was provided."
                    )

                self.secret = settings.SECRET_KEY

                if not self.secret or self.secret == "":
                    raise ImproperlyConfigured(
                        "JWTAuthentication secret cannot be empty. "
                        "Please provide a non-empty 'secret' parameter or set Django's SECRET_KEY."
                    )
            except ImportError as e:
                raise ImproperlyConfigured(
                    "JWTAuthentication requires Django to be installed and configured, "
                    "or a 'secret' parameter must be explicitly provided."
                ) from e

        # Revocation support (OPTIONAL - only checked if provided)
        self.revoked_token_handler = revoked_token_handler
        self.revocation_store = revocation_store

        # Auto-enable require_jti if revocation is configured
        if (revoked_token_handler or revocation_store) and not require_jti:
            require_jti = True
        self.require_jti = require_jti

        # If revocation_store provided, create handler from it
        if revocation_store and not revoked_token_handler:
            self.revoked_token_handler = create_revocation_handler(revocation_store)

    @property
    def scheme_name(self) -> str:
        return "jwt"

    def _resolve_jwks(self) -> str | None:
        """Return the JWKS document as a JSON string, or None.

        A ``jwks`` dict/string is used verbatim; a ``jwks_url`` is fetched
        here when ``to_metadata`` first runs and cached on the instance. The
        resulting key set is parsed into per-``kid`` decoding keys in Rust;
        remote sets are subsequently refreshed through ``_refresh_jwks``.
        """
        if self._jwks is not None:
            return self._jwks if isinstance(self._jwks, str) else json.dumps(self._jwks)
        if self.jwks_url is not None:
            response = httpx.get(self.jwks_url, timeout=10.0)
            response.raise_for_status()
            self._jwks = response.text
            self._jwks_refreshed_at = time.monotonic()
            return self._jwks
        return None

    def _refresh_jwks(self) -> str | None:
        """Fetch and atomically replace remote JWKS, retaining stale keys on failure."""
        if self.jwks_url is None:
            return None
        if not self._jwks_refresh_lock.acquire(blocking=False):
            return None
        try:
            response = httpx.get(self.jwks_url, timeout=10.0)
            response.raise_for_status()
            # Validate the shape before replacing a known-good cached value.
            document = response.json()
            if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                raise ValueError("JWKS response must contain a keys array")
            self._jwks = response.text
            self._jwks_refreshed_at = time.monotonic()
            return self._jwks
        except (httpx.HTTPError, ValueError):
            return None
        finally:
            self._jwks_refresh_lock.release()

    def to_metadata(self) -> dict[str, Any]:
        metadata = {
            "type": "jwt",
            # Key material: the HMAC secret, or the PEM public key for
            # asymmetric algorithms. Rust builds the decoding key from this
            # once at registration, branching on the algorithm family.
            "secret": self.public_key if self.public_key is not None else self.secret,
            "algorithms": self.algorithms,
            "header": self.header.lower(),
            "cookie": self.cookie,
            "audience": self.audience,
            "issuer": self.issuer,
            "leeway": self.leeway,
            "token_type": self.token_type,
            # CSRF is only meaningful for cookie-sourced tokens (header tokens
            # are not auto-attached by the browser). Rust checks the flag only
            # when `cookie` is set.
            "cookie_csrf": self.csrf,
            "require_jti": self.require_jti,
        }

        # JWKS (fetched from jwks_url or supplied directly) takes precedence
        # over a static secret; Rust builds a kid -> key map from it.
        jwks = self._resolve_jwks()
        if jwks is not None:
            metadata["jwks"] = jwks
            if self.jwks_url is not None:
                metadata["jwks_refresh"] = self._refresh_jwks
                metadata["jwks_refresh_interval"] = self.jwks_refresh_interval

        # Add revocation handler reference (will be called from Rust if present)
        if self.revoked_token_handler:
            metadata["has_revocation_handler"] = True

        return metadata

    async def get_user(self, user_id: str | None, auth_context: dict[str, Any]) -> Any | None:
        """
        Load user from database using the user_id from JWT token.

        The user_id should be the primary key of the user in the database.

        Runs the pre-compiled pk query off the event loop. Deliberately NOT
        ``User.objects.aget``: asgiref's thread_sensitive executor is a single
        shared thread per process, which serializes every user load in the
        worker (measured ~3k req/s ceiling); the default executor runs loads
        concurrently and the compiled query skips per-call SQL compilation.
        """
        if not user_id:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_user_sync, user_id)

    def get_user_sync(self, user_id: str | None) -> Any | None:
        """
        Synchronously load user from database using the user_id from JWT token.

        This method does the actual DB query — a pk SELECT compiled once per
        (user model, database alias), not recompiled per request. Thread pool
        wrapping is handled by the loader resolved in
        user_loader.resolve_user_loader() based on the handler's execution
        context.
        """
        if not user_id:
            return None

        User = self._get_user_model()

        try:
            return load_user_by_pk_sync(User, user_id)
        except (OperationalError, InterfaceError):
            raise
        except Exception as e:
            print(f"Error loading user {user_id} in JWTAuthentication: {type(e).__name__}: {e}", file=sys.stderr)
            return None


class APIKeyAuthentication(BaseAuthentication):
    """
    API key authentication.

    Validates API keys against a configured set of valid keys.
    Keys should be provided in the configured header (default: X-API-Key).

    Args:
        api_keys: Set of valid API keys
        header: Header name to extract API key from (default: "x-api-key")
        key_permissions: Optional mapping of API keys to permission sets
    """

    def __init__(
        self,
        api_keys: set[str] | None = None,
        header: str = "x-api-key",
        key_permissions: dict[str, set[str]] | None = None,
    ):
        self.api_keys = api_keys or set()
        self.header = header
        self.key_permissions = key_permissions or {}

    @property
    def scheme_name(self) -> str:
        return "api_key"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "type": "api_key",
            "api_keys": list(self.api_keys),
            "header": self.header.lower(),
            "key_permissions": {k: list(v) for k, v in self.key_permissions.items()},
        }


def get_default_authentication_classes() -> list[BaseAuthentication]:
    """
    Get default authentication classes from Django settings.

    Looks for BOLT_AUTHENTICATION_CLASSES in settings. If not found,
    returns an empty list (no authentication by default).
    """
    try:
        try:
            if hasattr(settings, "BOLT_AUTHENTICATION_CLASSES"):
                return settings.BOLT_AUTHENTICATION_CLASSES
        except ImproperlyConfigured:
            # Settings not configured, return empty list
            pass
    except (ImportError, AttributeError):
        pass

    return []
