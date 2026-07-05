"""App under test for Bolt URL reversing via Django's ``{% url %}`` tag.

A named Bolt route plus a route that renders a template using ``{% url %}``,
proving ``reverse()`` resolves Bolt route names end-to-end. The project wires
``path("", include("django_bolt.urls"))`` in its ROOT_URLCONF.
"""

from __future__ import annotations

from django.template import Context, Template

from django_bolt import BoltAPI
from django_bolt.responses import HTML

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/missions/{mission_id}", name="get_mission")
async def get_mission(mission_id: int):
    return {"id": mission_id}


@api.get("/render")
async def render_url_tag():
    html = Template("""<a href="{% url 'get_mission' mission_id=42 %}">go</a>""").render(Context())
    return HTML(html)
