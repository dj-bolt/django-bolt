"""Send one token to Bolt and to PyJWT, with the same options, and compare the answers.

Bolt's options map to the options of `jwt.decode` as follows:

- Bolt always requires `exp`: PyJWT gets `require=["exp"]`.
- Bolt checks `aud` only when the backend has an `audience`. PyJWT checks a token
  with an `aud` claim also with no audience, so it gets `verify_aud=False` then.
- `issuer` and `leeway` are the same in both.

The tokens are built from raw JSON text, so a test can send JSON that `jwt.encode`
cannot make, for example a name that occurs two times.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import jwt

from django_bolt import BoltAPI
from django_bolt.auth import JWTAuthentication

SECRET = "jwt-parity-secret-that-is-long-enough-for-hs256"

# Backend options: (audience, issuer, leeway).
BACKENDS: dict[str, tuple[str | None, str | None, int]] = {
    "plain": (None, None, 0),
    "leeway": (None, None, 60),
    "audience": ("api", None, 0),
    "issuer": (None, "https://issuer.example", 0),
    "all": ("api", "https://issuer.example", 60),
}

# Claims that name the type of a token. Bolt refuses a refresh or ID token on an
# access route, and a type claim that is not a string. PyJWT has no such check.
TOKEN_TYPE_CLAIMS = ("typ", "token_type", "token_use")
NON_ACCESS_TYPES = ("refresh", "offline", "id")
TIME_CLAIMS = ("exp", "nbf", "iat")
# The NumericDates that a Python datetime in UTC holds: 0001-01-01 to 9999-12-31T23:59:59.999999.
NUMERIC_DATE_START = -62135596800
NUMERIC_DATE_END = 253402300800


def parity_api() -> BoltAPI:
    """An API with one route for each backend. The route tells if Rust accepted the token."""
    api = BoltAPI()
    for name, (audience, issuer, leeway) in BACKENDS.items():
        backend = JWTAuthentication(secret=SECRET, audience=audience, issuer=issuer, leeway=leeway)

        async def accepted(request) -> dict[str, bool]:
            return {"accepted": request.context is not None}

        api.get(f"/jwt/{name}", auth=[backend])(accepted)
    return api


def _part(value: dict[str, Any] | str) -> str:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


def make_token(header: dict[str, Any] | str, claims: dict[str, Any] | str) -> str:
    """Sign the raw JSON of `header` and `claims` with HMAC-SHA256, whatever `alg` says."""
    message = f"{_part(header)}.{_part(claims)}"
    signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest()
    return f"{message}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def bolt_accepts(client, backend: str, token: str) -> bool:
    response = client.get(f"/jwt/{backend}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()["accepted"]


def pyjwt_accepts(backend: str, token: str) -> bool:
    audience, issuer, leeway = BACKENDS[backend]
    try:
        jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options={"require": ["exp"], "verify_aud": audience is not None},
        )
    except jwt.PyJWTError:
        return False
    return True


def known_difference(header: dict[str, Any], claims: dict[str, Any]) -> str | None:
    """The reason that Bolt refuses a token that PyJWT can accept, or None.

    Each reason is a wanted difference. The table in test_jwt_parity.py has a case for each.
    """
    for name in TIME_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str | bool):
            return "a NumericDate is a JSON number (RFC 7519, section 2); PyJWT's int() also reads a str or a bool"
        if isinstance(value, int | float) and not NUMERIC_DATE_START <= value < NUMERIC_DATE_END:
            return "a NumericDate must be a time that a Python datetime holds; PyJWT reads any number"
    for name in TOKEN_TYPE_CLAIMS:
        if name in claims:
            value = claims[name]
            if not isinstance(value, str):
                return "Bolt cannot read the type of the token, so it refuses it"
            if value.lower() in NON_ACCESS_TYPES:
                return "Bolt refuses a refresh or ID token on an access route"
    crit = header.get("crit")
    if (
        isinstance(crit, list)
        and crit
        and all(name == "b64" for name in crit)
        and header.get("b64", False) is not False
    ):
        return "Bolt supports no critical extension; PyJWT supports b64 (RFC 7797)"
    return None
