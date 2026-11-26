"""Mount a BoltAPI from Django's URLconf with ``path("prefix/", api.urls)``.

Bolt walks the resolver of the URLconf one time. It serves each route of a
mounted API at the path that Django's ``reverse()`` gives for it.
"""

from __future__ import annotations

import itertools
import re
import sys
import types
from contextlib import asynccontextmanager

import pytest
from django.conf.urls.i18n import i18n_patterns
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import Client as DjangoClient
from django.urls import NoReverseMatch, include, path, re_path, register_converter, resolve, reverse
from django.utils.translation import gettext_lazy

from django_bolt import BoltAPI
from django_bolt._urlconf import REVERSE_ONLY_URLCONF, UrlconfMount, join_mount_path, served_apis, urlconf_mounts
from django_bolt.management.commands.runbolt import Command
from django_bolt.testing import TestClient
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
    api = BoltAPI()

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
    api = BoltAPI(namespace="missions")

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
