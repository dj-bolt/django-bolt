"""django-allauth logins, read by Bolt routes on a real ``runbolt`` server.

The user logs in through the login form of allauth under ``/accounts``, or
through its headless API under ``/_allauth`` (both ``api.mount_django``):

- A form login or a headless browser login starts a session. Bolt routes read
  the session user through ``AuthenticationMiddleware`` and allauth's
  ``AccountMiddleware``, in each supported way, and with both wrappers.
- A headless app login with the JWT strategy gives an access token. Bolt's
  ``JWTAuthentication`` accepts it and loads the user.
- The same app login gives a session token. A small dependency reads the user
  from the ``X-Session-Token`` header, also on the request lanes.

See ``apps/allauth_session.py`` for the routes.

This needs a server project: allauth needs its own apps, URLconf, templates,
and migrations, which the shared test settings of ``TestClient`` do not have.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

USERS = {"alice": "alice-password-for-tests-1", "bob": "bob-password-for-tests-2"}
PREFIXES = ("", "/single")
ROUTES = ("/me/sync", "/me/async", "/me/auser", "/me/current", "/me/current-sync")
TOKEN_ROUTES = ("/token/me/sync", "/token/me/async", "/token/me/current", "/token/me/current-sync")
SESSION_BOUND_ROUTES = ("/token/session/me/sync", "/token/session/me/current")
SESSION_TOKEN_ROUTES = ("/me/session-token", "/me/session-token-sync")
HEADLESS = "/_allauth"

_SETTINGS = """
# The JWT strategy of allauth signs with SECRET_KEY for HS256, as JWTAuthentication verifies by default.
SECRET_KEY = "django-bolt-server-integration-allauth-jwt-secret-key"
HEADLESS_TOKEN_STRATEGY = "allauth.headless.tokens.strategies.jwt.JWTTokenStrategy"
HEADLESS_JWT_ALGORITHM = "HS256"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
LOGIN_REDIRECT_URL = "/me/sync"
"""

_URLS = """
from django.urls import include, path

