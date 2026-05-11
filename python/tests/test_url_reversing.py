"""Tests for Bolt URL reversing (issue #114)."""

from django.template import Context, Template

from django_bolt import BoltAPI


def test_bolt_url_template_tag_resolves_route():
    """Test that bolt_url template tag resolves to correct URLs."""
    api = BoltAPI()

    @api.get("/user/{id}", name="user-detail")
    async def get_user(id: int):
        return {"id": id}

    # --- ACT ---
    template = Template("{% load bolt_tags %}{% bolt_url 'user-detail' id=123 %}")
    context = Context({})
    result = template.render(context)

    # --- ASSERT ---
    assert result == "/user/123"
