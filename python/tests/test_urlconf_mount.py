"""Mount a BoltAPI from Django's URLconf with ``path("prefix/", api.urls)``.

Bolt walks the resolver of the URLconf one time. It serves each route of a
mounted API at the path that Django's ``reverse()`` gives for it.
"""

from __future__ import annotations

import importlib
import itertools
import json
import re
import sys
import types
from contextlib import asynccontextmanager
from typing import Annotated

import pytest
from bolt_mcp import MCP, ProtectedResource
from django.conf.urls.i18n import i18n_patterns
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client as DjangoClient
from django.test import override_settings
from django.urls import NoReverseMatch, include, path, re_path, register_converter, resolve, reverse
from django.utils.translation import gettext_lazy

import django_bolt.urls
from django_bolt import BoltAPI, Depends, WebSocket, _core
from django_bolt._urlconf import UrlconfMount, join_mount_path, served_apis, urlconf_mounts
from django_bolt.management.commands.runbolt import Command
from django_bolt.middleware import rate_limit
from django_bolt.openapi import OpenAPIConfig
from django_bolt.params import Path, Query
from django_bolt.testing import TestClient, WebSocketTestClient

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
            "Use path() with a static prefix, not re_path()",
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
        pytest.param(
            lambda api: [path("", api.urls, {"source": "urlconf"})], "keyword arguments ['source']", id="kwargs-at-root"
        ),
        pytest.param(lambda api: [path("x{y}/", api.urls)], "Remove the braces", id="braces"),
        pytest.param(
            lambda api: [path("<a>-<b>/", api.urls)],
            "The Bolt router allows one parameter in a path segment, and '{a}-{b}' has more",
            id="two-parameters-in-a-segment",
        ),
        pytest.param(
            lambda api: [path("<x>/<x>/", api.urls)], "The prefix has the parameter 'x' two times", id="repeated"
        ),
        pytest.param(
            lambda api: [path("<x>/", include([path("<x>/", api.urls)]))],
            "The prefix has the parameter 'x' two times",
            id="repeated-across-includes",
        ),
        pytest.param(lambda api: [path(gettext_lazy("missions/"), api.urls)], "is translated", id="translated"),
    ],
)
def test_a_prefix_that_bolt_cannot_serve_is_refused(make_urlconf, patterns, message):
    urlconf = make_urlconf(patterns(_api()))

    with pytest.raises(ImproperlyConfigured, match=f"The URLconf mounts a BoltAPI at .*{re.escape(message)}"):
        urlconf_mounts(urlconf)


def test_a_refused_prefix_names_each_urlconf_entry_above_the_api(make_urlconf):
    api = _api()
    urlconf = make_urlconf([path("outer/", include([re_path(r"^v1/", include([path("", api.urls)]))]))])

    entries = "path('outer/', include(...)) > re_path('^v1/', include(...)) > path('', api.urls)"
    with pytest.raises(ImproperlyConfigured, match=re.escape(f"The URLconf mounts a BoltAPI at {entries}.")):
        urlconf_mounts(urlconf)


def test_an_i18n_prefix_is_refused(make_urlconf, settings):
    settings.USE_I18N = True
    urlconf = make_urlconf(i18n_patterns(path("missions/", _api().urls)))

    with pytest.raises(
        ImproperlyConfigured, match=re.escape("The prefix of i18n_patterns() changes with the language")
    ):
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


def _org(org: str) -> str:
    return org


def test_a_dependency_that_takes_a_prefix_parameter_as_an_argument_is_refused(mount_urlconf):
    api = BoltAPI(openapi_config=_NO_DOCS)

    @api.get("/x")
    async def handler(org: Annotated[str, Depends(_org)]) -> dict:
        return {"org": org}

    mount_urlconf([path("org/<org>/", api.urls)])

    with pytest.raises(
        ValueError, match=r"A dependency of the handler of GET /x takes the parameter 'org' of the mount path"
    ):
        TestClient(api)


def _org_from_the_query(org: Annotated[str, Query()]) -> str:
    return org


def _org_from_the_path(org: Annotated[str, Path()]) -> str:
    return org


