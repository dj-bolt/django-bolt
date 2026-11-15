from __future__ import annotations

import time
from typing import ClassVar

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, IsAuthenticated, JWTAuthentication, JWTAuthViews
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db


class User:
    pk = "view-user"
    is_active = True


def test_ready_made_cookie_lifecycle_routes():
    api = BoltAPI()
    store = InMemoryRevocation()

    async def validate(request, credentials):
        if credentials.username == "alice" and credentials.password == "correct":
            return User()
        return None

    JWTAuthViews(
        store=store,
        secret="a-secure-test-secret-that-is-long-enough",
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)

    with TestClient(api) as client:
        denied = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        assert denied.status_code == 401

        login = client.post("/auth/login", json={"username": "alice", "password": "correct"})
        assert login.status_code == 200
        assert client.cookies.get("access_token")
        assert client.cookies.get("refresh_token")

        headers = {"Sec-Fetch-Site": "none"}
        refreshed = client.post("/auth/refresh", headers=headers)
        assert refreshed.status_code == 200

        logout_all = client.post("/auth/logout-all", headers=headers)
        assert logout_all.status_code == 200
        assert client.cookies.get("access_token") is None
        assert client.cookies.get("refresh_token") is None


def test_auth_views_are_documented_in_openapi():
    api = BoltAPI()
    JWTAuthViews(store=InMemoryRevocation(), secret="a-secure-test-secret-that-is-long-enough").register(api)
    api._register_openapi_routes()

    with TestClient(api) as client:
        schema = client.get("/docs/openapi.json").json()
    assert set(schema["paths"]) >= {
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/logout-all",
    }


SECRET = "a-secure-test-secret-that-is-long-enough"


def _views_api(**options) -> BoltAPI:
    api = BoltAPI()

    async def validate(request, credentials):
        return User() if credentials.password == "correct" else None

    options.setdefault("secret", SECRET)
    JWTAuthViews(store=InMemoryRevocation(), credential_validator=validate, **options).register(api)
    return api


LOGIN = {"username": "alice", "password": "correct"}
NO_CSRF_ORIGIN = {"Sec-Fetch-Site": "none"}


@pytest.mark.parametrize("secure_cookies", [True, False], ids=["secure", "not-secure"])
def test_cookies_are_secure_unless_the_project_turns_it_off(secure_cookies):
    options = {} if secure_cookies else {"secure_cookies": False}

    with TestClient(_views_api(**options)) as client:
        login = client.post("/auth/login", json=LOGIN)

    cookies = login.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        attributes = cookie.split("; ")
        assert ("Secure" in attributes) is secure_cookies
        assert "HttpOnly" in attributes


