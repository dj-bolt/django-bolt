"""Server integration test for Bolt URL reversing.

Boots a real ``runbolt`` server whose ``ROOT_URLCONF`` wires Bolt routes in with
the documented ``path("", include("django_bolt.urls"))``, then hits an endpoint
that renders a Django template. This proves the ``{% url %}`` tag (and the native
``reverse()`` it calls) resolves Bolt route names end-to-end in a real process.
"""

from __future__ import annotations

import pytest

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

_API = """
from django.template import Context, Template

from django_bolt.responses import HTML


@api.get("/missions/{mission_id}", name="get_mission")
async def get_mission(mission_id: int):
    return {"id": mission_id}


@api.get("/render")
async def render_url_tag():
    html = Template('<a href="{% url \\'get_mission\\' mission_id=42 %}">go</a>').render(Context())
    return HTML(html)
"""


def test_url_template_tag_resolves_bolt_route(make_server_project):
    project = make_server_project(project_api_body=_API, urls_content=_URLS, templates=_TEMPLATES)
    with project.start() as server:
        body = server.wait_for_text("/render", 'href="/missions/42"')
    assert body == '<a href="/missions/42">go</a>'
