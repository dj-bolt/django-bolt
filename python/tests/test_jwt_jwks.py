"""JWKS support: verify RS256 tokens against a key set selected by ``kid``.

Providers like Clerk, Auth0, and Okta publish a JWKS endpoint and rotate
signing keys, identifying each with a ``kid`` header on the token. These
tests supply the JWKS directly (``jwks=``) so no network is needed; the
``jwks_url`` fetch path shares the same Rust key-selection code.
"""

from __future__ import annotations

import json
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured
from jwt.algorithms import RSAAlgorithm

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, IsAuthenticated, JWTAuthentication
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


class _JwksResponse:
    """The part of an ``httpx.Response`` that ``_refresh_jwks`` reads."""

    def __init__(self, document):
        self.document = document
        self.text = json.dumps(document)

    def raise_for_status(self):
        pass

    def json(self):
        return self.document


def _serving(document):
    """Build a replacement for ``httpx.get`` that always answers with ``document``."""

    def fetch(url, timeout):
        return _JwksResponse(document)

    return fetch


def _remote_jwks_auth(monkeypatch, first_document, fetch):
    """Build a remote JWKS backend whose first fetch gives ``first_document``, then swap in ``fetch``."""
    monkeypatch.setattr(auth_backends.httpx, "get", _serving(first_document))
    auth = JWTAuthentication(
        jwks_url="https://issuer.example/jwks.json",
        algorithms=["RS256"],
        issuer="https://issuer.example/",
        audience="api",
    )
    auth.to_metadata()
    monkeypatch.setattr(auth_backends.httpx, "get", fetch)
    return auth


class TestJwksRefresh:
    def test_refresh_fetches_the_configured_url(self, monkeypatch):
        urls = []

        def fetch(url, timeout):
            urls.append(url)
            return _JwksResponse({"keys": [KEY2_JWK]})

        auth = _remote_jwks_auth(monkeypatch, JWKS, fetch)

        assert json.loads(auth._refresh_jwks()) == {"keys": [KEY2_JWK]}
        assert urls == ["https://issuer.example/jwks.json"]

    @pytest.mark.parametrize(
        "bad_document",
        [{"not_keys": []}, {"keys": "not-a-list"}, ["keys"]],
        ids=["no-keys", "keys-not-a-list", "not-a-dict"],
    )
    def test_malformed_document_keeps_cached_jwks(self, monkeypatch, bad_document):
        """A bad document from the issuer must not replace the key set that works."""
        auth = _remote_jwks_auth(monkeypatch, JWKS, _serving(bad_document))

        assert auth._refresh_jwks() is None
        assert auth._resolve_jwks() == json.dumps(JWKS)

    def test_refresh_in_flight_makes_a_second_refresh_return_at_once(self, monkeypatch):
        """Only one refresh runs at a time. A second caller does not wait and does not fetch."""
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            return _JwksResponse({"keys": [KEY2_JWK]})

        auth = _remote_jwks_auth(monkeypatch, JWKS, fetch)
        outcome = []
        waiter = threading.Thread(target=lambda: outcome.append(auth._refresh_jwks()))

        auth._jwks_refresh_lock.acquire()
        try:
            waiter.start()
            waiter.join(timeout=5)
            finished = not waiter.is_alive()
        finally:
            auth._jwks_refresh_lock.release()
        waiter.join(timeout=5)

        assert finished, "the second refresh waited for the lock"
        assert outcome == [None]
        assert calls == []


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

    def test_discovery_request_uses_a_ten_second_timeout(self, monkeypatch):
        requests = []

        def fetch(url, timeout):
            requests.append((url, timeout))
            return _JwksResponse(_discovery_document("https://issuer.example/tenantX"))

        monkeypatch.setattr(auth_backends.httpx, "get", fetch)

        auth = JWTAuthentication(oidc_issuer="https://issuer.example/tenantX/", audience="my-api")

        assert requests == [("https://issuer.example/tenantX/.well-known/openid-configuration", 10.0)]
        assert auth.oidc_issuer == "https://issuer.example/tenantX/"

    @pytest.mark.parametrize(
        ("oidc_issuer", "discovered_issuer"),
        [
            ("https://issuer.example/tenantX", "https://issuer.example/tenantX/"),
            ("https://issuer.example/tenantX/", "https://issuer.example/tenantX"),
        ],
        ids=["slash-in-document", "slash-in-config"],
    )
    def test_a_trailing_slash_does_not_change_the_issuer(self, monkeypatch, oidc_issuer, discovered_issuer):
        monkeypatch.setattr(auth_backends.httpx, "get", _serving(_discovery_document(discovered_issuer)))

        auth = JWTAuthentication(oidc_issuer=oidc_issuer, audience="my-api")

        assert auth.issuer == discovered_issuer

    def test_a_different_issuer_in_the_document_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            auth_backends.httpx,
            "get",
            _serving(_discovery_document("https://evil.example")),
        )

        with pytest.raises(ImproperlyConfigured) as raised:
            JWTAuthentication(oidc_issuer="https://issuer.example", audience="my-api")

        assert str(raised.value) == "OIDC discovery issuer does not match oidc_issuer."

    @pytest.mark.parametrize(
        "document",
        [
            {"issuer": "https://issuer.example"},
            {"jwks_uri": "https://issuer.example/keys"},
            {"issuer": 1, "jwks_uri": "https://issuer.example/keys"},
        ],
        ids=["no-jwks-uri", "no-issuer", "issuer-not-a-string"],
    )
    def test_an_incomplete_document_is_rejected(self, monkeypatch, document):
        monkeypatch.setattr(auth_backends.httpx, "get", _serving(document))

        with pytest.raises(ImproperlyConfigured) as raised:
            JWTAuthentication(oidc_issuer="https://issuer.example", audience="my-api")

        assert str(raised.value) == "OIDC discovery document must contain string 'issuer' and 'jwks_uri'."

    def test_algorithms_of_the_project_win_over_the_document(self, monkeypatch):
        monkeypatch.setattr(
            auth_backends.httpx,
            "get",
            _serving(_discovery_document("https://issuer.example", ["RS256", "ES256"])),
        )

        auth = JWTAuthentication(oidc_issuer="https://issuer.example", audience="my-api", algorithms=["ES256"])

        assert auth.algorithms == ["ES256"]


