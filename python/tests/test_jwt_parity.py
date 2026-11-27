"""Bolt's Rust JWT check against PyJWT, token by token.

Each case builds a token, sends it through `TestClient` to a route with
`JWTAuthentication`, and decodes it with `jwt.decode` under the same options
(see jwt_parity.py). Bolt and PyJWT must give the same answer. A case with a
`difference` is a wanted difference: Bolt refuses a token that PyJWT accepts,
for the reason that the case gives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient, WebSocketTestClient

from .jwt_parity import SECRET, bolt_accepts, make_token, parity_api, pyjwt_accepts

HS256 = '{"alg":"HS256"}'


@dataclass(frozen=True)
class Case:
    id: str
    # The claims for the time `now`: a dict, or raw JSON text.
    claims: Callable[[int], dict[str, Any] | str]
    header: dict[str, Any] | str = HS256
    backend: str = "plain"
    # Why Bolt refuses this token, which PyJWT accepts.
    difference: str | None = None


def live(**claims: Any) -> Callable[[int], dict[str, Any]]:
    """Claims with `sub` and an `exp` one hour from now, plus `claims`."""
    return lambda now: {"sub": "1", "exp": now + 3600, **claims}


def at(**offsets: Any) -> Callable[[int], dict[str, Any]]:
    """Claims with each time claim at `now` plus its offset."""
    return lambda now: {"sub": "1", "exp": now + 3600, **{name: now + offset for name, offset in offsets.items()}}


def raw(template: str) -> Callable[[int], str]:
    """Raw JSON claims. `{exp}` in the template is one hour from now."""
    return lambda now: template.replace("{exp}", str(now + 3600))


AGREE = [
    # J1: a NumericDate can have a fraction (RFC 7519, section 2).
    Case("j1-exp-with-a-fraction", at(exp=3600.5)),
    Case("j1-exp-in-exponent-form", raw('{"sub":"1","exp":{exp}e0}')),
    Case("j1-expired-exp-with-a-fraction", at(exp=-100.5)),
    Case("j1-nbf-with-a-fraction", at(nbf=-100.25)),
    Case("j1-iat-with-a-fraction", at(iat=-100.75)),
    Case("j1-huge-exp", live(exp=1e300)),
    # J2: a claim that Bolt does not check, in any JSON type.
    Case("j2-iss-number", live(iss=123)),
    Case("j2-iss-null", live(iss=None)),
    Case("j2-aud-number", live(aud=123)),
    Case("j2-aud-array-with-a-number", live(aud=["api", 1])),
    Case("j2-is-staff-number", live(is_staff=1)),
    Case("j2-is-superuser-string", live(is_superuser="true")),
    Case("j2-permissions-string", live(permissions="read")),
    Case("j2-permissions-with-a-number", live(permissions=["read", 1])),
    Case("j2-amr-string", live(amr="pwd")),
    Case("j2-fam-number", live(fam=7)),
    Case("j2-oat-fraction", live(oat=1.5)),
    Case("j2-ver-string", live(ver="1")),
    Case("j2-private-claim", live(org=[1, {"a": None}])),
    # J2: a claim that the backend checks still needs its type.
    Case("j2-iss-number-checked", live(iss=123), backend="issuer"),
    Case("j2-iss-null-checked", live(iss=None), backend="issuer"),
    Case("j2-iss-missing-checked", live(), backend="issuer"),
    Case("j2-iss-checked", live(iss="https://issuer.example"), backend="issuer"),
    Case("j2-aud-number-checked", live(aud=123), backend="audience"),
    Case("j2-aud-array-with-a-number-checked", live(aud=["api", 1]), backend="audience"),
    Case("j2-aud-null-checked", live(aud=None), backend="audience"),
    Case("j2-aud-empty-checked", live(aud=""), backend="audience"),
    Case("j2-aud-empty-array-checked", live(aud=[]), backend="audience"),
    Case("j2-aud-array-checked", live(aud=["web", "api"]), backend="audience"),
    Case(
        "j2-all-checked",
        lambda now: {"sub": "1", "exp": now + 3600, "iat": now + 30, "iss": "https://issuer.example", "aud": "api"},
        backend="all",
    ),
    # J4: JSON null is not a missing claim or header parameter.
    Case("j4-sub-null", live(sub=None)),
    Case("j4-sub-number", live(sub=42)),
    Case("j4-jti-null", live(jti=None)),
    Case("j4-jti-number", live(jti=5)),
    Case("j4-exp-null", live(exp=None)),
    Case("j4-nbf-null", live(nbf=None)),
    Case("j4-iat-null", live(iat=None)),
    Case("j4-kid-null", live(), header={"alg": "HS256", "kid": None}),
    Case("j4-kid-number", live(), header={"alg": "HS256", "kid": 5}),
    Case("j4-alg-null", live(), header={"alg": None}),
    # J5: a token issued after now plus the leeway.
    Case("j5-iat-in-an-hour", at(iat=3600), backend="leeway"),
    Case("j5-iat-in-30-seconds", at(iat=30)),
    Case("j5-iat-in-30-seconds-with-leeway", at(iat=30), backend="leeway"),
    # J6: Bolt supports no critical extension (RFC 7515, section 4.1.11).
    Case("j6-crit-unknown-extension", live(), header={"alg": "HS256", "crit": ["exp"], "exp": 1}),
    Case("j6-crit-empty", live(), header={"alg": "HS256", "crit": []}),
    Case("j6-crit-string", live(), header={"alg": "HS256", "crit": "exp"}),
    Case("j6-crit-number-item", live(), header={"alg": "HS256", "crit": [1]}),
    Case("j6-crit-null", live(), header={"alg": "HS256", "crit": None}),
    Case("j6-b64-false", live(), header={"alg": "HS256", "b64": False}),
    Case("j6-b64-false-critical", live(), header={"alg": "HS256", "b64": False, "crit": ["b64"]}),
    # The neighbours: alg, typ, the form of the token, exp and nbf.
    Case("alg-none", live(), header={"alg": "none"}),
    Case("alg-lowercase", live(), header={"alg": "hs256"}),
    Case("alg-not-allowed", live(), header={"alg": "HS384"}),
    Case("alg-missing", live(), header={"typ": "JWT"}),
    Case("header-typ-jwt", live(), header={"alg": "HS256", "typ": "JWT"}),
    Case("header-typ-null", live(), header={"alg": "HS256", "typ": None}),
    Case("header-typ-number", live(), header={"alg": "HS256", "typ": 5}),
    Case("header-array", live(), header='["HS256"]'),
    Case("claims-array", raw('["1",{exp}]')),
    Case("typ-access", live(typ="access")),
    Case("exp-missing", raw('{"sub":"1"}')),
    Case("exp-in-the-past", at(exp=-30)),
    Case("exp-in-the-past-with-leeway", at(exp=-30), backend="leeway"),
    Case("exp-boolean", live(exp=True)),
    Case("nbf-in-an-hour", at(nbf=3600), backend="leeway"),
    Case("nbf-in-30-seconds", at(nbf=30)),
    Case("nbf-in-30-seconds-with-leeway", at(nbf=30), backend="leeway"),
]

DIFFERENT = [
    Case(
        "duplicate-claim",
        raw('{"sub":"1","exp":{exp},"role":"a","role":"b"}'),
        difference="RFC 7519, section 4 allows a parser to refuse a duplicate name; PyJWT keeps the last",
    ),
    Case(
        "duplicate-registered-claim",
        raw('{"sub":"1","sub":"2","exp":{exp}}'),
        difference="RFC 7519, section 4 allows a parser to refuse a duplicate name; PyJWT keeps the last",
    ),
    Case(
        "duplicate-header-parameter",
        live(),
        header='{"alg":"HS256","kid":"a","kid":"a"}',
        difference="RFC 7515, section 4 allows a parser to refuse a duplicate name; PyJWT keeps the last",
    ),
    Case(
        "exp-numeric-string",
        lambda now: {"sub": "1", "exp": str(now + 3600)},
        difference="a NumericDate is a JSON number (RFC 7519, section 2); PyJWT's int() also reads a str",
    ),
    Case(
        "nbf-boolean",
        live(nbf=True),
        difference="a NumericDate is a JSON number (RFC 7519, section 2); PyJWT's int() also reads a bool",
    ),
    Case(
        "iat-numeric-string",
        live(iat="1"),
        difference="a NumericDate is a JSON number (RFC 7519, section 2); PyJWT's int() also reads a str",
    ),
    Case("typ-null", live(typ=None), difference="Bolt cannot read the type of the token, so it refuses it"),
    Case(
        "token-type-array",
        live(token_type=["refresh"]),
        difference="Bolt cannot read the type of the token, so it refuses it",
    ),
    Case("typ-refresh", live(typ="refresh"), difference="Bolt refuses a refresh or ID token on an access route"),
    Case(
        "crit-b64",
        live(),
        header={"alg": "HS256", "b64": True, "crit": ["b64"]},
        difference="Bolt supports no critical extension; PyJWT supports b64 (RFC 7797)",
    ),
    Case(
        "nan-in-a-private-claim",
        raw('{"sub":"1","exp":{exp},"x":NaN}'),
        difference="JSON has no NaN (RFC 8259, section 6); Python's json reads it",
    ),
    Case(
        "number-beyond-f64-in-a-private-claim",
        raw('{"sub":"1","exp":{exp},"x":1e400}'),
        difference="RFC 8259, section 6 allows a limit on the range of numbers; Python's json gives inf",
    ),
    Case(
        "deep-json-in-a-private-claim",
        raw('{"sub":"1","exp":{exp},"x":' + "[" * 200 + "]" * 200 + "}"),
        difference="RFC 8259, section 9 allows a limit on the depth; serde_json stops at 128",
    ),
    Case(
        "lone-surrogate-in-a-private-claim",
        raw('{"sub":"1","exp":{exp},"x":"\\ud800"}'),
        difference="a lone surrogate is not a Unicode string (RFC 8259, section 8.2)",
    ),
]


@pytest.fixture(scope="module")
def client():
    with TestClient(parity_api()) as test_client:
        yield test_client


@pytest.mark.parametrize("case", AGREE + DIFFERENT, ids=lambda case: case.id)
def test_bolt_and_pyjwt_give_the_same_answer(client, case):
    token = make_token(case.header, case.claims(int(time.time())))
    answers = {"bolt": bolt_accepts(client, case.backend, token), "pyjwt": pyjwt_accepts(case.backend, token)}
    if case.difference is None:
        assert answers["bolt"] == answers["pyjwt"], answers
    else:
        assert answers == {"bolt": False, "pyjwt": True}, case.difference


def _padded(part: str) -> str:
    return part + "=" * (-len(part) % 4)


def test_base64url_padding_is_refused(client):
    """RFC 7515, section 2 and RFC 7519, section 7.2: no padding. PyJWT accepts it for AWS ALB tokens."""
    now = int(time.time())
    header, claims, _ = make_token({"alg": "HS256", "x": 12}, {"sub": "1", "exp": now + 3600}).split(".")
    message = f"{_padded(header)}.{claims}"
    assert message.endswith("==." + claims)
    signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest()
    token = f"{message}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    assert (bolt_accepts(client, "plain", token), pyjwt_accepts("plain", token)) == (False, True)


def _signed(header: bytes, claims: bytes) -> str:
    def part(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    message = f"{part(header)}.{part(claims)}"
    return f"{message}.{part(hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest())}"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8-sig"])
@pytest.mark.parametrize("part", ["header", "claims"])
def test_json_that_is_not_utf8_is_refused(client, part, encoding):
    """RFC 7515, section 5.2 and RFC 7519, section 7.2: each part is UTF-8 JSON. Python's json reads the others."""
    texts = {"header": HS256, "claims": f'{{"sub":"1","exp":{int(time.time()) + 3600}}}'}
    token = _signed(*(text.encode(encoding if name == part else "utf-8") for name, text in texts.items()))
    assert (bolt_accepts(client, "plain", token), pyjwt_accepts("plain", token)) == (False, True)