def test_views_use_the_given_key_algorithm_and_prefix():
    """Login signs with the secret, algorithm and ``kid`` of the views. The refresh route accepts these tokens."""
    api = _views_api(algorithm="HS512", kid="key-1", prefix="/api/auth-X/", secure_cookies=False)

    with TestClient(api) as client:
        login = client.post("/api/auth-X/login", json=LOGIN)
        assert login.status_code == 200
        access = client.cookies.get("access_token")
        refresh = client.cookies.get("refresh_token")
        for token in (access, refresh):
            assert jwt.get_unverified_header(token)["kid"] == "key-1"
            assert jwt.decode(token, SECRET, algorithms=["HS512"])["sub"] == "view-user"
        # Only the new prefix has routes.
        assert client.post("/auth/login", json=LOGIN).status_code == 404

        refreshed = client.post("/api/auth-X/refresh", headers=NO_CSRF_ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        assert client.cookies.get("refresh_token") != refresh
        assert client.post("/api/auth-X/logout", headers=NO_CSRF_ORIGIN).status_code == 200


@pytest.fixture(scope="module")
def rsa_pems() -> tuple[str, str]:
    """Give one RSA key pair as (private PEM, public PEM). Key generation is slow."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def test_views_sign_with_the_private_key_and_verify_with_the_public_key(rsa_pems):
    """With an asymmetric algorithm, ``secret`` signs the tokens and ``public_key`` verifies them."""
    private_pem, public_pem = rsa_pems
    api = _views_api(secret=private_pem, public_key=public_pem, algorithm="RS256", kid="k1", secure_cookies=False)

    @api.get("/me", auth=[JWTAuthentication(public_key=public_pem, algorithms=["RS256"])], guards=[IsAuthenticated()])
    async def me(request):
        return {"user_id": request["context"]["user_id"]}

    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        first_refresh = client.cookies.get("refresh_token")

        refreshed = client.post("/auth/refresh", headers=NO_CSRF_ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        access = client.cookies.get("access_token")
        refresh = client.cookies.get("refresh_token")
        assert refresh != first_refresh
        # The refresh route signs the new pair with the private key.
        for token in (access, refresh):
            assert jwt.get_unverified_header(token)["kid"] == "k1"
            assert jwt.decode(token, public_pem, algorithms=["RS256"])["sub"] == "view-user"

        # A route of the application verifies the access token with the public key only.
        profile = client.get("/me", headers={"Authorization": f"Bearer {access}"})
        assert profile.status_code == 200, profile.text
        assert profile.json() == {"user_id": "view-user"}

        assert client.post("/auth/logout", headers=NO_CSRF_ORIGIN).status_code == 200
        assert client.cookies.get("refresh_token") is None


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"algorithm": "RS256", "secret": "private-pem"}, "requires 'secret' and 'public_key'"),
        ({"algorithm": "RS256", "public_key": "public-pem"}, "requires 'secret' and 'public_key'"),
        ({"public_key": "public-pem"}, "'public_key' only with an asymmetric algorithm"),
    ],
    ids=["asymmetric-without-public-key", "asymmetric-without-private-key", "hmac-with-public-key"],
)
def test_keys_that_do_not_match_the_algorithm_fail_at_construction(options, message):
    """A key set that cannot work must stop the startup, not the first refresh."""
    with pytest.raises(ImproperlyConfigured, match=message):
        JWTAuthViews(store=InMemoryRevocation(), **options)


def test_refresh_cookie_without_a_jti_is_rejected():
    """A refresh token that cannot be revoked must not reach the lifecycle routes."""
    now = int(time.time())
    claims = {"sub": "view-user", "typ": "refresh", "fam": "family-1", "ver": 0, "iat": now, "exp": now + 600}
    token = jwt.encode(claims, SECRET, algorithm="HS256")

    with TestClient(_views_api()) as client:
        for path in ("/auth/refresh", "/auth/logout", "/auth/logout-all"):
            response = client.post(path, headers={"Cookie": f"refresh_token={token}", **NO_CSRF_ORIGIN})
            assert response.status_code == 401, path

        # The same token with a jti passes the check.
        claims["jti"] = "jti-1"
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        response = client.post("/auth/logout-all", headers={"Cookie": f"refresh_token={token}", **NO_CSRF_ORIGIN})
        assert response.status_code == 200, response.text


def _post_with_refresh(client: TestClient, path: str, token: str):
    """Send one refresh token, and no other cookie, to a lifecycle route."""
    client.cookies.clear()
    return client.post(path, headers={"Cookie": f"refresh_token={token}", **NO_CSRF_ORIGIN})


@pytest.mark.parametrize("path", ["/auth/logout", "/auth/logout-all"])
def test_used_refresh_token_cannot_log_out_the_token_that_replaced_it(path):
    """Refresh uses R0 and gives R1. R0 must not end the session of R1."""
    with TestClient(_views_api(secure_cookies=False)) as client:
        client.post("/auth/login", json=LOGIN)
        used = client.cookies.get("refresh_token")
        assert client.post("/auth/refresh", headers=NO_CSRF_ORIGIN).status_code == 200
        live = client.cookies.get("refresh_token")

        rejected = _post_with_refresh(client, path, used)
        assert rejected.status_code == 401
        # The body is the same as for a token that the refresh route rejects.
        assert rejected.json() == {"detail": "Invalid token"}
        assert _post_with_refresh(client, "/auth/refresh", live).status_code == 200


def test_refresh_token_from_before_logout_all_cannot_log_out_a_new_login():
    """Logout-all makes R0 invalid. R0 must not log out the next login, R1."""
    with TestClient(_views_api(secure_cookies=False)) as client:
        client.post("/auth/login", json=LOGIN)
        old = client.cookies.get("refresh_token")
        assert client.post("/auth/logout-all", headers=NO_CSRF_ORIGIN).status_code == 200
        client.post("/auth/login", json=LOGIN)
        new = client.cookies.get("refresh_token")

        assert _post_with_refresh(client, "/auth/logout-all", old).status_code == 401
        assert _post_with_refresh(client, "/auth/refresh", new).status_code == 200
        # The tokens of the new login have version 1. They are live, so they can log out.
        assert client.post("/auth/logout", headers=NO_CSRF_ORIGIN).status_code == 200


def test_logged_out_refresh_token_cannot_log_out_everywhere():
    """Logout revokes the family of R0. R0 must not log out the other session, R1."""
    with TestClient(_views_api(secure_cookies=False)) as client:
        client.post("/auth/login", json=LOGIN)
        logged_out = client.cookies.get("refresh_token")
        client.post("/auth/login", json=LOGIN)
        other = client.cookies.get("refresh_token")
        assert _post_with_refresh(client, "/auth/logout", logged_out).status_code == 200

        assert _post_with_refresh(client, "/auth/logout-all", logged_out).status_code == 401
        assert _post_with_refresh(client, "/auth/refresh", other).status_code == 200


def test_refresh_token_without_a_version_has_version_zero():
    """The refresh route reads a token with no ``ver`` claim as version 0. The logout routes do the same."""
    now = int(time.time())
    claims = {"sub": "view-user", "typ": "refresh", "fam": "family-1", "iat": now, "exp": now + 600}
    first, second = (jwt.encode({**claims, "jti": jti}, SECRET, algorithm="HS256") for jti in ("jti-1", "jti-2"))

    with TestClient(_views_api()) as client:
        # The user has version 0, so the token is live. This logout-all gives the user version 1.
        assert _post_with_refresh(client, "/auth/logout-all", first).status_code == 200
        # Version 0 is now old.
        assert _post_with_refresh(client, "/auth/logout-all", second).status_code == 401
        assert _post_with_refresh(client, "/auth/refresh", second).status_code == 401


class RecordingBackend(ModelBackend):
    """Django auth backend that keeps the request that each login attempt passes."""

    requests: ClassVar[list] = []

    async def aauthenticate(self, request, username=None, password=None, **kwargs):
        type(self).requests.append(request)
        return await super().aauthenticate(request, username=username, password=password, **kwargs)


@pytest.mark.django_db(transaction=True)
def test_default_login_checks_the_django_user(settings):
    """With no ``credential_validator``, login uses Django ``authenticate`` and the user table."""
    backend_path = "tests.test_jwt_auth_views.RecordingBackend"
    settings.AUTHENTICATION_BACKENDS = [backend_path]
    # Django imports the backend by its path. Read the list from the class that Django uses.
    recorded_requests = import_string(backend_path).requests
    recorded_requests.clear()
    get_user_model().objects.create_user("carol", password="right-password")
    get_user_model().objects.create_user("dave", password="right-password", is_active=False)

    api = BoltAPI()
    JWTAuthViews(
        store=InMemoryRevocation(),
        secret="a-secure-test-secret-that-is-long-enough",
        secure_cookies=False,
    ).register(api)

    with TestClient(api) as client:
        wrong_password = client.post("/auth/login", json={"username": "carol", "password": "wrong"})
        unknown_user = client.post("/auth/login", json={"username": "nobody", "password": "right-password"})
        inactive_user = client.post("/auth/login", json={"username": "dave", "password": "right-password"})
        assert [wrong_password.status_code, unknown_user.status_code, inactive_user.status_code] == [401] * 3
        assert not client.cookies.get("access_token")

        recorded_requests.clear()
        login = client.post("/auth/login", json={"username": "carol", "password": "right-password"})

        assert login.status_code == 200
        assert client.cookies.get("access_token")
        assert client.cookies.get("refresh_token")
    # The login passes the request of the client to Django, for backends that read it.
    assert len(recorded_requests) == 1
    assert recorded_requests[0] is not None
