"""Mount a BoltAPI from Django's URLconf with ``path("prefix/", api.urls)``.

Bolt walks the resolver of the URLconf one time. It serves each route of a
mounted API at the path that Django's ``reverse()`` gives for it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import sys
import types
from contextlib import asynccontextmanager
from wsgiref.util import setup_testing_defaults

import pytest
from bolt_mcp import MCP, ProtectedResource
from django.conf.urls.i18n import i18n_patterns
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse, JsonResponse
from django.test import Client as DjangoClient
from django.urls import (
    NoReverseMatch,
    get_script_prefix,
    include,
    path,
    re_path,
    register_converter,
    resolve,
    reverse,
    set_script_prefix,
)
from django.utils.translation import gettext_lazy

from django_bolt import BoltAPI, WebSocket, _core
from django_bolt._urlconf import REVERSE_ONLY_URLCONF, UrlconfMount, join_mount_path, served_apis, urlconf_mounts
from django_bolt.management.commands.runbolt import Command
from django_bolt.openapi import OpenAPIConfig
from django_bolt.testing import TestClient, WebSocketTestClient
from django_bolt.urls import _LazyUrlpatterns

_names = itertools.count()


def _django_view(request, **kwargs):
    return HttpResponse("django")


@pytest.fixture
def make_urlconf():
    """Return a function that makes an importable URLconf module from ``urlpatterns``.

    Each module has a new name, so Django's resolver cache never gives an old resolver.
    """
    created: list[str] = []

    def make(urlpatterns: list) -> str:
        name = f"tests._urlconf_mount_{next(_names)}"
        module = types.ModuleType(name)
        module.urlpatterns = urlpatterns
        sys.modules[name] = module
        created.append(name)
        return name

    yield make
    for name in created:
        sys.modules.pop(name, None)


# A prefix with a path parameter cannot hold the docs.
_NO_DOCS = OpenAPIConfig(title="Missions", version="1.0.0", enabled=False)


def _api(namespace: str | None = None) -> BoltAPI:
    api = BoltAPI(namespace=namespace)

    @api.get("/{mission_id}", name="detail")
    async def detail(mission_id: int) -> dict:
        return {"id": mission_id}

    return api


class _OddConverter:
    regex = "[13579]+"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(_OddConverter, "odd_for_urlconf_mount_tests")


# --- The join -------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "route_path", "expected"),
    [
        ("missions/", "/{id}", "/missions/{id}"),
        ("missions/", "/", "/missions/"),
        ("missions/", "/list/", "/missions/list/"),
        ("org/{org}/missions/", "/{id}", "/org/{org}/missions/{id}"),
        ("", "/{id}", "/{id}"),
        ("", "/", "/"),
    ],
)
def test_join_mount_path(prefix, route_path, expected):
    assert join_mount_path(prefix, route_path) == expected


# --- The walk -------------------------------------------------------------


def test_a_urlconf_with_no_api_has_no_mount(make_urlconf):
    assert urlconf_mounts(make_urlconf([path("plain/", _django_view)])) == []


def test_the_prefix_of_a_mounted_api(make_urlconf):
    api = _api()
    urlconf = make_urlconf([path("missions/", api.urls)])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "missions/", "")]


def test_the_namespace_of_the_api_is_its_instance_namespace(make_urlconf):
    api = _api(namespace="missions")
    urlconf = make_urlconf([path("missions/", api.urls)])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "missions/", "missions")]


def test_a_mount_at_the_root_has_an_empty_prefix(make_urlconf):
    api = _api()

    assert urlconf_mounts(make_urlconf([path("", api.urls)])) == [UrlconfMount(api, "", "")]


def test_two_levels_of_include_join_prefixes_and_namespaces(make_urlconf):
    api = _api(namespace="api")
    app_patterns = ([path("api/", api.urls)], "myapp")
    urlconf = make_urlconf([path("myapp/", include(app_patterns))])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "myapp/api/", "myapp:api")]


def test_three_levels_of_include_join_prefixes_and_namespaces(make_urlconf):
    api = _api(namespace="api")
    inner = ([path("api/", api.urls)], "inner")
    outer = ([path("v1/", include(inner))], "outer")
    urlconf = make_urlconf([path("project/", include(outer))])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "project/v1/api/", "outer:inner:api")]


def test_an_include_with_an_instance_namespace_names_the_instance(make_urlconf):
    api = _api()
    urlconf = make_urlconf([path("live/", include(([path("", api.urls)], "app"), namespace="live"))])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "live/", "live")]


def test_two_apis_and_one_api_two_times_in_urlconf_order(make_urlconf):
    first, second = _api(namespace="first"), _api(namespace="second")
    urlconf = make_urlconf(
        [
            path("a/", first.urls),
            path("plain/", _django_view),
            path("b/", second.urls),
            path("c/", include(([path("", first.urls)], "again"))),
        ]
    )

    assert urlconf_mounts(urlconf) == [
        UrlconfMount(first, "a/", "first"),
        UrlconfMount(second, "b/", "second"),
        UrlconfMount(first, "c/", "again:first"),
    ]


@pytest.mark.parametrize("parameter", ["<org>", "<str:org>"])
def test_a_str_parameter_in_the_prefix_is_a_bolt_parameter(make_urlconf, parameter):
    api = _api()
    urlconf = make_urlconf([path(f"org/{parameter}/", api.urls)])

    assert urlconf_mounts(urlconf) == [UrlconfMount(api, "org/{org}/", "")]


@pytest.mark.parametrize(
    ("patterns", "message"),
    [
        pytest.param(
            lambda api: [path("org/<int:org>/", api.urls)],
            "Bolt cannot check the converter of '<int:org>'",
            id="int-converter",
        ),
        pytest.param(
            lambda api: [path("org/<slug:org>/", api.urls)],
            "Bolt cannot check the converter of '<slug:org>'",
            id="slug-converter",
        ),
        pytest.param(
            lambda api: [path("org/<odd_for_urlconf_mount_tests:org>/", api.urls)],
            "Bolt cannot check the converter",
            id="custom-converter",
        ),
        pytest.param(
            lambda api: [re_path(r"^missions/", api.urls)],
            "Mount the API with path(), not with re_path()",
            id="re-path",
        ),
        pytest.param(
            lambda api: [path("outer/", include([re_path(r"^v1/", include([path("", api.urls)]))]))],
            "cannot use the prefix '^v1/'",
            id="re-path-above",
        ),
        pytest.param(lambda api: [path("missions", api.urls)], "End the prefix with a slash", id="no-slash"),
        pytest.param(
            lambda api: [path("/missions/", api.urls)], "The prefix gives an empty path segment", id="leading-slash"
        ),
        pytest.param(lambda api: [path("a//", api.urls)], "The prefix gives an empty path segment", id="double-slash"),
        pytest.param(
            lambda api: [path("missions/", api.urls, {"source": "urlconf"})],
            "keyword arguments ['source']",
            id="kwargs",
        ),
        pytest.param(lambda api: [path("x{y}/", api.urls)], "Remove the braces", id="braces"),
        pytest.param(lambda api: [path(gettext_lazy("missions/"), api.urls)], "is translated", id="translated"),
    ],
)
def test_a_prefix_that_bolt_cannot_serve_is_refused(make_urlconf, patterns, message):
    urlconf = make_urlconf(patterns(_api()))

    with pytest.raises(ImproperlyConfigured, match=f"under .*{re.escape(message)}"):
        urlconf_mounts(urlconf)


def test_an_i18n_prefix_is_refused(make_urlconf, settings):
    settings.USE_I18N = True
    urlconf = make_urlconf(i18n_patterns(path("missions/", _api().urls)))

    with pytest.raises(ImproperlyConfigured, match="not with re_path\\(\\) or i18n_patterns\\(\\)"):
        urlconf_mounts(urlconf)


def test_a_bad_prefix_above_a_plain_django_view_is_not_refused(make_urlconf):
    urlconf = make_urlconf([re_path(r"^legacy/", include([path("<int:pk>/", _django_view)]))])

    assert urlconf_mounts(urlconf) == []


# --- Served paths -----------------------------------------------------------


@pytest.fixture
def mount_urlconf(make_urlconf, settings):
    """Return a function that makes a URLconf from ``urlpatterns`` and makes it the ROOT_URLCONF."""

    def make(urlpatterns: list) -> str:
        settings.ROOT_URLCONF = make_urlconf(urlpatterns)
        return settings.ROOT_URLCONF

    return make


def test_a_mounted_api_serves_at_the_prefixed_path_only(mount_urlconf):
    api = _api()
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/missions/42").json() == {"id": 42}
        assert client.get("/42").status_code == 404


def test_an_api_that_the_urlconf_does_not_mount_serves_its_own_paths(mount_urlconf):
    mounted, unmounted = _api(), _api()
    mount_urlconf([path("missions/", mounted.urls)])

    with TestClient(unmounted) as client:
        assert client.get("/42").json() == {"id": 42}
        assert client.get("/missions/42").status_code == 404


def test_two_apis_serve_at_their_own_prefixes(mount_urlconf):
    first, second = BoltAPI(), BoltAPI()

    @first.get("/who")
    async def first_who() -> dict:
        return {"route": "first"}

    @second.get("/who")
    async def second_who() -> dict:
        return {"route": "second"}

    mount_urlconf([path("a/", first.urls), path("b/", include(([path("v1/", second.urls)], "b")))])

    with TestClient(first) as client:
        assert client.get("/a/who").json() == {"route": "first"}
        assert client.get("/b/v1/who").status_code == 404
    with TestClient(second) as client:
        assert client.get("/b/v1/who").json() == {"route": "second"}
        assert client.get("/a/who").status_code == 404


def test_an_api_mounted_two_times_serves_under_each_prefix(mount_urlconf):
    api = _api()
    mount_urlconf([path("v1/", api.urls), path("latest/", include(([path("", api.urls)], "latest")))])

    with TestClient(api) as client:
        assert client.get("/v1/7").json() == {"id": 7}
        assert client.get("/latest/7").json() == {"id": 7}


@pytest.mark.parametrize(("trailing_slash", "route"), [("strip", "/"), ("strip", ""), ("append", "/"), ("keep", "/")])
def test_the_root_route_serves_at_the_prefix_with_its_slash_as_django_does(mount_urlconf, trailing_slash, route):
    api = BoltAPI(trailing_slash=trailing_slash)

    @api.get(route)
    async def index() -> dict:
        return {"route": "index"}

    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api, follow_redirects=False) as client:
        assert client.get("/missions/").json() == {"route": "index"}
        response = client.get("/missions?page=2")
        assert response.status_code == 308
        assert response.headers["location"] == "/missions/?page=2"


def test_a_route_with_a_trailing_slash_keeps_it_under_the_prefix(mount_urlconf):
    api = BoltAPI(trailing_slash="append")

    @api.get("/list")
    async def listing() -> dict:
        return {"route": "list"}

    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/missions/list/").json() == {"route": "list"}


def test_the_api_prefix_comes_after_the_urlconf_prefix(mount_urlconf):
    api = BoltAPI(prefix="/v2")

    @api.get("/{mission_id}")
    async def detail(mission_id: int) -> dict:
        return {"id": mission_id}

    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/missions/v2/5").json() == {"id": 5}


def test_a_prefix_parameter_is_in_request_params(mount_urlconf):
    api = BoltAPI(openapi_config=_NO_DOCS)

    @api.get("/{mission_id}")
    async def detail(request, mission_id: int) -> dict:
        return {"org": request.params["org"], "id": mission_id}

    mount_urlconf([path("org/<str:org>/missions/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/org/acme/missions/3").json() == {"org": "acme", "id": 3}


def test_a_handler_that_takes_a_prefix_parameter_as_an_argument_is_refused(mount_urlconf):
    api = BoltAPI()

    @api.get("/{mission_id}")
    async def detail(mission_id: int, org: str = "") -> dict:
        return {"id": mission_id}

    mount_urlconf([path("org/<org>/", api.urls)])

    with pytest.raises(ValueError, match="takes the parameter 'org' of the mount path"):
        TestClient(api)


def test_a_mounted_api_keeps_its_middleware_and_its_lifespan(mount_urlconf):
    events: list[str] = []

    class Tag:
        def __init__(self, get_response):
            self.get_response = get_response

        async def __call__(self, request):
            response = await self.get_response(request)
            response.headers["x-tag"] = "mounted"
            return response

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")

    api = BoltAPI(middleware=[Tag], lifespan=lifespan)

    @api.get("/ping")
    async def ping() -> dict:
        return {"route": "pong"}

    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        response = client.get("/missions/ping")
        missing = client.get("/missions/none")
    assert response.json() == {"route": "pong"}
    assert response.headers["x-tag"] == "mounted"
    assert missing.status_code == 404
    assert missing.headers["x-tag"] == "mounted"
    assert events == ["start", "stop"]


def test_runbolt_serves_a_found_and_mounted_api_once_and_a_mounted_api_that_it_did_not_find(mount_urlconf):
    found, mounted, unmounted = _api(namespace="found"), _api(namespace="mounted"), BoltAPI()

    @unmounted.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    mount_urlconf([path("found/", found.urls), path("mounted/", mounted.urls)])

    apis = served_apis([("project.api:api", unmounted), ("found.api:api", found)])
    merged = Command().merge_apis(apis)

    assert [label for label, _api in apis] == ["project.api:api", "found.api:api", "path('mounted/', api.urls)"]
    with TestClient(merged) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/found/1").json() == {"id": 1}
        assert client.get("/mounted/2").json() == {"id": 2}
        assert client.get("/1").status_code == 404


# --- Names and reverse() -------------------------------------------------


def test_reverse_gives_the_served_path(mount_urlconf):
    api = _api()
    mount_urlconf([path("missions/", api.urls)])

    url = reverse("detail", kwargs={"mission_id": 42})

    assert url == "/missions/42"
    with TestClient(api) as client:
        assert client.get(url).json() == {"id": 42}


def test_the_namespace_of_the_api_names_its_routes(mount_urlconf):
    api = _api(namespace="missions")
    mount_urlconf([path("missions/", api.urls)])

    assert reverse("missions:detail", kwargs={"mission_id": 42}) == "/missions/42"
    with pytest.raises(NoReverseMatch):
        reverse("detail", kwargs={"mission_id": 42})


@pytest.mark.parametrize(
    ("patterns", "name", "expected"),
    [
        pytest.param(
            lambda api: [path("myapp/", include(([path("api/", api.urls)], "myapp")))],
            "myapp:api:detail",
            "/myapp/api/7",
            id="two-levels",
        ),
        pytest.param(
            lambda api: [
                path("project/", include(([path("v1/", include(([path("api/", api.urls)], "inner")))], "outer")))
            ],
            "outer:inner:api:detail",
            "/project/v1/api/7",
            id="three-levels",
        ),
    ],
)
def test_reverse_through_nested_namespaces_gives_the_served_path(mount_urlconf, patterns, name, expected):
    api = _api(namespace="api")
    mount_urlconf(patterns(api))

    url = reverse(name, kwargs={"mission_id": 7})

    assert url == expected
    with TestClient(api) as client:
        assert client.get(url).json() == {"id": 7}


def test_the_same_route_name_in_two_namespaces(mount_urlconf):
    first, second = _api(namespace="first"), _api(namespace="second")
    mount_urlconf([path("a/", first.urls), path("b/", second.urls)])

    first_url = reverse("first:detail", kwargs={"mission_id": 1})
    second_url = reverse("second:detail", kwargs={"mission_id": 1})

    assert (first_url, second_url) == ("/a/1", "/b/1")
    with TestClient(first) as client:
        assert client.get(first_url).json() == {"id": 1}
    with TestClient(second) as client:
        assert client.get(second_url).json() == {"id": 1}


def test_a_prefix_parameter_reverses_from_its_kwarg(mount_urlconf):
    api = BoltAPI(namespace="missions", openapi_config=_NO_DOCS)

    @api.get("/{mission_id}", name="detail")
    async def detail(request, mission_id: int) -> dict:
        return {"org": request.params["org"], "id": mission_id}

    mount_urlconf([path("org/<str:org>/missions/", api.urls)])

    url = reverse("missions:detail", kwargs={"org": "acme", "mission_id": 3})

    assert url == "/org/acme/missions/3"
    with TestClient(api) as client:
        assert client.get(url).json() == {"org": "acme", "id": 3}


def test_a_django_view_next_to_a_mounted_api_reverses_and_resolves_by_the_same_rules(mount_urlconf):
    api = _api(namespace="api")
    app = ([path("api/", api.urls), path("page/<int:pk>/", _django_view, name="page")], "myapp")
    mount_urlconf([path("myapp/", include(app))])

    bolt_url = reverse("myapp:api:detail", kwargs={"mission_id": 3})
    django_url = reverse("myapp:page", kwargs={"pk": 3})

    assert (bolt_url, django_url) == ("/myapp/api/3", "/myapp/page/3/")
    bolt_match, django_match = resolve(bolt_url), resolve(django_url)
    assert (bolt_match.namespaces, bolt_match.url_name) == (["myapp", "api"], "detail")
    assert (django_match.namespaces, django_match.url_name) == (["myapp"], "page")
    assert DjangoClient().get(django_url).content == b"django"
    with TestClient(api) as client:
        assert client.get(bolt_url).json() == {"id": 3}


def test_a_route_of_a_mounted_sub_api_keeps_its_own_namespace(mount_urlconf):
    sub = _api(namespace="sub")
    api = BoltAPI(namespace="outer")
    api.mount("/sub", sub)
    mount_urlconf([path("missions/", api.urls)])

    url = reverse("outer:sub:detail", kwargs={"mission_id": 5})

    assert url == "/missions/sub/5"
    with TestClient(api) as client:
        assert client.get(url).json() == {"id": 5}


# --- django_bolt.urls ------------------------------------------------------

UNMOUNTED = BoltAPI()
MOUNTED = BoltAPI(namespace="mounted")


@UNMOUNTED.get("/own/{mission_id}", name="own_detail")
async def own_detail(mission_id: int) -> dict:
    return {"id": mission_id}


@MOUNTED.get("/{mission_id}", name="detail")
async def mounted_detail(mission_id: int) -> dict:
    return {"id": mission_id}


def _reverse_only_module() -> types.ModuleType:
    """A new module that works as ``django_bolt.urls``, with its own lazy patterns."""
    module = types.ModuleType(REVERSE_ONLY_URLCONF)
    module.urlpatterns = _LazyUrlpatterns()
    return module


def test_django_bolt_urls_leaves_out_an_api_that_the_urlconf_mounts(mount_urlconf, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED", f"{__name__}:MOUNTED"]
    mount_urlconf([path("", include(_reverse_only_module())), path("missions/", MOUNTED.urls)])

    assert reverse("own_detail", kwargs={"mission_id": 1}) == "/own/1"
    assert reverse("mounted:detail", kwargs={"mission_id": 1}) == "/missions/1"


def test_django_bolt_urls_under_a_prefix_is_refused(mount_urlconf):
    mount_urlconf([path("api/", include(_reverse_only_module()))])

    with pytest.raises(ImproperlyConfigured, match="includes django_bolt.urls under 'api/'"):
        TestClient(_api())


# --- Other route kinds ------------------------------------------------------


def test_the_docs_and_the_schema_paths_of_a_mounted_api_carry_the_prefix(mount_urlconf):
    api = _api()
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        schema = client.get("/missions/docs/openapi.json").json()
        page = client.get("/missions/docs/rapidoc").text
        unprefixed = client.get("/docs/openapi.json").status_code

    assert list(schema["paths"]) == ["/missions/{mission_id}"]
    assert 'spec-url="/missions/docs/openapi.json"' in page
    assert unprefixed == 404


def test_docs_under_a_prefix_with_a_parameter_are_refused(mount_urlconf):
    api = _api()
    mount_urlconf([path("org/<org>/", api.urls)])

    with pytest.raises(ImproperlyConfigured, match=r"OpenAPIConfig\(enabled=False\)"):
        TestClient(api)


@pytest.mark.asyncio
async def test_websocket_routes_of_a_mounted_api_carry_the_prefix(mount_urlconf):
    api = BoltAPI()

    @api.websocket("/echo")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    mount_urlconf([path("missions/", api.urls)])

    async with WebSocketTestClient(api, "/missions/echo") as ws:
        await ws.send_text("hi")
        assert await ws.receive_text() == "hi"
    with pytest.raises(ValueError, match="No WebSocket handler found for path: /echo"):
        async with WebSocketTestClient(api, "/echo"):
            pass


def test_an_asgi_mount_of_a_mounted_api_carries_the_prefix(mount_urlconf):
    async def files(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"files"})

    api = BoltAPI()
    api.mount_asgi("/files", files)
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/missions/files/a.txt").text == "files"
        assert client.get("/files/a.txt").status_code == 404


def test_an_mcp_server_of_a_mounted_api_carries_the_prefix(mount_urlconf):
    api = BoltAPI()
    api.mount_mcp(MCP("missions"))
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        prefixed = client.post("/missions/mcp", content=_MCP_INITIALIZE, headers=_MCP_HEADERS)
        unprefixed = client.post("/mcp", content=_MCP_INITIALIZE, headers=_MCP_HEADERS)

    assert prefixed.status_code == 200
    assert "mcp-session-id" in prefixed.headers
    assert unprefixed.status_code == 404


def test_an_oauth_protected_mcp_server_under_a_prefix_is_refused(mount_urlconf):
    api = BoltAPI()
    oauth = ProtectedResource(resource_url="https://example.com/mcp", token_verifier=lambda _token: None)
    api.mount_mcp(MCP("missions"), oauth=oauth)
    mount_urlconf([path("missions/", api.urls)])

    with pytest.raises(ImproperlyConfigured, match="OAuth-protected MCP server"):
        TestClient(api)


def test_reverse_of_a_websocket_route_gives_its_served_path(mount_urlconf):
    api = BoltAPI(namespace="live")

    @api.websocket("/feed/{channel}", name="feed")
    async def feed(websocket: WebSocket, channel: str) -> None:
        await websocket.close()

    mount_urlconf([path("missions/", api.urls)])

    assert reverse("live:feed", kwargs={"channel": "news"}) == "/missions/feed/news"


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
_MCP_INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
)


def test_a_native_route_of_a_mounted_api_carries_the_prefix(mount_urlconf):
    api = BoltAPI()
    api.native_route("GET", "/items/{id}", _core.native_test_capsule())
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        prefixed = client.get("/missions/items/7")
        unprefixed = client.get("/items/7")

    assert prefixed.status_code == 201
    assert (prefixed.json()["path"], prefixed.json()["params"]) == ("/missions/items/7", {"id": "7"})
    assert unprefixed.status_code == 404


# --- Script prefix (FORCE_SCRIPT_NAME) ----------------------------------------
# The proxy removes the script prefix from the path, as for Django's WSGIHandler.


def _request_facts(request, name: str, **kwargs) -> dict:
    return {
        "reverse": reverse(name, kwargs=kwargs),
        "path": request.path,
        "path_info": request.path_info,
        "SCRIPT_NAME": request.META["SCRIPT_NAME"],
        "PATH_INFO": request.META["PATH_INFO"],
        "full_path": request.get_full_path(),
        "absolute": request.build_absolute_uri(),
    }


def _django_facts_view(request, mission_id):
    return JsonResponse(_request_facts(request, "plain", mission_id=mission_id))


def _django_wsgi_get(path_info: str, query: str = "") -> tuple[int, bytes]:
    """GET through Django's own WSGIHandler, as a WSGI server behind a proxy that removed the script name."""
    environ = {"PATH_INFO": path_info, "QUERY_STRING": query, "SCRIPT_NAME": "", "SERVER_NAME": "testserver"}
    setup_testing_defaults(environ)
    statuses: list[str] = []
    # The handler sets the script prefix of this thread. Give the next test its own.
    previous = get_script_prefix()
    try:
        body = b"".join(WSGIHandler()(environ, lambda status, _headers, _exc_info=None: statuses.append(status)))
    finally:
        set_script_prefix(previous)
    return int(statuses[0].split()[0]), body


