"""The user of the request with django-debug-toolbar on Bolt routes.

The toolbar is Django middleware, so each request with it runs on a lane.
The docs tell users to enable it with ``BoltAPI(django_middleware=[...])``,
and the example project turns it on with ``DEBUG = True``. The user must
then load in each supported way, and the SQL panel must record the query.
"""

from __future__ import annotations

import sys
import time

import jwt
import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import include, path

from django_bolt import BoltAPI, CurrentUser, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.shortcuts import render
from django_bolt.testing import TestClient

pytest.importorskip("debug_toolbar")

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "debug-toolbar-test-secret-longer-than-32-characters"
TOOLBAR = ["debug_toolbar.middleware.DebugToolbarMiddleware"]

urlpatterns: list = []


@pytest.fixture
def toolbar_settings(monkeypatch):
    """Turn the toolbar on for every request, as ``DEBUG = True`` does in the example project."""
    templates = [
        {
            **settings.TEMPLATES[0],
            "APP_DIRS": False,
            "OPTIONS": {
                **settings.TEMPLATES[0]["OPTIONS"],
                "loaders": [
                    (
                        "django.template.loaders.locmem.Loader",
                        {"account.html": "<html><body><p>{{ user.username }}</p></body></html>"},
                    ),
                    "django.template.loaders.app_directories.Loader",
                ],
            },
        }
    ]
    with override_settings(
        INSTALLED_APPS=[*settings.INSTALLED_APPS, "debug_toolbar"],
        ROOT_URLCONF=__name__,
        TEMPLATES=templates,
        DEBUG_TOOLBAR_PANELS=["debug_toolbar.panels.history.HistoryPanel", "debug_toolbar.panels.sql.SQLPanel"],
        DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": lambda _request: True},
    ):
        # The toolbar URLs import its models, so the app must be installed first.
        monkeypatch.setattr(
            sys.modules[__name__], "urlpatterns", [path("__debug__/", include("debug_toolbar.urls"))], raising=False
        )
        yield


def _toolbar_api() -> BoltAPI:
    api = BoltAPI(django_middleware=TOOLBAR)
    auth = [JWTAuthentication(secret=SECRET)]

    @api.get("/auser", auth=auth, guards=[IsAuthenticated()])
    async def with_auser(request: Request):
        user = await request.auser()
        return {"username": user.username}

    @api.get("/current", auth=auth, guards=[IsAuthenticated()])
    async def with_current_user(user: CurrentUser):
        return {"username": user.username}

    @api.get("/sync", auth=auth, guards=[IsAuthenticated()])
    def with_sync_read(request: Request):
        return {"username": request.user.username}

    @api.get("/page", auth=auth, guards=[IsAuthenticated()])
    async def page(request: Request):
        await request.auser()
        return render(request, "account.html")

    return api


def _headers(user: User) -> dict[str, str]:
    token = jwt.encode({"sub": str(user.pk), "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("route", ["/auser", "/current", "/sync"])
def test_the_user_loads_behind_the_toolbar(toolbar_settings, route):
    user = User.objects.create(username="toolbar_user")

    with TestClient(_toolbar_api()) as client:
        response = client.get(route, headers=_headers(user))

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "toolbar_user"}
    assert response.headers.get("djdt-request-id")


def test_the_sql_panel_records_the_user_query(toolbar_settings):
    from debug_toolbar.store import get_store  # noqa: PLC0415 - needs debug_toolbar in INSTALLED_APPS

    user = User.objects.create(username="sql_panel_user")

    with TestClient(_toolbar_api()) as client:
        response = client.get("/auser", headers=_headers(user))

    assert response.status_code == 200, response.text
    stats = get_store().panel(response.headers["djdt-request-id"], "SQLPanel")
    queries = [query["sql"] for query in stats["queries"]]
    assert any("auth_user" in sql for sql in queries), queries


def test_a_page_reads_the_user_that_auser_loaded(toolbar_settings):
    user = User.objects.create(username="page_user")

    with TestClient(_toolbar_api()) as client:
        response = client.get("/page", headers=_headers(user))

    assert response.status_code == 200, response.text
    assert "<p>page_user</p>" in response.text
    assert 'id="djDebug"' in response.text
