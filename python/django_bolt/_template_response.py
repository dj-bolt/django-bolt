"""Render a TemplateResponse after the view, as Django's handler does.

The serializers and the Django middleware adapter share this module. The
adapter imports the serializers, so this code cannot live in the adapter.
"""

from __future__ import annotations

from typing import Any

from django.template.response import SimpleTemplateResponse

# request.state key. The value is ``(exception_hooks, template_hooks, django_request, outer)``:
# the process_exception and process_template_response hooks of one Django layer,
# its Django request, and the value of the layer around it. Only a layer that
# has such a hook sets it. Each tuple of hooks holds the last middleware first.
DJANGO_HOOKS_KEY = "_django_middleware_hooks"


def render_template_response(response: SimpleTemplateResponse, request: Any) -> SimpleTemplateResponse:
    """Run process_template_response of each Django middleware, then render ``response``.

    Django calls the hooks of the last middleware first. ``request`` is the Bolt
    request. It holds the hooks of the Django layers of the route. A response
    with no Bolt request gets no hooks.
    """
    state = getattr(request, "state", None)
    layer = state.get(DJANGO_HOOKS_KEY) if state is not None else None
    while layer is not None:
        _, hooks, django_request, layer = layer
        for hook in hooks:
            response = hook(django_request, response)
            if response is None:
                # The message of Django's BaseHandler.check_response.
                name = getattr(hook, "__qualname__", hook)
                raise ValueError(f"{name} didn't return an HttpResponse object. It returned None instead.")
    # render() returns the response. A rendered response does not render again.
    return response.render()