@pytest.mark.django_db
def test_a_bolt_request_under_force_script_name_matches_a_django_wsgi_request(mount_urlconf, settings):
    api = BoltAPI(namespace="missions")

    @api.get("/{mission_id}", name="detail")
    async def detail(request, mission_id: int) -> dict:
        return _request_facts(request, "missions:detail", mission_id=mission_id)

    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("missions/", api.urls), path("plain/<int:mission_id>", _django_facts_view, name="plain")])

    with TestClient(api) as client:
        bolt = client.get("/missions/7?x=1").json()
    status, body = _django_wsgi_get("/plain/7", "x=1")
    django = json.loads(body)

    assert status == 200
    assert django == {
        "reverse": "/myapp/plain/7",
        "path": "/myapp/plain/7",
        "path_info": "/plain/7",
        "SCRIPT_NAME": "/myapp",
        "PATH_INFO": "/plain/7",
        "full_path": "/myapp/plain/7?x=1",
        "absolute": "http://testserver/myapp/plain/7?x=1",
    }
    assert bolt == {key: value.replace("plain", "missions") for key, value in django.items()}


@pytest.mark.django_db
def test_an_unstripped_path_is_not_found_as_with_django_wsgi(mount_urlconf, settings):
    api = _api()
    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("missions/", api.urls), path("plain/<int:mission_id>", _django_facts_view, name="plain")])

    with TestClient(api) as client:
        assert client.get("/myapp/missions/7").status_code == 404
    assert _django_wsgi_get("/myapp/plain/7")[0] == 404


