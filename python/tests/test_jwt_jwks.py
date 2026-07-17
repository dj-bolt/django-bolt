"""JWKS support: verify RS256 tokens against a key set selected by ``kid``.

Providers like Clerk, Auth0, and Okta publish a JWKS endpoint and rotate
signing keys, identifying each with a ``kid`` header on the token. These
tests supply the JWKS directly (``jwks=``) so no network is needed; the
``jwks_url`` fetch path shares the same Rust key-selection code.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured
from jwt.algorithms import RSAAlgorithm

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db


def _make_key(kid: str):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return private_key, jwk


KEY1_PRIV, KEY1_JWK = _make_key("key-1")
KEY2_PRIV, KEY2_JWK = _make_key("key-2")
JWKS = {"keys": [KEY1_JWK, KEY2_JWK]}


def make_token(private_key, kid: str, **claims):
    payload = {"sub": "jwks-user", "iat": int(time.time()), "exp": int(time.time()) + 3600}
    payload.update(claims)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get(
        "/jwks",
        auth=[JWTAuthentication(jwks=JWKS, algorithms=["RS256"])],
        guards=[IsAuthenticated()],
    )
    async def jwks_route(request: dict):
        return {"user_id": request["context"]["user_id"]}

    with TestClient(api) as c:
        yield c


def get(client, token):
    return client.get("/jwks", headers={"Authorization": f"Bearer {token}"})


class TestJwks:
    def test_token_signed_with_first_key_verifies(self, client):
        r = get(client, make_token(KEY1_PRIV, "key-1"))
        assert r.status_code == 200
        assert r.json()["user_id"] == "jwks-user"

    def test_token_signed_with_second_key_verifies(self, client):
        r = get(client, make_token(KEY2_PRIV, "key-2"))
        assert r.status_code == 200

    def test_unknown_kid_rejected(self, client):
        r = get(client, make_token(KEY1_PRIV, "key-unknown"))
        assert r.status_code == 401

    def test_wrong_key_for_kid_rejected(self, client):
        # Signed with key-2's private key but claiming kid key-1 → the key-1
        # public key can't verify it.
        r = get(client, make_token(KEY2_PRIV, "key-1"))
        assert r.status_code == 401

    def test_string_jwks_accepted(self):
        api = BoltAPI()

        @api.get(
            "/s",
            auth=[JWTAuthentication(jwks=json.dumps(JWKS), algorithms=["RS256"])],
            guards=[IsAuthenticated()],
        )
        async def route(request: dict):
            return {"ok": True}

        with TestClient(api) as c:
            r = c.get("/s", headers={"Authorization": f"Bearer {make_token(KEY1_PRIV, 'key-1')}"})
            assert r.status_code == 200

    def test_empty_jwks_fails_startup(self):
        api = BoltAPI()

        @api.get("/e", auth=[JWTAuthentication(jwks={"keys": []}, algorithms=["RS256"])], guards=[IsAuthenticated()])
        async def route():
            return {"ok": True}

        with pytest.raises(Exception, match="no usable keys"), TestClient(api):
            pass

    def test_jwks_with_static_key_rejected_at_construction(self):
        with pytest.raises(ImproperlyConfigured, match="JWKS"):
            JWTAuthentication(secret="x", jwks=JWKS, algorithms=["RS256"])