def _discovery_document(issuer: str, algorithms: list[str] | None = None) -> dict:
    return {
        "issuer": issuer,
        "jwks_uri": "https://issuer.example/keys",
        "id_token_signing_alg_values_supported": algorithms or ["RS256"],
    }


class TestJwtAuthenticationDefaults:
    def test_defaults(self):
        auth = JWTAuthentication(secret="a-secure-test-secret-that-is-long-enough")

        assert auth.leeway == 60
        assert auth.jwks_refresh_interval == 300
        assert auth.csrf is True
        assert auth.require_jti is False
        assert auth.algorithms == ["HS256"]
        assert auth.revoked_token_handler is None
        assert auth.revocation_store is None

    def test_options_are_kept(self):
        auth = JWTAuthentication(secret="a-secure-test-secret-that-is-long-enough", leeway=7, csrf=False)

        assert auth.leeway == 7
        assert auth.csrf is False

    def test_refresh_interval_of_one_second_is_valid(self):
        auth = JWTAuthentication(jwks=JWKS, jwks_refresh_interval=1)

        assert auth.jwks_refresh_interval == 1

    @pytest.mark.parametrize("mode", ["store", "handler", "both"])
    @pytest.mark.parametrize("require_jti", [False, True])
    def test_a_revocation_check_turns_on_require_jti(self, mode, require_jti):
        """A token with no jti cannot be revoked. A revocation check must reject it."""
        store = InMemoryRevocation()

        async def handler(jti):
            return False

        options = {
            "store": {"revocation_store": store},
            "handler": {"revoked_token_handler": handler},
            "both": {"revocation_store": store, "revoked_token_handler": handler},
        }[mode]

        auth = JWTAuthentication(secret="a-secure-test-secret-that-is-long-enough", require_jti=require_jti, **options)

        assert auth.require_jti is True
        assert auth.revocation_store is options.get("revocation_store")
        assert auth.revoked_token_handler is not None
        if "revoked_token_handler" in options:
            assert auth.revoked_token_handler is handler

    def test_require_jti_stays_on_without_a_revocation_check(self):
        auth = JWTAuthentication(secret="a-secure-test-secret-that-is-long-enough", require_jti=True)

        assert auth.require_jti is True


