"""
Tests for Django template rendering via the render shortcut.

Issue: render() defaults content_type=None, which creates a header tuple with None value.
Rust fails to extract Vec<(String, String)> and returns 500 error.

Fix: render() should default content_type to "text/html".

Note: The test template "test_dashboard.html" is defined in conftest.py via locmem loader.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.responses import HTML
from django_bolt.shortcuts import render
from django_bolt.testing import TestClient


@pytest.fixture(scope="module")
def api():
    api = BoltAPI()

    @api.get("/dashboard")
    async def dashboard(req):
        return render(req, "test_dashboard.html", {"title": "Dashboard"})

    @api.get("/html")
    async def html():
        return HTML("<h1>Dashboard</h1>")

    @api.get("/missing")
    async def missing(req):
        return render(req, "page.html", {"title": "Gone"}, status=404, using="second")

    return api


@pytest.fixture(scope="module")
def client(api):
    return TestClient(api)


def _engine(name: str, template: str) -> dict:
    return {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "NAME": name,
        "OPTIONS": {
            "context_processors": ["django.template.context_processors.request"],
            "loaders": [("django.template.loaders.locmem.Loader", {"page.html": template})],
        },
    }


class TestRenderShortcut:
    def test_render_returns_200(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200

    def test_render_returns_html_content(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "<h1>Dashboard</h1>" in response.text

    def test_render_returns_html_content_type(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "text/html" in content_type

    def test_render_sends_the_content_type_of_html(self, client):
        # HTML() and render() of Django send the charset. render() of Bolt sent "text/html" only.
        assert client.get("/html").headers["content-type"] == "text/html; charset=utf-8"
        assert client.get("/dashboard").headers["content-type"] == "text/html; charset=utf-8"

    def test_render_passes_request_engine_and_status(self, client):
        engines = [
            _engine("first", "first {{ title }}"),
            _engine("second", "second {{ title }} at {{ request.path }}"),
        ]
        with override_settings(TEMPLATES=engines):
            response = client.get("/missing")
        assert response.status_code == 404
        assert response.text == "second Gone at /missing"
