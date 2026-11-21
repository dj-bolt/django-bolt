"""
Tests for TemplateResponse and process_template_response of Django middleware (issue #868).

A handler returns a TemplateResponse. Bolt renders it after the view, as Django does.
Before the render, Bolt calls process_template_response of each Django middleware, the last one first.
"""

from __future__ import annotations

import pytest
from django.test import override_settings
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.responses import JSON, TemplateResponse
from django_bolt.testing import TestClient

_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "OPTIONS": {
            "context_processors": ["django.template.context_processors.request"],
            "loaders": [
                (
                    "django.template.loaders.locmem.Loader",
                    {
                        "page.html": "page:{{ title }}:{{ added|default:'-' }}",
                        "other.html": "other:{{ title }}:{{ added|default:'-' }}",
                        "path.html": "path:{{ request.path }}",
                    },
                )
            ],
        },
    }
]


@pytest.fixture(autouse=True, scope="module")
def _templates():
    with override_settings(TEMPLATES=_TEMPLATES):
        yield


_calls: list = []


class _TemplateHook(MiddlewareMixin):
    """Record process_template_response and process_response."""

    name = ""

    def process_template_response(self, request, response):
        _calls.append((self.name, "process_template_response", response.is_rendered))
        response.context_data["added"] = (response.context_data.get("added") or "") + self.name
        return response

    def process_response(self, request, response):
        _calls.append((self.name, "process_response", response.content.decode()))
        return response


class _FirstHook(_TemplateHook):
    name = "first"


class _SecondHook(_TemplateHook):
    name = "second"

    def process_template_response(self, request, response):
        response = super().process_template_response(request, response)
        response.template_name = "other.html"
        return response


class _TemplateOnlyHook(MiddlewareMixin):
    """A middleware with process_template_response and no other hook."""

    def process_template_response(self, request, response):
        _calls.append(("template-only", "process_template_response", response.is_rendered))
        response.context_data["added"] = "template-only"
        return response


class _CallStyleHook:
    """A middleware with ``__call__`` and process_template_response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        _calls.append(("call-style", "process_template_response", response.is_rendered))
        response.context_data["added"] = "call-style"
        return response


class _NoneHook(MiddlewareMixin):
    def process_template_response(self, request, response):
        return None


class _ViewAnswerHook(MiddlewareMixin):
    """process_view answers with a TemplateResponse, as a view can."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        return TemplateResponse(request, "page.html", {"title": "from-view"})


class _ExceptionAnswerHook(MiddlewareMixin):
    """process_exception answers with a TemplateResponse."""

    def process_exception(self, request, exception):
        return TemplateResponse(request, "page.html", {"title": str(exception)}, status=503)


def _layout(layout, classes):
    if layout == "wrappers":
        return [DjangoMiddleware(cls) for cls in classes]
    return [DjangoMiddlewareStack(list(classes))]


def _api(middleware=None, kind="async"):
    api = BoltAPI(middleware=middleware or [])
    if kind == "sync":

        @api.get("/page")
        def page(request):
            return TemplateResponse(request, "page.html", {"title": "T"})

        @api.get("/created")
        def created(request):
            return TemplateResponse(request, "page.html", {"title": "C"}, status=201, headers={"X-Kind": "t"})

        @api.get("/json")
        def json_route():
            return JSON({"ok": True})

    else:

        @api.get("/page")
        async def page(request):
            return TemplateResponse(request, "page.html", {"title": "T"})

        @api.get("/created")
        async def created(request):
            return TemplateResponse(request, "page.html", {"title": "C"}, status=201, headers={"X-Kind": "t"})

        @api.get("/json")
        async def json_route():
            return JSON({"ok": True})

    @api.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @api.get("/path")
    async def path(request):
        return TemplateResponse(request, "path.html")

    return api


@pytest.mark.parametrize("kind", ["sync", "async"])
class TestTemplateResponseWithoutMiddleware:
    def test_the_route_renders_the_template(self, kind):
        with TestClient(_api(kind=kind)) as client:
            response = client.get("/page")

        assert response.status_code == 200
        assert response.text == "page:T:-"
        assert response.headers["content-type"] == "text/html; charset=utf-8"

    def test_the_status_and_the_headers_stay(self, kind):
        with TestClient(_api(kind=kind)) as client:
            response = client.get("/created")

        assert response.status_code == 201
        assert response.text == "page:C:-"
        assert response.headers["x-kind"] == "t"

    def test_the_template_gets_the_request(self, kind):
        with TestClient(_api(kind=kind)) as client:
            assert client.get("/path").text == "path:/path"


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("layout", ["stack", "wrappers"])
class TestProcessTemplateResponse:
    def test_the_hooks_run_in_reverse_order_before_the_render(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _SecondHook)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        assert response.status_code == 200
        # "second" changes the template. Each hook adds its name to the context.
        assert response.text == "other:T:secondfirst"
        # As in Django, process_response gets the rendered response.
        assert _calls == [
            ("second", "process_template_response", False),
            ("first", "process_template_response", False),
            ("second", "process_response", "other:T:secondfirst"),
            ("first", "process_response", "other:T:secondfirst"),
        ]

    def test_the_status_and_the_headers_stay(self, layout, kind):
        api = _api(_layout(layout, (_FirstHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/created")

        assert response.status_code == 201
        assert response.text == "page:C:first"
        assert response.headers["x-kind"] == "t"
        assert response.headers["content-type"] == "text/html; charset=utf-8"

    def test_the_hooks_do_not_run_for_another_response(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/json")

        assert response.json() == {"ok": True}
        assert _calls == [("first", "process_response", '{"ok":true}')]

    def test_a_middleware_with_only_the_template_hook(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_TemplateOnlyHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        assert response.text == "page:T:template-only"
        assert _calls == [("template-only", "process_template_response", False)]

    def test_a_call_style_middleware_gets_the_hook(self, layout, kind):
        """A stack of hook and ``__call__`` middleware uses the compatibility chain."""
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _CallStyleHook)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        # "call-style" runs first and sets the value. "first" adds its name.
        assert response.text == "page:T:call-stylefirst"
        assert _calls[:2] == [
            ("call-style", "process_template_response", False),
            ("first", "process_template_response", False),
        ]

    def test_a_hook_that_returns_none_is_an_error(self, layout, kind, caplog):
        api = _api(_layout(layout, (_NoneHook,)), kind)

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.get("/page")

        assert response.status_code == 500
        # The message of Django's handler.
        assert "_NoneHook.process_template_response didn't return an HttpResponse object" in caplog.text

    def test_the_hooks_run_for_the_response_of_process_view(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _ViewAnswerHook)), kind)

        with TestClient(api) as client:
            response = client.get("/json")

        assert response.text == "page:from-view:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_response", "page:from-view:first"),
        ]

    def test_the_hooks_run_for_the_response_of_process_exception(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _ExceptionAnswerHook)), kind)

        with TestClient(api) as client:
            response = client.get("/boom")

        assert response.status_code == 503
        assert response.text == "page:boom:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_response", "page:boom:first"),
        ]
