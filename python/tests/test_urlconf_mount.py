"""Mount a BoltAPI from Django's URLconf with ``path("prefix/", api.urls)``.

Bolt walks the resolver of the URLconf one time. It serves each route of a
mounted API at the path that Django's ``reverse()`` gives for it.
"""

from __future__ import annotations

import itertools
import re
import sys
import types

import pytest
from django.conf.urls.i18n import i18n_patterns
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.urls import include, path, re_path, register_converter
from django.utils.translation import gettext_lazy

from django_bolt import BoltAPI
from django_bolt._urlconf import UrlconfMount, join_mount_path, urlconf_mounts

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
