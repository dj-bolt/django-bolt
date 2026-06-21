"""Server integration test for Bolt URL reversing.

Boots a real ``runbolt`` server whose ``ROOT_URLCONF`` wires Bolt routes in with
the documented ``path("", include("django_bolt.urls"))``, then hits an endpoint
that renders a Django template. This proves the ``{% url %}`` tag (and the native
``reverse()`` it calls) resolves Bolt route names end-to-end in a real process.
"""

from __future__ import annotations

import pytest

from .apps import app_source

pytestmark = pytest.mark.server_integration


_URLS = """
from django.urls import include, path

urlpatterns = [path("", include("django_bolt.urls"))]
"""

_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]


def test_url_template_tag_resolves_bolt_route(make_server_project):
    project = make_server_project(api_source=app_source("url_reverse"), urls_content=_URLS, templates=_TEMPLATES)
    with project.start() as server:
        body = server.wait_for_text("/render", 'href="/missions/42"')
    assert body == '<a href="/missions/42">go</a>'
