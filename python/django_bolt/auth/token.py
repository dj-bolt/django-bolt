"""
JWT Token handling for Django-Bolt.

Provides a Token dataclass for encoding/decoding JWTs. The actual validation
happens in Rust for performance, but this provides Python-side utilities for
token creation and inspection.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt


def _normalize_datetime(value: datetime) -> datetime:
    """
    Convert datetime to UTC and strip microseconds for consistent JWT claims.

    Args:
        value: A datetime instance

    Returns:
        A normalized datetime in UTC without microseconds
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.replace(microsecond=0, tzinfo=UTC)


@dataclass
class Token:
    """
    JWT Token data structure.

    This class represents a JWT token with standard claims (exp, sub, iat, etc.)
    and optional custom claims. The actual encoding/decoding happens in Rust
    for performance, but you can use this class to construct tokens in Python.

    Standard Claims:
        exp: Expiration time (required, must be in the future)
        sub: Subject - usually user ID (required)
        iat: Issued at time (auto-generated if not provided)
        iss: Issuer - identifies who issued the token
        aud: Audience - intended recipient(s)
        jti: JWT ID - unique identifier for this token

    Django-Bolt Custom Claims:
        is_staff: Whether user is staff
        is_superuser: Whether user is a superuser/admin
        permissions: List of permission strings

    Example:
        >>> token = Token(
        ...     sub="user123",
        ...     exp=datetime.now(UTC) + timedelta(hours=1),
        ...     is_staff=True,
        ...     permissions=["users.view", "users.edit"]
        ... )
        >>> encoded = token.encode(secret="my-secret", algorithm="HS256")
    """

    exp: datetime
    """Expiration time - when the token expires (required, must be future)"""

    sub: str
    """Subject - typically the user ID or identifier (required)"""

    iat: datetime = field(default_factory=lambda: _normalize_datetime(datetime.now(UTC)))
    """Issued at - timestamp when token was created"""

    iss: str | None = None
    """Issuer - who issued the token (e.g., "my-auth-service")"""

    aud: str | None = None
    """Audience - intended recipient (e.g., "my-api")"""

    jti: str | None = None
    """JWT ID - unique identifier for this token (useful for revocation)"""

    nbf: datetime | None = None
    """Not before - token is not valid before this time"""

    # Django-Bolt specific claims
    is_staff: bool | None = None
    """Whether the user is staff"""

    is_superuser: bool | None = None
    """Whether the user is a superuser/admin"""

    is_admin: bool | None = None
    """Alternative admin flag (checked along with is_superuser)"""

    permissions: list[str] | None = None
    """List of permission strings (e.g., ["users.view", "posts.create"])"""

    # Extra custom claims
    extras: dict[str, Any] = field(default_factory=dict)
    """Any additional custom claims not covered by standard fields"""

    # Internal flag to skip validation (used during decoding)
    _skip_validation: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self):
        """Validate token fields after initialization."""
        # Validate sub is non-empty
        if not self.sub or len(self.sub) < 1:
            raise ValueError("sub (subject) must be a non-empty string")

        # Normalize and validate exp
        if isinstance(self.exp, datetime):
            self.exp = _normalize_datetime(self.exp)
            if not self._skip_validation:
                now = _normalize_datetime(datetime.now(UTC))
                if self.exp.timestamp() <= now.timestamp():
                    raise ValueError("exp (expiration) must be in the future")
        else:
            raise ValueError("exp must be a datetime object")

        # Normalize iat
        if isinstance(self.iat, datetime):
            self.iat = _normalize_datetime(self.iat)
            if not self._skip_validation:
                now = _normalize_datetime(datetime.now(UTC))
                if self.iat.timestamp() > now.timestamp():
                    raise ValueError("iat (issued at) must be current or past time")

        # Normalize nbf if provided
        if self.nbf is not None and isinstance(self.nbf, datetime):
            self.nbf = _normalize_datetime(self.nbf)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert token to dictionary for encoding.

        Returns:
            Dictionary with all claims, converting datetimes to Unix timestamps
        """
        result = {}

        # Standard claims
        result["exp"] = int(self.exp.timestamp())
        result["sub"] = self.sub
        result["iat"] = int(self.iat.timestamp())

        if self.iss is not None:
            result["iss"] = self.iss
        if self.aud is not None:
            result["aud"] = self.aud
        if self.jti is not None:
            result["jti"] = self.jti
        if self.nbf is not None:
            result["nbf"] = int(self.nbf.timestamp())

        # Django-Bolt custom claims
        if self.is_staff is not None:
            result["is_staff"] = self.is_staff
        if self.is_superuser is not None:
            result["is_superuser"] = self.is_superuser
        if self.is_admin is not None:
            result["is_admin"] = self.is_admin
        if self.permissions is not None:
            result["permissions"] = self.permissions

        # Extra claims
        result.update(self.extras)

        return result

    def encode(
        self,
        secret: str,
        algorithm: str = "HS256",
        headers: dict[str, Any] | None = None,
    ) -> str:
        """
        Encode the token into a JWT string.

        Note: This uses Python's jwt library for convenience. For production
        token generation at scale, consider using Rust directly via PyO3.

        Args:
            secret: Secret key for signing
            algorithm: JWT algorithm (HS256, HS384, HS512, RS256, etc.)
            headers: Optional additional headers (e.g., {"kid": "key-id"})

        Returns:
            Encoded JWT string

        Raises:
            ValueError: If encoding fails
        """
        try:
            return jwt.encode(
                payload=self.to_dict(),
                key=secret,
                algorithm=algorithm,
                headers=headers,
            )
        except Exception as e:
            raise ValueError(f"Failed to encode token: {e}") from e

    @classmethod
    def decode(
        cls,
        encoded_token: str,
        secret: str,
        algorithm: str = "HS256",
        audience: str | None = None,
        issuer: str | None = None,
        verify_exp: bool = True,
        verify_nbf: bool = True,
    ) -> "Token":
        """
        Decode and validate a JWT token.

        Note: In production, JWT validation happens in Rust for performance.
        This method is provided for testing and Python-side token inspection.

        Args:
            encoded_token: The JWT string to decode
            secret: Secret key for validation
            algorithm: Expected algorithm
            audience: Expected audience (if any)
            issuer: Expected issuer (if any)
            verify_exp: Verify expiration time
            verify_nbf: Verify not-before time

        Returns:
            Decoded Token instance

        Raises:
            ValueError: If token is invalid or verification fails
        """
        try:
            options = {
                "verify_signature": True,
                "verify_exp": verify_exp,
                "verify_nbf": verify_nbf,
                "verify_aud": audience is not None,
                "verify_iss": issuer is not None,
            }

            payload = jwt.decode(
                jwt=encoded_token,
                key=secret,
                algorithms=[algorithm],
                audience=audience if audience else None,
                issuer=issuer if issuer else None,
                options=options,
            )

            # Convert timestamps back to datetime
            exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
            iat = datetime.fromtimestamp(payload.get("iat", payload["exp"] - 3600), tz=UTC)
            nbf = None
            if "nbf" in payload:
                nbf = datetime.fromtimestamp(payload["nbf"], tz=UTC)

            # Extract known fields
            known_fields = {
                "exp",
                "sub",
                "iat",
                "iss",
                "aud",
                "jti",
                "nbf",
                "is_staff",
                "is_superuser",
                "is_admin",
                "permissions",
            }
            extras = {k: v for k, v in payload.items() if k not in known_fields}

            return cls(
                exp=exp,
                sub=payload["sub"],
                iat=iat,
                iss=payload.get("iss"),
                aud=payload.get("aud"),
                jti=payload.get("jti"),
                nbf=nbf,
                is_staff=payload.get("is_staff"),
                is_superuser=payload.get("is_superuser"),
                is_admin=payload.get("is_admin"),
                permissions=payload.get("permissions"),
                extras=extras,
                _skip_validation=True,  # Skip validation when decoding
            )
        except Exception as e:
            raise ValueError(f"Failed to decode token: {e}") from e

    @classmethod
    def create(
        cls,
        sub: str,
        expires_delta: timedelta | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        is_staff: bool = False,
        is_admin: bool = False,
        permissions: list[str] | None = None,
        **extra_claims: Any,
    ) -> "Token":
        """
        Convenient factory method to create a token.

        Args:
            sub: Subject (user ID)
            expires_delta: How long until expiration (default: 1 hour)
            issuer: Token issuer
            audience: Token audience
            is_staff: Staff flag
            is_admin: Admin flag. An admin is also staff, so it sets ``is_staff`` too.
            permissions: List of permissions
            **extra_claims: Any additional claims

        Returns:
            New Token instance
        """
        if expires_delta is None:
            expires_delta = timedelta(hours=1)

        now = datetime.now(UTC)
        exp = now + expires_delta

        return cls(
            exp=exp,
            sub=sub,
            iat=now,
            iss=issuer,
            aud=audience,
            is_staff=is_staff or is_admin,  # An admin is also staff.
            is_admin=is_admin,
            permissions=permissions,
            extras=extra_claims,
        )
