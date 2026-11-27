"""A claim that Bolt types, in another JSON form: each reader treats it as a failure.

A claim has three states: missing, valid and malformed. Bolt accepts a token whose
malformed claim nothing reads (audit finding J2). A guard that reads such a claim
refuses the request with its own 403, in each form, ``none_of`` too. A malformed
``sub``, ``jti``, ``typ``, ``exp``, ``nbf`` or ``iat`` fails the token (401).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from django_bolt import BoltAPI
from django_bolt.auth import JWTAuthentication, Requires
from django_bolt.testing import TestClient

from .jwt_parity import SECRET, make_token

FUTURE = 4_000_000_000

# Each claim that Bolt types: (a valid value, a scalar of another JSON type).
# `amr` and `permissions` are arrays of strings; the valid value is one item.
TYPED_CLAIMS: dict[str, tuple[Any, Any]] = {
    "sub": ("1", 1),
    "jti": ("j1", 1),
    "typ": ("access", 1),
    "exp": (FUTURE, str(FUTURE)),
    "nbf": (1, True),
    "iat": (1, "1"),
    "iss": ("https://issuer.example", 123),
    "aud": ("api", 123),
    "fam": ("f1", 7),
    "oat": (1, 1.5),
    "ver": (1, "1"),
    "amr": ("pwd", "pwd"),
    "is_staff": (True, 1),
    "is_superuser": (True, "true"),
    "is_admin": (True, "false"),
    "permissions": ("admin", "admin"),
}
TOKEN_CLAIMS = ("sub", "jti", "typ", "exp", "nbf", "iat")
LIST_CLAIMS = ("amr", "permissions")


def _other(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 2
    return "other"


def _guards(value: Any) -> dict[str, Callable[[str], Requires]]:
    """Each guard form, on a claim that the caller names later."""
    return {
        "presence": lambda claim: Requires(claim),
        "equality": lambda claim: Requires(claim, value),
        "any_of": lambda claim: Requires(claim, value, _other(value)),
        "all_of": lambda claim: Requires(claim, all_of=[value]),
        "none_of": lambda claim: Requires(claim, none_of=[value]),
    }


FORMS = tuple(_guards(None))


def _malformed(valid: Any, wrong_scalar: Any) -> dict[str, Any]:
    return {
        "wrong-scalar": wrong_scalar,
        "mixed-array": [valid, 1],
        "null": None,
        "object": {"x": valid},
    }


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()
    backend = JWTAuthentication(secret=SECRET)
    for claim, (valid, _) in TYPED_CLAIMS.items():
        for form, guard in _guards(valid).items():

            async def handler(request) -> dict[str, bool]:
                return {"ok": True}

            api.get(f"/{claim}/{form}", auth=[backend], guards=[guard(claim)])(handler)
    with TestClient(api) as test_client:
        yield test_client


def _statuses(client, claim: str, claims: dict[str, Any]) -> dict[str, int]:
    token = make_token({"alg": "HS256"}, {"sub": "1", "exp": FUTURE, **claims})
    headers = {"Authorization": f"Bearer {token}"}
    return {form: client.get(f"/{claim}/{form}", headers=headers).status_code for form in FORMS}


@pytest.mark.parametrize("kind", ["wrong-scalar", "mixed-array", "null", "object"])
@pytest.mark.parametrize("claim", list(TYPED_CLAIMS))
def test_a_malformed_claim_fails_each_guard_form(client, claim, kind):
    value = _malformed(*TYPED_CLAIMS[claim])[kind]
    status = 401 if claim in TOKEN_CLAIMS else 403
    assert _statuses(client, claim, {claim: value}) == dict.fromkeys(FORMS, status)


@pytest.mark.parametrize("claim", list(TYPED_CLAIMS))
def test_a_valid_claim_gives_the_usual_answers(client, claim):
    valid, _ = TYPED_CLAIMS[claim]
    exact = [valid] if claim in LIST_CLAIMS else valid
    expected = {**dict.fromkeys(FORMS, 200), "none_of": 403}
    assert _statuses(client, claim, {claim: exact}) == expected


@pytest.mark.parametrize("claim", [claim for claim in TYPED_CLAIMS if claim not in ("sub", "exp")])
def test_a_missing_claim_passes_only_none_of(client, claim):
    expected = {**dict.fromkeys(FORMS, 403), "none_of": 200}
    assert _statuses(client, claim, {}) == expected