urlpatterns = [
    path("accounts/", include("allauth.urls")),
    path("_allauth/", include("allauth.headless.urls")),
]
"""

_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


@pytest.fixture
def allauth_server(make_server_project):
    project = make_server_project(
        api_module=app_module("allauth_session"),
        installed_apps=[
            "django.contrib.sessions",
            "django.contrib.messages",
            "allauth",
            "allauth.account",
            "allauth.headless",
        ],
        middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
            "allauth.account.middleware.AccountMiddleware",
        ],
        templates=_TEMPLATES,
        urls_content=_URLS,
        settings_extra=_SETTINGS,
    )
    # The session and allauth tables must exist before the first request.
    project.manage("migrate", "--noinput")
    project.manage(
        "shell",
        "-c",
        "from django.contrib.auth.models import User\n"
        + "".join(f"User.objects.create_user({name!r}, password={pw!r})\n" for name, pw in USERS.items()),
    )
    with project.start() as server:
        yield server


def _client(server) -> httpx.Client:
    return httpx.Client(base_url=server.base_url, follow_redirects=False, timeout=10)


def _csrf_token(page: httpx.Response) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    assert match, page.text[:500]
    return match.group(1)


@contextlib.contextmanager
def _login(server, username: str) -> Iterator[httpx.Client]:
    """A client with the session of a login through the form of allauth."""
    with _client(server) as client:
        page = client.get("/accounts/login/")
        assert page.status_code == 200, page.text[:500]
        response = client.post(
            "/accounts/login/",
            data={"login": username, "password": USERS[username], "csrfmiddlewaretoken": _csrf_token(page)},
        )
        assert response.status_code == 302, response.text[:500]
        assert "sessionid" in client.cookies
        yield client


@contextlib.contextmanager
def _browser_login(server, username: str) -> Iterator[httpx.Client]:
    """A client with the session of a login through the headless browser API."""
    with _client(server) as client:
        # A headless browser view sets the CSRF cookie; the next unsafe request sends it back as a header.
        session = client.get(f"{HEADLESS}/browser/v1/auth/session")
        assert session.status_code == 401, session.text[:500]
        response = client.post(
            f"{HEADLESS}/browser/v1/auth/login",
            json={"username": username, "password": USERS[username]},
            headers={"X-CSRFToken": client.cookies["csrftoken"]},
        )
        assert response.status_code == 200, response.text[:500]
        assert response.json()["meta"]["is_authenticated"] is True
        assert "sessionid" in client.cookies
        yield client


def _app_login(server, username: str) -> dict:
    """The ``meta`` of a login through the headless app API: session token, access and refresh tokens."""
    with _client(server) as client:
        response = client.post(
            f"{HEADLESS}/app/v1/auth/login", json={"username": username, "password": USERS[username]}
        )
        assert response.status_code == 200, response.text[:500]
        meta = response.json()["meta"]
        assert {"session_token", "access_token", "refresh_token"} <= meta.keys(), meta
        return meta


def _check_user(client: httpx.Client, prefix: str, username: str) -> None:
    for route in ROUTES:
        response = client.get(prefix + route)
        assert response.status_code == 200, (prefix + route, response.text)
        body = response.json()
        assert body["authenticated"] is True, (prefix + route, body)
        assert body["username"] == username, (prefix + route, body)
    optional = client.get(prefix + "/me/optional")
    assert optional.json() == {"username": username}


def test_an_anonymous_request_has_no_user(allauth_server):
    with _client(allauth_server) as client:
        for prefix in PREFIXES:
            body = client.get(prefix + "/me/sync").json()
            assert body == {"authenticated": False, "username": None, "allauth": True}, (prefix, body)
            assert client.get(prefix + "/me/async").json()["authenticated"] is False
            assert client.get(prefix + "/me/auser").json()["authenticated"] is False
            assert client.get(prefix + "/me/current").status_code == 401
            assert client.get(prefix + "/me/current-sync").status_code == 401
            assert client.get(prefix + "/me/optional").json() == {"username": None}


def test_an_allauth_login_reaches_each_way_to_read_the_user(allauth_server):
    """The session that allauth starts is the user of each Bolt route, with both wrappers."""
    with _login(allauth_server, "alice") as client:
        for prefix in PREFIXES:
            _check_user(client, prefix, "alice")
            # AccountMiddleware ran, and Bolt copied request.allauth to request.state.
            assert client.get(prefix + "/me/async").json()["allauth"] is True


def test_an_allauth_logout_ends_the_session_for_bolt_routes(allauth_server):
    with _login(allauth_server, "alice") as client:
        page = client.get("/accounts/logout/")
        assert page.status_code == 200, page.text[:500]
        response = client.post("/accounts/logout/", data={"csrfmiddlewaretoken": _csrf_token(page)})
        assert response.status_code == 302, response.text[:500]
        for prefix in PREFIXES:
            assert client.get(prefix + "/me/current").status_code == 401
            assert client.get(prefix + "/me/sync").json()["authenticated"] is False


def test_two_sessions_do_not_mix(allauth_server):
    """Concurrent requests of two logged-in users each read their own user."""
    cookies = {}
    for name in USERS:
        with _login(allauth_server, name) as client:
            cookies[name] = dict(client.cookies)

    def check(case: tuple[str, str]) -> None:
        name, prefix = case
        # A client for each check: the connection pool of httpx is not safe to share between threads.
        with httpx.Client(base_url=allauth_server.base_url, cookies=cookies[name], timeout=10) as client:
            _check_user(client, prefix, name)

    cases = [(name, prefix) for _ in range(20) for name in USERS for prefix in PREFIXES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check, cases))


def test_a_headless_browser_login_reaches_each_way_to_read_the_user(allauth_server):
    with _browser_login(allauth_server, "alice") as client:
        for prefix in PREFIXES:
            _check_user(client, prefix, "alice")


def test_a_headless_browser_logout_ends_the_session_for_bolt_routes(allauth_server):
    with _browser_login(allauth_server, "alice") as client:
        response = client.delete(
            f"{HEADLESS}/browser/v1/auth/session", headers={"X-CSRFToken": client.cookies["csrftoken"]}
        )
        assert response.status_code == 401, response.text[:500]
        for prefix in PREFIXES:
            assert client.get(prefix + "/me/current").status_code == 401
            assert client.get(prefix + "/me/sync").json()["authenticated"] is False


def test_jwt_authentication_accepts_the_access_token_of_the_allauth_jwt_strategy(allauth_server):
    meta = _app_login(allauth_server, "alice")
    with _client(allauth_server) as client:
        for route in TOKEN_ROUTES:
            response = client.get(route, headers={"Authorization": f"Bearer {meta['access_token']}"})
            assert response.status_code == 200, (route, response.text)
            assert response.json() == {"authenticated": True, "username": "alice"}, route
        assert client.get("/token/me/current").status_code == 401


def test_jwt_authentication_rejects_the_refresh_token_of_the_allauth_jwt_strategy(allauth_server):
    """allauth marks a refresh token with ``token_use``, not ``typ``. It must not act as an access token."""
    meta = _app_login(allauth_server, "alice")
    with _client(allauth_server) as client:
        for route in TOKEN_ROUTES:
            response = client.get(route, headers={"Authorization": f"Bearer {meta['refresh_token']}"})
            assert response.status_code == 401, (route, response.text)


def test_a_dependency_reads_the_user_of_an_x_session_token(allauth_server):
    tokens = {name: _app_login(allauth_server, name)["session_token"] for name in USERS}

    def check(case: tuple[str, str, str]) -> None:
        name, prefix, route = case
        with _client(allauth_server) as client:
            response = client.get(prefix + route, headers={"X-Session-Token": tokens[name]})
            assert response.status_code == 200, (prefix + route, response.text)
            assert response.json() == {"authenticated": True, "username": name}, prefix + route

    # Concurrent requests of two users, on the lanes of the stack and with the single wrappers.
    cases = [
        (name, prefix, route)
        for _ in range(10)
        for name in USERS
        for prefix in PREFIXES
        for route in SESSION_TOKEN_ROUTES
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check, cases))

    with _client(allauth_server) as client:
        for prefix in PREFIXES:
            for route in SESSION_TOKEN_ROUTES:
                assert client.get(prefix + route).status_code == 401
                assert client.get(prefix + route, headers={"X-Session-Token": "not-a-session"}).status_code == 401


def test_a_headless_app_logout_ends_the_x_session_token(allauth_server):
    token = _app_login(allauth_server, "alice")["session_token"]
    with _client(allauth_server) as client:
        headers = {"X-Session-Token": token}
        assert client.get("/me/session-token-sync", headers=headers).status_code == 200
        response = client.delete(f"{HEADLESS}/app/v1/auth/session", headers=headers)
        assert response.status_code == 401, response.text[:500]
        for prefix in PREFIXES:
            for route in SESSION_TOKEN_ROUTES:
                assert client.get(prefix + route, headers=headers).status_code == 401


def test_an_allauth_logout_ends_its_access_token_for_bolt_routes(allauth_server):
    """The /session routes check the session of the token, as allauth's stateful validation does."""
    meta = _app_login(allauth_server, "alice")
    with _client(allauth_server) as client:
        bearer = {"Authorization": f"Bearer {meta['access_token']}"}
        for route in SESSION_BOUND_ROUTES:
            assert client.get(route, headers=bearer).json() == {"authenticated": True, "username": "alice"}, route

        response = client.delete(f"{HEADLESS}/app/v1/auth/session", headers={"X-Session-Token": meta["session_token"]})
        assert response.status_code == 401, response.text

        for route in SESSION_BOUND_ROUTES:
            assert client.get(route, headers=bearer).status_code == 401, route
        # A route with no session check accepts the token until it expires.
        assert client.get("/token/me/current", headers=bearer).status_code == 200