@pytest.mark.django_db
def test_reverse_has_the_script_prefix_in_each_kind_of_handler(mount_urlconf, settings):
    api = BoltAPI(namespace="m")

    @api.get("/sync", name="sync")
    def sync_handler() -> dict:
        return {"url": reverse("m:sync")}

    @api.get("/orm", name="orm")
    def orm_handler() -> dict:
        User.objects.count()
        return {"url": reverse("m:orm")}

    @api.get("/async", name="async")
    async def async_handler() -> dict:
        await asyncio.sleep(0)
        await User.objects.acount()
        return {"url": reverse("m:async")}

    settings.FORCE_SCRIPT_NAME = "/myapp/"
    mount_urlconf([path("m/", api.urls)])

    with TestClient(api) as client:
        urls = [client.get(f"/m/{kind}").json()["url"] for kind in ("sync", "orm", "async")]

    assert urls == ["/myapp/m/sync", "/myapp/m/orm", "/myapp/m/async"]


def test_an_unmounted_api_has_no_script_prefix_without_the_setting():
    api = BoltAPI()

    @api.get("/facts")
    async def facts(request) -> dict:
        return {"path": request.path, "path_info": request.path_info, "SCRIPT_NAME": request.META["SCRIPT_NAME"]}

    with TestClient(api) as client:
        assert client.get("/facts").json() == {"path": "/facts", "path_info": "/facts", "SCRIPT_NAME": ""}


