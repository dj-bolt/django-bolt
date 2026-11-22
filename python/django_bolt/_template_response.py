"""Render a TemplateResponse after the handler, as Django's handler does.

The serializers and the Django middleware adapter share this module. The
adapter imports the serializers, so this code cannot live in the adapter.
"""

from __future__ import annotations

from contextvars import ContextVar

from django.template.response import SimpleTemplateResponse

# The value is ``((exception_hooks, template_hooks), django_request, outer)``:
# the process_exception and process_template_response hooks of one Django layer,
# its Django request, and the value of the layer around it. Only a layer that
# has such a hook sets it. Each tuple of hooks holds the last middleware first.
DJANGO_HOOKS: ContextVar[tuple | None] = ContextVar("django_bolt_django_hooks", default=None)

# The attribute that marks an error of process_template_response.
HOOK_ERROR_ATTR = "_bolt_template_hook_error"


def render_template_response(response: SimpleTemplateResponse) -> SimpleTemplateResponse:
    """Run process_template_response of each Django middleware, then render ``response``.

    Django calls the hooks of the last middleware first.
    """
    layer = DJANGO_HOOKS.get()
    try:
        while layer is not None:
            (_, hooks), django_request, layer = layer
            for hook in hooks:
                response = hook(django_request, response)
                if response is None:
                    # The message of Django's BaseHandler.check_response.
                    name = f"{type(hook.__self__).__name__}.process_template_response"
                    raise ValueError(f"{name} didn't return an HttpResponse object. It returned None instead.")
    except Exception as exc:
        # Django calls the hooks outside the try of process_exception.
        setattr(exc, HOOK_ERROR_ATTR, True)
        raise
    # render() returns the response. A rendered response does not render again.
    return response.render()
