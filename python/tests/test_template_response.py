"""
Tests for TemplateResponse and process_template_response of Django middleware (issue #868).

A handler returns a TemplateResponse. Bolt renders it after the handler, as Django does.
Before the render, Bolt calls process_template_response of each Django middleware, the last one first.
"""

from __future__ import annotations

import pytest
from django.http import HttpResponse
from django.template.response import SimpleTemplateResponse
from django.test import override_settings
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.responses import JSON, TemplateResponse
from django_bolt.testing import TestClient

from .test_models import Article

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
                        "list.html": "{% for article in articles %}{{ article.title }};{% endfor %}",
                    },
                )
            ],
        },
    }
]

_KINDS = pytest.mark.parametrize("kind", ["sync", "async"])


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


class _AsyncHook(MiddlewareMixin):
    async def process_template_response(self, request, response):
        response.context_data["added"] = "async"
        return response


class _NoneHook(MiddlewareMixin):
    def process_template_response(self, request, response):
        return None


class _InheritedNoneHook(_NoneHook):
    """The error message names this class, as in Django."""


class _RaisingHook(MiddlewareMixin):
    def process_template_response(self, request, response):
        raise RuntimeError("hook")


class _ViewAnswerHook(MiddlewareMixin):
    """process_view answers with a TemplateResponse, as a handler can."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        return TemplateResponse(request, "page.html", {"title": "from-view"})


class _ExceptionRecordHook(MiddlewareMixin):
    """Record process_exception."""

    def process_exception(self, request, exception):
        _calls.append(("record", "process_exception", type(exception).__name__))


class _ExceptionAnswerHook(MiddlewareMixin):
    """process_exception answers with a TemplateResponse."""

    def process_exception(self, request, exception):
        return TemplateResponse(request, "page.html", {"title": str(exception)}, status=503)


def _layout(layout, classes):
    if layout == "none":
        return []
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

        @api.get("/articles")
        def articles(request):
            return TemplateResponse(request, "list.html", {"articles": Article.objects.all()})

    else:

        @api.get("/page")
        async def page(request):
            return TemplateResponse(request, "page.html", {"title": "T"})

        @api.get("/created")
        async def created(request):
            return TemplateResponse(request, "page.html", {"title": "C"}, status=201, headers={"X-Kind": "t"})

        @api.get("/articles")
        async def articles(request):
            return TemplateResponse(request, "list.html", {"articles": Article.objects.all()})

    @api.get("/json")
    async def json_route():
        return JSON({"ok": True})

    @api.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @api.get("/path")
    async def path(request):
        return TemplateResponse(request, "path.html")

    @api.get("/missing")
    async def missing(request):
        return TemplateResponse(request, "missing.html", {})

    @api.get("/simple")
    async def simple():
        return SimpleTemplateResponse("page.html", {"title": "S"})

    @api.get("/cookie")
    async def cookie(request):
        response = HttpResponse("cookie")
        response.set_cookie("plain", "1", httponly=True)
        return response

    @api.get("/template-cookie")
    async def template_cookie(request):
        response = TemplateResponse(request, "page.html", {"title": "T"})
        response.set_cookie("template", "2", max_age=60)
        return response

    return api


class TestTemplateResponseWithoutMiddleware:
    @_KINDS
    def test_the_route_renders_the_template(self, kind):
        with TestClient(_api(kind=kind)) as client:
            response = client.get("/page")

        assert response.status_code == 200
        assert response.text == "page:T:-"
        assert response.headers["content-type"] == "text/html; charset=utf-8"

    @_KINDS
    def test_the_status_and_the_headers_stay(self, kind):
        with TestClient(_api(kind=kind)) as client:
            response = client.get("/created")

        assert response.status_code == 201
        assert response.text == "page:C:-"
        assert response.headers["x-kind"] == "t"

    def test_the_template_gets_the_request(self):
        with TestClient(_api()) as client:
            assert client.get("/path").text == "path:/path"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("layout", ["none", "stack", "wrappers"])
@_KINDS
def test_the_template_reads_a_queryset(layout, kind):
    Article.objects.create(title="A1", content="c", author="a")

    with TestClient(_api(_layout(layout, (_FirstHook,)), kind)) as client:
        response = client.get("/articles")

    assert response.status_code == 200, response.text
    assert response.text == "A1;"


@pytest.mark.parametrize("layout", ["none", "stack", "wrappers"])
@pytest.mark.parametrize(
    ("path", "start", "attribute"),
    [("/cookie", "plain=1;", "HttpOnly"), ("/template-cookie", "template=2;", "Max-Age=60")],
)
def test_a_django_response_sends_its_cookies(layout, path, start, attribute):
    with TestClient(_api(_layout(layout, (_TemplateOnlyHook,)))) as client:
        response = client.get(path)

    assert response.status_code == 200
    [cookie] = response.headers.get_list("set-cookie")
    assert cookie.startswith(start)
    assert attribute in cookie


@pytest.mark.parametrize("layout", ["stack", "wrappers"])
class TestProcessTemplateResponse:
    @_KINDS
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

    @_KINDS
    def test_the_status_and_the_headers_stay(self, layout, kind):
        api = _api(_layout(layout, (_FirstHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/created")

        assert response.status_code == 201
        assert response.text == "page:C:first"
        assert response.headers["x-kind"] == "t"
        assert response.headers["content-type"] == "text/html; charset=utf-8"

    @_KINDS
    def test_a_middleware_with_only_the_template_hook(self, layout, kind):
        _calls.clear()
        api = _api(_layout(layout, (_TemplateOnlyHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        assert response.text == "page:T:template-only"
        assert _calls == [("template-only", "process_template_response", False)]

    @_KINDS
    def test_a_call_style_middleware_gets_the_hook(self, layout, kind):
        """A ``__call__`` middleware gets the hook. In a stack, the mix uses the compatibility chain."""
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _CallStyleHook)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        # "call-style" runs first and sets the value. "first" adds its name.
        assert response.text == "page:T:call-stylefirst"
        assert _calls == [
            ("call-style", "process_template_response", False),
            ("first", "process_template_response", False),
            ("first", "process_response", "page:T:call-stylefirst"),
        ]

    @_KINDS
    def test_an_async_hook_runs(self, layout, kind):
        api = _api(_layout(layout, (_AsyncHook,)), kind)

        with TestClient(api) as client:
            response = client.get("/page")

        assert response.status_code == 200, response.text
        assert response.text == "page:T:async"

    def test_a_simple_template_response_gets_the_hooks(self, layout):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook,)))

        with TestClient(api) as client:
            response = client.get("/simple")

        assert response.text == "page:S:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_response", "page:S:first"),
        ]

    @_KINDS
    def test_a_hook_that_returns_none_is_an_error(self, layout, kind, caplog):
        api = _api(_layout(layout, (_InheritedNoneHook,)), kind)

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.get("/page")

        assert response.status_code == 500
        # The message of Django's handler.
        assert "_InheritedNoneHook.process_template_response didn't return an HttpResponse object" in caplog.text

    @pytest.mark.parametrize("hook", [_NoneHook, _RaisingHook])
    def test_process_exception_does_not_get_an_error_of_a_hook(self, layout, hook):
        # Django calls the hooks outside the try of process_exception.
        _calls.clear()
        api = _api(_layout(layout, (_ExceptionRecordHook, hook)))

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.get("/page")

        assert response.status_code == 500
        assert _calls == []

    def test_process_exception_gets_an_error_of_the_render(self, layout):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _ExceptionAnswerHook)))

        with TestClient(api) as client:
            response = client.get("/missing")

        assert response.status_code == 503
        # The TemplateDoesNotExist message is the name of the template.
        assert response.text == "page:missing.html:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_template_response", False),
            ("first", "process_response", "page:missing.html:first"),
        ]

    def test_the_hooks_run_for_the_response_of_process_view(self, layout):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _ViewAnswerHook)))

        with TestClient(api) as client:
            response = client.get("/json")

        assert response.text == "page:from-view:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_response", "page:from-view:first"),
        ]

    def test_the_hooks_run_for_the_response_of_process_exception(self, layout):
        _calls.clear()
        api = _api(_layout(layout, (_FirstHook, _ExceptionAnswerHook)))

        with TestClient(api) as client:
            response = client.get("/boom")

        assert response.status_code == 503
        assert response.text == "page:boom:first"
        assert _calls == [
            ("first", "process_template_response", False),
            ("first", "process_response", "page:boom:first"),
        ]


def test_a_django_middleware_outside_a_stack_gets_the_hook():
    _calls.clear()
    api = _api([DjangoMiddleware(_FirstHook), DjangoMiddlewareStack([_SecondHook])])

    with TestClient(api) as client:
        response = client.get("/page")

    assert response.text == "other:T:secondfirst"
    assert _calls == [
        ("second", "process_template_response", False),
        ("first", "process_template_response", False),
        ("second", "process_response", "other:T:secondfirst"),
        ("first", "process_response", "other:T:secondfirst"),
    ]