def test_the_trailing_slash_redirect_has_the_script_prefix(mount_urlconf, settings):
    api = BoltAPI()

    @api.get("/list")
    async def listing() -> dict:
        return {}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api, follow_redirects=False) as client:
        response = client.get("/missions/list/?page=2")

    assert response.status_code == 308
    assert response.headers["location"] == "/myapp/missions/list?page=2"


def test_the_docs_fetch_the_schema_through_the_script_prefix(mount_urlconf, settings):
    api = _api()
    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("missions/", api.urls)])

    with TestClient(api) as client:
        schema = client.get("/missions/docs/openapi.json").json()
        page = client.get("/missions/docs/rapidoc").text

    assert schema["servers"] == [{"url": "/myapp"}]
    assert list(schema["paths"]) == ["/missions/{mission_id}"]
    assert 'spec-url="/myapp/missions/docs/openapi.json"' in page


def test_static_files_serve_at_the_static_url_without_the_script_prefix(settings, tmp_path):
    (tmp_path / "site.css").write_text("body {}")
    settings.FORCE_SCRIPT_NAME = "/myapp"
    settings.STATIC_URL = "/myapp/static/"
    settings.STATIC_ROOT = str(tmp_path)

    with TestClient(BoltAPI()) as client:
        assert client.get("/static/site.css").text == "body {}"


