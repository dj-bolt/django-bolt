"""django-allauth session login, read by Bolt routes on a real ``runbolt`` server.

The user logs in through the login form of allauth, which Django serves under
``/accounts`` (``api.mount_django``). Bolt routes then read the session user
through ``AuthenticationMiddleware`` and allauth's ``AccountMiddleware``, in
each supported way, and with both wrappers (see ``apps/allauth_session.py``).

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

_SETTINGS = """
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

urlpatterns = [path("accounts/", include("allauth.urls"))]
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
        installed_apps=["django.contrib.sessions", "django.contrib.messages", "allauth", "allauth.account"],
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
