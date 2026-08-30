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
from django_bolt.auth import backends as auth_backends
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

    def test_jwks_and_jwks_url_are_mutually_exclusive(self):
        with pytest.raises(ImproperlyConfigured, match="either 'jwks_url' or 'jwks'"):
            JWTAuthentication(
                jwks=JWKS,
                jwks_url="https://issuer.example/jwks.json",
                algorithms=["RS256"],
                issuer="https://issuer.example/",
                audience="api",
            )

    def test_empty_algorithm_allowlist_is_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="must not be empty"):
            JWTAuthentication(jwks=JWKS, algorithms=[])

    def test_empty_secret_is_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="secret must not be empty"):
            JWTAuthentication(secret="")

    def test_remote_jwks_requires_https(self):
        with pytest.raises(ImproperlyConfigured, match="HTTPS"):
            JWTAuthentication(
                jwks_url="http://issuer.example/jwks.json",
                algorithms=["RS256"],
                issuer="https://issuer.example/",
                audience="api",
            )

    @pytest.mark.parametrize("missing", ["issuer", "audience"])
    def test_remote_jwks_requires_issuer_and_audience(self, missing):
        kwargs = {
            "jwks_url": "https://issuer.example/jwks.json",
            "algorithms": ["RS256"],
            "issuer": "https://issuer.example/",
            "audience": "api",
        }
        kwargs[missing] = None
        with pytest.raises(ImproperlyConfigured, match="issuer.*audience"):
            JWTAuthentication(**kwargs)


class TestJwksUrlCaching:
    def test_unknown_kid_triggers_runtime_refresh(self, monkeypatch):
        documents = [{"keys": [KEY1_JWK]}, {"keys": [KEY1_JWK, KEY2_JWK]}]

        class FakeResponse:
            def __init__(self, document):
                self.document = document
                self.text = json.dumps(document)

            def raise_for_status(self):
                pass

            def json(self):
                return self.document

        def fake_get(url, timeout):
            return FakeResponse(documents.pop(0))

        monkeypatch.setattr(auth_backends.httpx, "get", fake_get)
        api = BoltAPI()

        @api.get(
            "/rotating",
            auth=[
                JWTAuthentication(
                    jwks_url="https://issuer.example/jwks.json",
                    algorithms=["RS256"],
                    issuer="https://issuer.example/",
                    audience="api",
                )
            ],
            guards=[IsAuthenticated()],
        )
        async def rotating():
            return {"ok": True}

        with TestClient(api) as client:
            token = make_token(KEY2_PRIV, "key-2", iss="https://issuer.example/", aud="api")
            assert client.get("/rotating", headers={"Authorization": f"Bearer {token}"}).status_code == 200

        assert documents == []

    def test_jwks_url_fetched_once_across_metadata_calls(self, monkeypatch):
        """A backend reused as a global default or across routes runs
        ``to_metadata()`` once per route; the fetched document must be cached
        on the instance, not re-fetched (and its failure delay re-paid) per
        route."""
        calls = []

        class FakeResponse:
            text = json.dumps(JWKS)

            def raise_for_status(self):
                pass

        def fake_get(url, timeout):
            calls.append(url)
            return FakeResponse()

        monkeypatch.setattr(auth_backends.httpx, "get", fake_get)

        auth = JWTAuthentication(
            jwks_url="https://issuer.example/jwks.json",
            algorithms=["RS256"],
            issuer="https://issuer.example/",
            audience="api",
        )
        first = auth.to_metadata()
        second = auth.to_metadata()

        assert first["jwks"] == second["jwks"] == json.dumps(JWKS)
        assert len(calls) == 1

    def test_remote_metadata_exposes_single_flight_refresh_callback(self, monkeypatch):
        responses = [JWKS, {"keys": [KEY2_JWK]}]

        class FakeResponse:
            def __init__(self, document):
                self.document = document
                self.text = json.dumps(document)

            def raise_for_status(self):
                pass

            def json(self):
                return self.document

        def next_response(_url, timeout):
            assert timeout == 10.0
            return FakeResponse(responses.pop(0))

        monkeypatch.setattr(auth_backends.httpx, "get", next_response)
        auth = JWTAuthentication(
            jwks_url="https://issuer.example/jwks.json",
            algorithms=["RS256"],
            issuer="https://issuer.example/",
            audience="api",
            jwks_refresh_interval=60,
        )

        metadata = auth.to_metadata()
        refreshed = metadata["jwks_refresh"]()

        assert json.loads(refreshed) == {"keys": [KEY2_JWK]}
        assert metadata["jwks_refresh_interval"] == 60

    def test_failed_refresh_keeps_cached_jwks(self, monkeypatch):
        class FakeResponse:
            text = json.dumps(JWKS)

            def raise_for_status(self):
                pass

        def initial_response(_url, timeout):
            assert timeout == 10.0
            return FakeResponse()

        monkeypatch.setattr(auth_backends.httpx, "get", initial_response)
        auth = JWTAuthentication(
            jwks_url="https://issuer.example/jwks.json",
            algorithms=["RS256"],
            issuer="https://issuer.example/",
            audience="api",
        )
        metadata = auth.to_metadata()

        def unavailable(url, timeout):
            raise auth_backends.httpx.ConnectError("offline")

        monkeypatch.setattr(auth_backends.httpx, "get", unavailable)
        assert metadata["jwks_refresh"]() is None
        assert auth._resolve_jwks() == json.dumps(JWKS)


class TestOidcDiscovery:
    def test_discovery_configures_issuer_jwks_and_algorithms(self, monkeypatch):
        calls = []

        class FakeResponse:
            def __init__(self, document):
                self.document = document
                self.text = json.dumps(document)

            def raise_for_status(self):
                pass

            def json(self):
                return self.document

        def fake_get(url, timeout):
            calls.append(url)
            if url.endswith("openid-configuration"):
                return FakeResponse(
                    {
                        "issuer": "https://issuer.example",
                        "jwks_uri": "https://issuer.example/keys",
                        "id_token_signing_alg_values_supported": ["RS256", "none"],
                    }
                )
            return FakeResponse(JWKS)

        monkeypatch.setattr(auth_backends.httpx, "get", fake_get)
        metadata = JWTAuthentication(
            oidc_issuer="https://issuer.example",
            audience="my-api",
        ).to_metadata()

        assert metadata["issuer"] == "https://issuer.example"
        assert metadata["algorithms"] == ["RS256"]
        assert calls == [
            "https://issuer.example/.well-known/openid-configuration",
            "https://issuer.example/keys",
        ]

    def test_discovery_requires_audience(self):
        with pytest.raises(ImproperlyConfigured, match="audience"):
            JWTAuthentication(oidc_issuer="https://issuer.example")
