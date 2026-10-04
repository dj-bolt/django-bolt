"""Docs behind a Django admin login, on a real ``runbolt`` server (issue #378).

The project default is JWT auth with ``IsAuthenticated``. The docs set
``SessionAuthentication``, so a staff user who logged in to the admin can open
them in a browser. See ``apps/docs_session.py``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator

import httpx
import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

USERS = {"staff": ("staff-password-for-tests-1", True), "member": ("member-password-for-tests-2", False)}

_SETTINGS = """
from django_bolt.auth import IsAuthenticated, JWTAuthentication

BOLT_AUTHENTICATION_CLASSES = [JWTAuthentication()]
BOLT_DEFAULT_PERMISSION_CLASSES = [IsAuthenticated()]
"""

_URLS = """
from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(template_name="admin/login.html")),
]
"""

_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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
def docs_server(make_server_project):
    project = make_server_project(
        api_module=app_module("docs_session"),
        installed_apps=["django.contrib.admin", "django.contrib.sessions", "django.contrib.messages"],
        middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        templates=_TEMPLATES,
        urls_content=_URLS,
        settings_extra=_SETTINGS,
    )
    project.manage("migrate", "--noinput")
    project.manage(
        "shell",
        "-c",
        "from django.contrib.auth.models import User\n"
        + "".join(
            f"User.objects.create_user({name!r}, password={password!r}, is_staff={is_staff!r})\n"
            for name, (password, is_staff) in USERS.items()
        ),
    )
    with project.start() as server:
        yield server


@contextlib.contextmanager
def _login(server, username: str, login_path: str) -> Iterator[httpx.Client]:
    """A client with the session of a login through a Django login form."""
    with httpx.Client(base_url=server.base_url, follow_redirects=False, timeout=10) as client:
        page = client.get(login_path)
        assert page.status_code == 200, page.text[:500]
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
        assert match, page.text[:500]
        response = client.post(
            login_path,
            data={
                "username": username,
                "password": USERS[username][0],
                "csrfmiddlewaretoken": match.group(1),
                "next": "/docs",
            },
            headers={"referer": f"{server.base_url}{login_path}"},
        )
        assert response.status_code == 302, response.text[:500]
        assert "sessionid" in client.cookies
        yield client


def test_anonymous_browser_goes_to_the_admin_login(docs_server):
    response = httpx.get(f"{docs_server.base_url}/docs", follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/admin/login/?next=/docs"


def test_staff_session_opens_the_docs(docs_server):
    with _login(docs_server, "staff", "/admin/login/") as client:
        for route in ("/docs", "/docs/openapi.json"):
            response = client.get(route)
            assert response.status_code == 200, f"{route}: {response.text[:500]}"
        assert client.get("/me").json() == {"username": "staff"}


def test_member_session_gets_403_on_the_docs(docs_server):
    with _login(docs_server, "member", "/accounts/login/") as client:
        assert client.get("/docs").status_code == 403
        assert client.get("/me").json() == {"username": "member"}