SECRET_FOR_ERRORS = "a-secure-test-secret-that-is-long-enough"
REMOTE = {"issuer": "https://issuer.example/", "audience": "api"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"secret": SECRET_FOR_ERRORS, "public_key": "pem"},
            "JWTAuthentication accepts either 'secret' (HMAC) or 'public_key' (asymmetric), not both.",
        ),
        (
            {"oidc_issuer": "https://issuer.example", "issuer": "https://issuer.example"},
            "JWTAuthentication oidc_issuer is mutually exclusive with 'issuer', 'jwks_url', and 'jwks'.",
        ),
        (
            {"oidc_issuer": "https://issuer.example", "jwks_url": "https://issuer.example/keys"},
            "JWTAuthentication oidc_issuer is mutually exclusive with 'issuer', 'jwks_url', and 'jwks'.",
        ),
        (
            {"oidc_issuer": "https://issuer.example", "jwks": JWKS},
            "JWTAuthentication oidc_issuer is mutually exclusive with 'issuer', 'jwks_url', and 'jwks'.",
        ),
        (
            {"jwks_url": "https://issuer.example/keys", "jwks": JWKS, **REMOTE},
            "JWTAuthentication accepts either 'jwks_url' or 'jwks', not both.",
        ),
        (
            {"jwks": JWKS, "secret": SECRET_FOR_ERRORS},
            "JWTAuthentication accepts JWKS (jwks_url/jwks) or a static key (secret/public_key), not both.",
        ),
        (
            {"jwks": JWKS, "jwks_refresh_interval": 0},
            "JWTAuthentication jwks_refresh_interval must be > 0 seconds.",
        ),
        (
            {"jwks": JWKS, "jwks_refresh_interval": -5},
            "JWTAuthentication jwks_refresh_interval must be > 0 seconds.",
        ),
        (
            {"oidc_issuer": "http://issuer.example", "audience": "api"},
            "JWTAuthentication oidc_issuer must be an absolute HTTPS URL.",
        ),
        (
            {"oidc_issuer": "https://", "audience": "api"},
            "JWTAuthentication oidc_issuer must be an absolute HTTPS URL.",
        ),
        (
            {"oidc_issuer": "https://issuer.example"},
            "JWTAuthentication with oidc_issuer requires 'audience'.",
        ),
        (
            {"jwks_url": "http://issuer.example/keys", **REMOTE},
            "JWTAuthentication jwks_url must be an absolute HTTPS URL.",
        ),
        (
            {"jwks_url": "https://issuer.example/keys", "audience": "api"},
            "JWTAuthentication with jwks_url requires both 'issuer' and 'audience' "
            "to prevent cross-application token substitution.",
        ),
        ({"secret": SECRET_FOR_ERRORS, "leeway": -1}, "JWTAuthentication leeway must be >= 0 seconds."),
        ({"secret": ""}, "JWTAuthentication secret must not be empty."),
        ({"secret": SECRET_FOR_ERRORS, "algorithms": []}, "JWTAuthentication algorithms must not be empty."),
    ],
    ids=[
        "secret-and-public-key",
        "oidc-and-issuer",
        "oidc-and-jwks-url",
        "oidc-and-jwks",
        "jwks-url-and-jwks",
        "jwks-and-secret",
        "refresh-interval-zero",
        "refresh-interval-negative",
        "oidc-http",
        "oidc-no-host",
        "oidc-no-audience",
        "jwks-url-http",
        "jwks-url-no-issuer",
        "negative-leeway",
        "empty-secret",
        "empty-algorithms",
    ],
)
def test_configuration_error_message_names_the_mistake(kwargs, message):
    with pytest.raises(ImproperlyConfigured) as raised:
        JWTAuthentication(**kwargs)

    assert str(raised.value) == message