def test_an_empty_jti_fails_require_jti():
    """An empty jti names no token. The `require` option of PyJWT checks only that the claim is present."""
    api = BoltAPI()

    @api.get("/jti", auth=[JWTAuthentication(secret=SECRET, require_jti=True)])
    async def jti(request) -> dict[str, bool]:
        return {"accepted": request.context is not None}

    now = int(time.time())
    with TestClient(api) as test_client:
        for claims, bolt in [({"jti": "j1"}, True), ({"jti": ""}, False)]:
            token = make_token(HS256, {"sub": "1", "exp": now + 3600, **claims})
            response = test_client.get("/jti", headers={"Authorization": f"Bearer {token}"})
            assert response.json() == {"accepted": bolt}, claims
            jwt.decode(token, SECRET, algorithms=["HS256"], options={"require": ["exp", "jti"]})


def test_four_parts_are_refused(client):
    token = make_token(HS256, {"sub": "1", "exp": int(time.time()) + 3600})
    header, claims, _ = token.split(".")
    message = f"{header}.{claims}.{claims}"
    signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest()
    four = f"{message}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    assert (bolt_accepts(client, "plain", four), pyjwt_accepts("plain", four)) == (False, False)


def test_auth_claims_give_a_time_claim_as_an_int_unless_it_has_a_fraction():
    api = BoltAPI()

    @api.get("/claims", auth=[JWTAuthentication(secret=SECRET)])
    async def claims(request) -> dict[str, Any]:
        claims = request.context["auth_claims"]
        return {"claims": claims, "types": {name: type(value).__name__ for name, value in claims.items()}}

    now = int(time.time())
    # `N.0` and `Ne0` are whole numbers, as `N` is. `json.loads` gives a float for them.
    token = make_token(HS256, f'{{"sub":"1","exp":{now + 3600}.0,"iat":{now}e0,"nbf":{now - 10}.5}}')
    with TestClient(api) as test_client:
        body = test_client.get("/claims", headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {
        "claims": {"sub": "1", "exp": now + 3600, "iat": now, "nbf": now - 9.5},
        "types": {"sub": "str", "exp": "int", "iat": "int", "nbf": "float"},
    }


def test_auth_claims_give_a_malformed_claim_as_none():
    """A claim of another JSON form has three states with the others: missing, valid and malformed."""
    api = BoltAPI()

    @api.get("/claims", auth=[JWTAuthentication(secret=SECRET)])
    async def claims(request) -> dict[str, Any]:
        return {"claims": request.context["auth_claims"], "is_staff": request.context["is_staff"]}

    now = int(time.time())
    token = make_token(HS256, {"sub": "1", "exp": now, "is_staff": 1, "iss": 5, "ver": "1", "amr": ["pwd", None]})
    with TestClient(api) as test_client:
        body = test_client.get("/claims", headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {
        "claims": {"sub": "1", "exp": now, "is_staff": None, "iss": None, "ver": None, "amr": None},
        "is_staff": False,
    }


@pytest.mark.parametrize("separator", [" ", "  ", "     "])
def test_one_or_more_spaces_end_the_scheme(client, separator):
    """RFC 9110, section 11.4: credentials = auth-scheme [ 1*SP ( token68 / #auth-param ) ]."""
    token = make_token(HS256, {"sub": "1", "exp": int(time.time()) + 3600})
    for scheme in ("Bearer", "bearer"):
        response = client.get("/jwt/plain", headers={"Authorization": f"{scheme}{separator}{token}"})
        assert response.json() == {"accepted": True}, repr(separator)


@pytest.mark.parametrize("separator", ["\t", " \t", " "])
def test_another_white_space_does_not_end_the_scheme(client, separator):
    token = make_token(HS256, {"sub": "1", "exp": int(time.time()) + 3600})
    value = f"Bearer{separator}{token}".encode()
    response = client.get("/jwt/plain", headers={"Authorization": value})
    assert response.json() == {"accepted": False}


@pytest.mark.asyncio
async def test_more_than_one_space_after_the_scheme_of_a_websocket_token():
    api = BoltAPI()

    @api.websocket("/ws", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    token = make_token(HS256, {"sub": "1", "exp": int(time.time()) + 3600})
    async with WebSocketTestClient(api, "/ws", headers={"authorization": f"Bearer   {token}"}) as websocket:
        assert await websocket.receive_text() == "connected"
    with pytest.raises(PermissionError, match="Authentication required"):
        async with WebSocketTestClient(api, "/ws", headers={"authorization": f"Bearer\t{token}"}):
            pass


def test_more_than_one_space_after_an_api_key_scheme():
    api = BoltAPI()

    @api.get("/key", auth=[APIKeyAuthentication(api_keys={"key-one"})])
    async def key(request) -> dict[str, bool]:
        return {"accepted": request.context is not None}

    with TestClient(api) as test_client:
        for value, accepted in [("Bearer  key-one", True), ("ApiKey   key-one", True), ("ApiKey\tkey-one", False)]:
            response = test_client.get("/key", headers={"X-API-Key": value})
            assert response.json() == {"accepted": accepted}, value