def test_a_marker_on_a_prefix_parameter_reads_its_own_source(mount_urlconf):
    # An explicit marker reads its source on each route, so a mount does not change what it reads.
    api = BoltAPI(openapi_config=_NO_DOCS)

    @api.get("/x")
    async def from_the_query(
        org: Annotated[str, Query()],
        dependency_query: Annotated[str, Depends(_org_from_the_query)],
        dependency_path: Annotated[str, Depends(_org_from_the_path)],
    ) -> dict:
        return {"query": org, "dependency_query": dependency_query, "dependency_path": dependency_path}

    @api.get("/y")
    async def from_the_path(org: Annotated[str, Path()]) -> dict:
        return {"path": org}

    mount_urlconf([path("org/<org>/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/org/acme/x?org=other").json() == {
            "query": "other",
            "dependency_query": "other",
            "dependency_path": "acme",
        }
        assert client.get("/org/acme/y?org=other").json() == {"path": "acme"}


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


def _named_detail(api: BoltAPI, route: str, owner: str, *, explicit: bool) -> None:
    """Add a route with the derived name ``detail``, or with ``detail`` as its explicit name."""

    @api.get(route, **({"name": "detail"} if explicit else {}))
    async def detail(pk: int) -> dict:
        return {owner: pk}


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-names", "derived-names"])
def test_a_mounted_sub_api_with_the_namespace_of_its_parent_keeps_its_names(mount_urlconf, explicit):
    parent, sub = BoltAPI(namespace="api"), BoltAPI(namespace="api")
    _named_detail(parent, "/p/{pk}", "parent", explicit=explicit)
    _named_detail(sub, "/c/{pk}", "sub", explicit=explicit)
    parent.mount("/sub", sub)
    mount_urlconf([path("blog/", include(([path("", parent.urls)], "blog")))])

    parent_url = reverse("blog:api:detail", kwargs={"pk": 1})
    sub_url = reverse("blog:api:api:detail", kwargs={"pk": 2})

    assert (parent_url, sub_url) == ("/blog/p/1", "/blog/sub/c/2")
    with TestClient(parent) as client:
        assert client.get(parent_url).json() == {"parent": 1}
        assert client.get(sub_url).json() == {"sub": 2}


def test_each_level_of_mount_adds_the_namespace_of_its_api(mount_urlconf):
    outer, middle, inner = BoltAPI(namespace="outer"), BoltAPI(namespace="middle"), BoltAPI(namespace="inner")
    _named_detail(middle, "/m/{pk}", "middle", explicit=False)
    _named_detail(inner, "/i/{pk}", "inner", explicit=False)
    middle.mount("/in", inner)
    outer.mount("/mid", middle)
    mount_urlconf([path("blog/", outer.urls)])

    assert reverse("outer:middle:detail", kwargs={"pk": 1}) == "/blog/mid/m/1"
    assert reverse("outer:middle:inner:detail", kwargs={"pk": 2}) == "/blog/mid/in/i/2"


# --- django_bolt.urls ------------------------------------------------------

UNMOUNTED = BoltAPI()
MOUNTED = BoltAPI(namespace="mounted")


@UNMOUNTED.get("/own/{mission_id}", name="own_detail")
async def own_detail(mission_id: int) -> dict:
    return {"id": mission_id}


@MOUNTED.get("/{mission_id}", name="detail")
async def mounted_detail(mission_id: int) -> dict:
    return {"id": mission_id}


@pytest.fixture
def fresh_bolt_urls():
    """Import ``django_bolt.urls`` again, so that its patterns read the settings of the test."""
    yield importlib.reload(django_bolt.urls)
    importlib.reload(django_bolt.urls)


def test_django_bolt_urls_leaves_out_an_api_that_the_urlconf_mounts(fresh_bolt_urls, mount_urlconf, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED", f"{__name__}:MOUNTED"]
    mount_urlconf([path("", include("django_bolt.urls")), path("missions/", MOUNTED.urls)])

    assert reverse("own_detail", kwargs={"mission_id": 1}) == "/own/1"
    assert reverse("mounted:detail", kwargs={"mission_id": 1}) == "/missions/1"


@pytest.mark.parametrize(
    ("patterns", "url"),
    [
        pytest.param(lambda: [path("api/", include("django_bolt.urls"))], "/api/own/1", id="prefix"),
        pytest.param(
            lambda: [path("v1/", include([path("api/", include("django_bolt.urls"))]))], "/v1/api/own/1", id="nested"
        ),
        pytest.param(lambda: [re_path(r"^", include("django_bolt.urls"))], "/own/1", id="re-path-caret"),
        # Bolt ignores the keyword arguments of this include, as before.
        pytest.param(lambda: [path("api/", include("django_bolt.urls"), {"flag": 1})], "/api/own/1", id="kwargs"),
    ],
)
def test_the_apis_of_django_bolt_urls_serve_at_the_reversed_path(
    fresh_bolt_urls, mount_urlconf, settings, patterns, url
):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED"]
    mount_urlconf(patterns())

    assert reverse("own_detail", kwargs={"mission_id": 1}) == url
    with TestClient(UNMOUNTED) as client:
        assert client.get(url).json() == {"id": 1}
        if url != "/own/1":
            assert client.get("/own/1").status_code == 404


def test_runbolt_serves_the_apis_of_django_bolt_urls_under_its_prefixes(fresh_bolt_urls, mount_urlconf, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED", f"{__name__}:MOUNTED"]
    mount_urlconf(
        [
            path("", include("django_bolt.urls")),
            path("api/", include("django_bolt.urls")),
            path("missions/", MOUNTED.urls),
        ]
    )

    merged = Command().merge_apis(served_apis(Command().autodiscover_apis()))

    with TestClient(merged) as client:
        assert client.get("/own/1").json() == {"id": 1}
        assert client.get("/api/own/1").json() == {"id": 1}
        # An API that api.urls mounts serves under its own prefix only.
        assert client.get("/missions/2").json() == {"id": 2}
        assert client.get("/api/2").status_code == 404


def test_django_bolt_urls_follows_a_change_of_the_root_urlconf(fresh_bolt_urls, make_urlconf, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED", f"{__name__}:MOUNTED"]
    mounting = make_urlconf([path("missions/", MOUNTED.urls), path("api/", include("django_bolt.urls"))])
    including = make_urlconf([path("api/", include("django_bolt.urls"))])

    with override_settings(ROOT_URLCONF=mounting):
        assert reverse("mounted:detail", kwargs={"mission_id": 1}) == "/missions/1"
        with TestClient(MOUNTED) as client:
            assert client.get("/missions/1").json() == {"id": 1}
    with override_settings(ROOT_URLCONF=including):
        assert reverse("mounted:detail", kwargs={"mission_id": 1}) == "/api/1"
        with TestClient(MOUNTED) as client:
            assert client.get("/api/1").json() == {"id": 1}
            assert client.get("/1").status_code == 404


def test_an_api_that_django_bolt_urls_does_not_name_serves_its_own_paths(fresh_bolt_urls, mount_urlconf, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED"]
    mount_urlconf([path("api/", include("django_bolt.urls"))])

    with TestClient(_api()) as client:
        assert client.get("/42").json() == {"id": 42}


@pytest.mark.parametrize(
    ("error", "where"),
    [
        # Django reads a urlpatterns attribute with a default, which would hide an AttributeError.
        pytest.param(AttributeError, "reverse", id="attribute-error-reverse"),
        pytest.param(AttributeError, "check", id="attribute-error-check"),
        # A sequence ends its iteration at an IndexError, which would hide it.
        pytest.param(IndexError, "reverse", id="index-error-reverse"),
        pytest.param(IndexError, "check", id="index-error-check"),
    ],
)
def test_an_error_of_autodiscovery_in_django_bolt_urls_is_not_hidden(
    fresh_bolt_urls, mount_urlconf, settings, tmp_path, monkeypatch, error, where
):
    module = f"{error.__name__.lower()}_api"
    (tmp_path / f"{module}.py").write_text(f"raise {error.__name__}('a typo in the api module')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)
    settings.BOLT_API = [f"{module}:api"]
    mount_urlconf([path("", include("django_bolt.urls"))])

    with pytest.raises(error, match="a typo in the api module"):
        if where == "reverse":
            reverse("own_detail", kwargs={"mission_id": 1})
        else:
            call_command("check")


@pytest.mark.parametrize(
    ("patterns", "entry", "message"),
    [
        pytest.param(
            lambda: [re_path(r"^api/", include("django_bolt.urls"))],
            "re_path('^api/', include('django_bolt.urls'))",
            "Use path() with a static prefix, not re_path().",
            id="re-path",
        ),
        pytest.param(
            lambda: i18n_patterns(path("", include("django_bolt.urls")), prefix_default_language=False),
            "i18n_patterns(include(...)) > path('', include('django_bolt.urls'))",
            "The prefix of i18n_patterns() changes with the language",
            id="i18n",
        ),
        pytest.param(
            lambda: [path("api", include("django_bolt.urls"))],
            "path('api', include('django_bolt.urls'))",
            "End the prefix with a slash",
            id="no-slash",
        ),
        pytest.param(
            lambda: [path("<int:v>/", include("django_bolt.urls"))],
            "path('<int:v>/', include('django_bolt.urls'))",
            "Bolt cannot check the converter of '<int:v>'",
            id="int-converter",
        ),
    ],
)
def test_a_prefix_of_django_bolt_urls_that_bolt_cannot_serve_names_the_entry(
    fresh_bolt_urls, mount_urlconf, settings, patterns, entry, message
):
    settings.USE_I18N = True
    mount_urlconf(patterns())

    with pytest.raises(ImproperlyConfigured, match=re.escape(f"includes django_bolt.urls at {entry}.")) as raised:
        TestClient(_api())
    assert message in str(raised.value)
    # The fix does not keep the regex: no path('^api/', ...).
    assert not re.search(r"(?<!re_)path\('\^", str(raised.value))


@pytest.mark.parametrize(
    ("patterns", "kwargs", "url"),
    [
        pytest.param(lambda: [re_path(r"^api/", include("django_bolt.urls"))], {}, "/api/own/1", id="re-path"),
        pytest.param(
            lambda: i18n_patterns(path("api/", include("django_bolt.urls")), prefix_default_language=False),
            {},
            "/api/own/1",
            id="i18n",
        ),
        pytest.param(lambda: [path("api", include("django_bolt.urls"))], {}, "/apiown/1", id="no-slash"),
        pytest.param(lambda: [path("<int:v>/", include("django_bolt.urls"))], {"v": 3}, "/3/own/1", id="int-converter"),
    ],
)
def test_django_alone_accepts_a_prefix_of_django_bolt_urls_that_bolt_cannot_serve(
    fresh_bolt_urls, mount_urlconf, settings, patterns, kwargs, url
):
    # Only runbolt and the test clients serve Bolt routes, so only they refuse the prefix.
    settings.USE_I18N = True
    settings.BOLT_API = [f"{__name__}:UNMOUNTED"]
    mount_urlconf([*patterns(), path("page/", _django_view, name="page")])

    call_command("check")
    assert reverse("page") == "/page/"
    assert DjangoClient().get("/page/").content == b"django"
    # The reverse-only names give the paths that Django joins, as before this include could move.
    assert reverse("own_detail", kwargs={**kwargs, "mission_id": 1}) == url
    with pytest.raises(ImproperlyConfigured, match="includes django_bolt.urls at"):
        TestClient(UNMOUNTED)


@pytest.fixture
def urlconf_file(tmp_path, monkeypatch, settings):
    """Return a function that writes a URLconf file and makes it the ROOT_URLCONF.

    Django imports the file at the first reverse(), as it imports the URLconf of a project.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []

    def make(source: str) -> None:
        name = f"urlconf_file_{next(_names)}"
        (tmp_path / f"{name}.py").write_text(source)
        created.append(name)
        settings.ROOT_URLCONF = name

    yield make
    for name in created:
        sys.modules.pop(name, None)


@pytest.mark.parametrize(
    "expression",
    [
        "[*bolt_urls.urlpatterns, path('missions/', MOUNTED.urls)]",
        "[path('missions/', MOUNTED.urls)] + bolt_urls.urlpatterns",
        "bolt_urls.urlpatterns + [path('missions/', MOUNTED.urls)]",
    ],
)
def test_a_urlconf_can_copy_the_patterns_of_django_bolt_urls(fresh_bolt_urls, urlconf_file, settings, expression):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED", f"{__name__}:MOUNTED"]
    urlconf_file(
        "from django.urls import path\n"
        "import django_bolt.urls as bolt_urls\n"
        f"from {__name__} import MOUNTED\n"
        f"urlpatterns = {expression}\n"
    )

    assert reverse("own_detail", kwargs={"mission_id": 1}) == "/own/1"
    assert reverse("mounted:detail", kwargs={"mission_id": 2}) == "/missions/2"
    with TestClient(UNMOUNTED) as client:
        assert client.get("/own/1").json() == {"id": 1}
    with TestClient(MOUNTED) as client:
        assert client.get("/missions/2").json() == {"id": 2}


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


async def _files(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"files"})


@pytest.mark.parametrize("kind", ["asgi", "django", "mcp"])
def test_a_mount_of_an_app_under_a_prefix_parameter_is_refused(mount_urlconf, kind):
    api = BoltAPI(openapi_config=_NO_DOCS)
    if kind == "asgi":
        api.mount_asgi("/files", _files)
    elif kind == "django":
        api.mount_django("/legacy")
    else:
        api.mount_mcp(MCP("missions"))
    mount_urlconf([path("<tenant>/", api.urls)])

    with pytest.raises(ImproperlyConfigured) as raised:
        TestClient(api)
    message = str(raised.value)
    assert message.startswith("The URLconf mounts a BoltAPI at path('<tenant>/', api.urls).")
    assert "Bolt serves an ASGI mount, a Django mount and an MCP server at a static path" in message


def test_a_prefix_parameter_with_the_name_of_a_route_parameter_is_refused(mount_urlconf):
    api = _api()
    mount_urlconf([path("org/<mission_id>/", include(([path("", api.urls)], "org")))])

    with pytest.raises(ImproperlyConfigured) as raised:
        TestClient(api)
    message = str(raised.value)
    assert message.startswith("The URLconf mounts a BoltAPI at path('org/<mission_id>/', include(...)) > ")
    assert "The path '/org/{mission_id}/{mission_id}' has the parameter 'mission_id' two times" in message


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


# --- Review follow-ups ------------------------------------------------------


def test_one_prefix_in_two_namespaces_serves_the_routes_once(mount_urlconf):
    api = _api()
    mount_urlconf(
        [
            path("missions/", include(([path("", api.urls)], "first"))),
            path("missions/", include(([path("", api.urls)], "second"))),
        ]
    )

    assert reverse("first:detail", kwargs={"mission_id": 3}) == "/missions/3"
    assert reverse("second:detail", kwargs={"mission_id": 3}) == "/missions/3"
    with TestClient(api) as client:
        assert client.get("/missions/3").json() == {"id": 3}


@pytest.fixture
def reverse_only_root(fresh_bolt_urls, settings):
    """Make ``django_bolt.urls`` the ROOT_URLCONF."""
    settings.ROOT_URLCONF = "django_bolt.urls"


def test_django_bolt_urls_as_the_root_urlconf_reverses_and_serves(reverse_only_root, settings):
    settings.BOLT_API = [f"{__name__}:UNMOUNTED"]

    assert reverse("own_detail", kwargs={"mission_id": 1}) == "/own/1"
    with TestClient(UNMOUNTED) as client:
        assert client.get("/own/1").json() == {"id": 1}


@pytest.mark.asyncio
async def test_websocket_connections_to_a_mounted_api_share_its_rate_limit(mount_urlconf):
    api = BoltAPI()

    @api.websocket("/limited")
    @rate_limit(rps=1, burst=1)
    async def limited(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("connected")

    mount_urlconf([path("missions/", api.urls)])
    options = {"cors_allowed_origins": ["*"], "read_django_settings": False}

    async with WebSocketTestClient(api, "/missions/limited", **options) as ws:
        assert await ws.receive_text() == "connected"
    with pytest.raises(PermissionError, match="Rate limit exceeded"):
        async with WebSocketTestClient(api, "/missions/limited", **options):
            pass
