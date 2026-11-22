"""runbolt serves an admin site behind a path parameter (issue #872)."""

from __future__ import annotations

import pytest

from tests.integration.apps import app_module

TENANT_URLS = """
from django.contrib.admin import AdminSite
from django.http import HttpResponse
from django.urls import path


class TenantAdminSite(AdminSite):
    def get_urls(self):
        return [path("login/", self.tenant_login, name="login"), path("", self.tenant_login, name="index")]

    def tenant_login(self, request, tenant):
        return HttpResponse(f"admin login of {tenant}")


site = TenantAdminSite()
urlpatterns = [path("<slug:tenant>/admin/", site.urls)]
"""


@pytest.mark.server_integration
def test_runbolt_serves_an_admin_site_behind_a_path_parameter(make_server_project):
    project = make_server_project(
        api_module=app_module("tenant_admin"),
        installed_apps=["django.contrib.admin", "django.contrib.sessions", "django.contrib.messages"],
        middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        templates=[
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
        ],
        urls_content=TENANT_URLS,
    )

    with project.start() as server:
        acme = server.get("/acme/admin/login/")
        bolt_route = server.get("/items/7")
    _stdout, stderr = server.stop()

    assert acme.status_code == 200
    assert acme.text == "admin login of acme"
    assert bolt_route.json() == {"item_id": 7}
    assert "Django answers each path that has no Bolt route" in stderr
