"""
Integration tests for the Rust JWT validation loop.

Covers behavior that real-world identity providers depend on:
- Asymmetric algorithms (RS256/ES256) verified against a PEM public key
  passed as ``secret`` (issue #261).
- Non-string JOSE header extension parameters, which RFC 7515 §4.1.11
  permits and providers emit (Clerk: ``"oiat": <int>``, Auth0:
  ``"gty": [...]``). These previously failed before signature
  verification even ran.
- ``aud`` as a string or an array, and ``aud``-bearing tokens not being
  rejected when no audience is configured.
- The full configured algorithm list being honored (not just the first).
- Algorithm-confusion attacks (HS token against an RS-configured route).
- Invalid auth configuration failing at startup instead of silently
  serving unauthenticated routes.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient

HS_SECRET = "asymmetric-test-secret"


def _pem_pair(private_key):
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _pem_pair(rsa.generate_private_key(public_exponent=65537, key_size=2048))
EC_PRIVATE_PEM, EC_PUBLIC_PEM = _pem_pair(ec.generate_private_key(ec.SECP256R1()))


def make_token(algorithm, key, *, headers=None, **claims):
    payload = {"sub": "user123", "exp": int(time.time()) + 3600, "iat": int(time.time())}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    def add_route(path, auth_backend):
        @api.get(path, auth=[auth_backend], guards=[IsAuthenticated()])
        async def endpoint(request: dict):
            context = request["context"]
            return {"user_id": context["user_id"], "claims": context.get("auth_claims", {})}

    add_route("/hs256", JWTAuthentication(secret=HS_SECRET, algorithms=["HS256"]))
    add_route("/hs-multi", JWTAuthentication(secret=HS_SECRET, algorithms=["HS256", "HS384"]))
    add_route("/hs256-aud", JWTAuthentication(secret=HS_SECRET, algorithms=["HS256"], audience="api"))
    add_route("/rs256", JWTAuthentication(secret=RSA_PUBLIC_PEM, algorithms=["RS256"]))
    add_route("/es256", JWTAuthentication(secret=EC_PUBLIC_PEM, algorithms=["ES256"]))

    with TestClient(api) as test_client:
        yield test_client


def get(client, path, token):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


class TestNonStringHeaderExtras:
    """RFC 7515 §4.1.11: header extension params may be any JSON type."""

    def test_integer_header_extra_hs256(self, client):
        # Clerk-style numeric header param.
        token = make_token("HS256", HS_SECRET, headers={"oiat": 1784025590})
        response = get(client, "/hs256", token)
        assert response.status_code == 200
        assert response.json()["user_id"] == "user123"

    def test_array_header_extra_hs256(self, client):
        # Auth0-style array header param.
        token = make_token("HS256", HS_SECRET, headers={"gty": ["authorization_code", "refresh_token"]})
        response = get(client, "/hs256", token)
        assert response.status_code == 200

    def test_integer_header_extra_rs256(self, client):
        # The full Clerk repro: RS256 + numeric header extension param.
        token = make_token("RS256", RSA_PRIVATE_PEM, headers={"oiat": 1784025590})
        response = get(client, "/rs256", token)
        assert response.status_code == 200
        assert response.json()["user_id"] == "user123"


class TestAsymmetricAlgorithms:
    def test_rs256_valid_token(self, client):
        # Issue #261 repro: PEM public key as `secret`, RS256 token.
        token = make_token("RS256", RSA_PRIVATE_PEM)
        response = get(client, "/rs256", token)
        assert response.status_code == 200
        assert response.json()["user_id"] == "user123"

    def test_rs256_tampered_token(self, client):
        token = make_token("RS256", RSA_PRIVATE_PEM)
        header, payload, signature = token.split(".")
        tampered_signature = signature[:-2] + ("AA" if not signature.endswith("AA") else "BB")
        response = get(client, "/rs256", f"{header}.{payload}.{tampered_signature}")
        assert response.status_code == 401

    def test_rs256_missing_token(self, client):
        response = client.get("/rs256")
        assert response.status_code == 401

    def test_rs256_expired_token(self, client):
        token = make_token("RS256", RSA_PRIVATE_PEM, exp=int(time.time()) - 3600)
        response = get(client, "/rs256", token)
        assert response.status_code == 401

    def test_es256_valid_token(self, client):
        token = make_token("ES256", EC_PRIVATE_PEM)
        response = get(client, "/es256", token)
        assert response.status_code == 200

    def test_hs256_token_rejected_on_rs256_route(self, client):
        # Algorithm-confusion attack: HMAC token signed with the public PEM
        # as the shared secret must not verify against an RS256 backend.
        # Built by hand because PyJWT itself refuses to sign HMAC with a
        # public key — the attacker has no such scruples.
        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = b64url(json.dumps({"sub": "user123", "exp": int(time.time()) + 3600}).encode())
        message = f"{header}.{payload}"
        signature = b64url(hmac.new(RSA_PUBLIC_PEM.encode(), message.encode(), hashlib.sha256).digest())
        response = get(client, "/rs256", f"{message}.{signature}")
        assert response.status_code == 401


class TestAudience:
    def test_aud_array_matching_configured_audience(self, client):
        token = make_token("HS256", HS_SECRET, aud=["api", "web"])
        response = get(client, "/hs256-aud", token)
        assert response.status_code == 200
        assert response.json()["claims"]["aud"] == ["api", "web"]

    def test_aud_string_matching_configured_audience(self, client):
        token = make_token("HS256", HS_SECRET, aud="api")
        response = get(client, "/hs256-aud", token)
        assert response.status_code == 200

    def test_aud_mismatch_rejected(self, client):
        token = make_token("HS256", HS_SECRET, aud=["mobile"])
        response = get(client, "/hs256-aud", token)
        assert response.status_code == 401

    def test_missing_aud_rejected_when_audience_configured(self, client):
        token = make_token("HS256", HS_SECRET)
        response = get(client, "/hs256-aud", token)
        assert response.status_code == 401

    def test_aud_claim_ignored_when_no_audience_configured(self, client):
        # Auth0 access tokens always carry aud; a backend without a
        # configured audience must not reject them.
        token = make_token("HS256", HS_SECRET, aud="https://some.api")
        response = get(client, "/hs256", token)
        assert response.status_code == 200


class TestAlgorithmList:
    def test_second_configured_algorithm_accepted(self, client):
        token = make_token("HS384", HS_SECRET)
        response = get(client, "/hs-multi", token)
        assert response.status_code == 200

    def test_unlisted_algorithm_rejected(self, client):
        token = make_token("HS384", HS_SECRET)
        response = get(client, "/hs256", token)
        assert response.status_code == 401


class TestStartupValidation:
    """Invalid auth config must fail loudly at registration, not serve
    routes with the misconfigured backend silently dropped."""

    def _make_api(self, auth_backend):
        api = BoltAPI()

        @api.get("/x", auth=[auth_backend], guards=[IsAuthenticated()])
        async def endpoint():
            return {"ok": True}

        return api

    def test_invalid_pem_for_rs256_fails_startup(self):
        api = self._make_api(JWTAuthentication(secret="not-a-pem", algorithms=["RS256"]))
        with pytest.raises(Exception, match="Invalid RSA public key PEM"), TestClient(api):
            pass

    def test_unknown_algorithm_fails_startup(self):
        api = self._make_api(JWTAuthentication(secret=HS_SECRET, algorithms=["ES512"]))
        with pytest.raises(Exception, match="Unsupported JWT algorithm 'ES512'"), TestClient(api):
            pass

    def test_mixed_key_families_fail_startup(self):
        api = self._make_api(JWTAuthentication(secret=HS_SECRET, algorithms=["HS256", "RS256"]))
        with pytest.raises(Exception, match="mix key families"), TestClient(api):
            pass
