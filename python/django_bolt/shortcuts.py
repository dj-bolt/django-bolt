from __future__ import annotations

from django.template import loader

from .enums import MediaType
from .responses import Response

# The content type of HTML(), and of render() in Django.
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"


def render(
    request,
    template_name: str,
    context=None,
    content_type: str | MediaType = _HTML_CONTENT_TYPE,
    status=200,
    using=None,
):
    """
    Render a Django template to a Response.

    The arguments have the meaning that they have in django.shortcuts.render().
    The default content type is "text/html; charset=utf-8", as for HTML().
    Bolt sends a content_type that you pass with no change.
    """
    content = loader.render_to_string(template_name, context, request, using=using)
    return Response(content=content, media_type=content_type, status_code=status)