def test_the_script_prefix_of_one_client_does_not_stay_on_the_worker_threads(settings):
    def make_api() -> BoltAPI:
        api = BoltAPI()

        @api.get("/sync")
        def sync_prefix() -> dict:
            return {"prefix": get_script_prefix()}

        @api.get("/async")
        async def async_prefix() -> dict:
            return {"prefix": get_script_prefix()}

        return api

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(make_api()) as client:
        prefixed = {client.get(route).json()["prefix"] for route in ("/sync", "/async") for _ in range(3)}
    settings.FORCE_SCRIPT_NAME = None
    with TestClient(make_api()) as client:
        plain = {client.get(route).json()["prefix"] for route in ("/sync", "/async") for _ in range(6)}

    assert (prefixed, plain) == ({"/myapp/"}, {"/"})


@pytest.mark.django_db
def test_a_django_view_behind_mount_django_sees_the_paths_of_django_wsgi(mount_urlconf, settings):
    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("plain/<int:mission_id>", _django_facts_view, name="plain")])
    api = BoltAPI()
    # As the admin mount: the URLconf patterns have the mount path.
    api.mount_django("/plain", clear_root_path=True)

    with TestClient(api) as client:
        through_bolt = client.get("/plain/7?x=1").json()
    status, body = _django_wsgi_get("/plain/7", "x=1")

    assert status == 200
    assert through_bolt == json.loads(body)
