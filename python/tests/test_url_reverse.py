"""Tests for URL reversing of Bolt routes.

Covers route-name derivation (explicit names kept verbatim, framework-derived
names slugified), opt-in per-API namespaces, Bolt-to-Django path conversion, and
the reverse-only urlpatterns that ``django_bolt.urls`` contributes to
``ROOT_URLCONF`` so Django's native ``reverse()`` resolves Bolt names.
"""

from __future__ import annotations

import sys
import types

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.urls import NoReverseMatch
from django.urls import reverse as django_reverse

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.urls import _to_django_route, build_urlpatterns
from django_bolt.utils import slugify_route_name
from django_bolt.views import APIView

_urlconf_counter = 0


def _make_urlconf(api: BoltAPI) -> str:
    """Build a reverse-only urlconf from ``api`` and return its importable name.

    A unique module name per call keeps Django's ``get_resolver`` LRU cache from
    serving a stale resolver across tests.
    """
    global _urlconf_counter
    _urlconf_counter += 1
    name = f"tests._tmp_bolt_urlconf_{_urlconf_counter}"
    mod = types.ModuleType(name)
    mod.urlpatterns = build_urlpatterns(api)
    sys.modules[name] = mod
    return name


def _route_metas(api: BoltAPI) -> list[dict]:
    """Return the handler metadata for every registered HTTP route."""
    return [api._handler_meta[handler_id] for _method, _path, handler_id, _fn in api._routes]


# --- Pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GetMission", "get-mission"),
        ("get_mission", "get-mission"),
        ("UserViewSet", "user-view-set"),
        ("partial_update", "partial-update"),
        ("Already-Slug", "already-slug"),
    ],
)
def test_slugify_route_name(value, expected):
    assert slugify_route_name(value) == expected


@pytest.mark.parametrize(
    ("bolt_path", "expected"),
    [
        ("/missions/{id}", "missions/<id>"),
        ("/missions/{id:int}", "missions/<id>"),  # router ignores :int; reverse stays untyped
        ("/files/{path:path}", "files/<path:path>"),  # catch-all keeps Django's path converter
        ("/health", "health"),
        ("/", ""),
    ],
)
def test_to_django_route(bolt_path, expected):
    assert _to_django_route(bolt_path) == expected


# --- Name derivation ------------------------------------------------------


def test_explicit_name_is_kept_verbatim():
    """An explicit name= must not be slugified (matches Django's path(name=...))."""
    api = BoltAPI()

    @api.get("/x", name="user_profile")
    def handler():
        return {}

    meta = _route_metas(api)[0]
    assert meta["name"] == "user_profile"
    assert meta["name_explicit"] is True


def test_derived_name_is_slugified():
    api = BoltAPI()

    @api.get("/y")
    def get_mission():
        return {}

    meta = _route_metas(api)[0]
    assert meta["name"] == "get-mission"
    assert meta["name_explicit"] is False


def test_namespace_is_opt_in():
    """No namespace= means a bare name; setting it namespaces every route."""
    bare = BoltAPI()

    @bare.get("/ping")
    def ping():
        return {}

    assert _route_metas(bare)[0]["namespace"] == ""

    namespaced = BoltAPI(namespace="missions")

    @namespaced.get("/missions/{id}", name="get_mission")
    def get_mission(id: int):
        return {}

    assert _route_metas(namespaced)[0]["namespace"] == "missions"


def test_unnamed_view_is_not_explicit():
    """A view()'s class-name fallback is derived, so it must not be explicit."""
    api = BoltAPI()

    @api.view("/items")
    class ItemView(APIView):
        async def get(self, request):
            return {}

        async def post(self, request):
            return {}

    metas = _route_metas(api)
    assert metas, "expected registered routes"
    assert all(m["name"] == "item-view" for m in metas)
    assert all(m["name_explicit"] is False for m in metas)


def test_named_view_is_verbatim_and_explicit():
    api = BoltAPI()

    @api.view("/items", name="item_box")
    class ItemView(APIView):
        async def get(self, request):
            return {}

    meta = _route_metas(api)[0]
    assert meta["name"] == "item_box"
    assert meta["name_explicit"] is True


def test_viewset_action_names():
    api = BoltAPI()

    @api.viewset("/users", name="user")
    class UserViewSet(ViewSet):
        async def list(self, request):
            return []

        async def retrieve(self, request):
            return {}

        async def partial_update(self, request):
            return {}

    names = {m["name"] for m in _route_metas(api)}
    assert "user-list" in names
    assert "user-retrieve" in names
    assert "user-partial-update" in names
    assert all(m["name_explicit"] for m in _route_metas(api))


def test_unnamed_viewset_derives_slug_and_is_not_explicit():
    api = BoltAPI()

    @api.viewset("/users")
    class UserViewSet(ViewSet):
        async def list(self, request):
            return []

    meta = _route_metas(api)[0]
    assert meta["name"] == "user-view-set-list"
    assert meta["name_explicit"] is False


def test_custom_action_reverse_name():
    """@action routes reverse as {base}-{action}, slugified from the method name."""
    api = BoltAPI()

    @api.viewset("/users", name="user")
    class UserViewSet(ViewSet):
        @action(["GET"], detail=False)
        async def recent(self, request):
            return []

    names = {m["name"] for m in _route_metas(api)}
    assert "user-recent" in names


# --- reverse() against the contributed urlpatterns ------------------------


def test_reverse_bare_name():
    api = BoltAPI()

    @api.get("/missions/{id}", name="get_mission")
    def get_mission(id: int):
        return {}

    urlconf = _make_urlconf(api)
    assert django_reverse("get_mission", urlconf=urlconf, kwargs={"id": 5}) == "/missions/5"


def test_reverse_namespaced():
    """A namespaced API is reversed as namespace:name, and only that way."""
    api = BoltAPI(namespace="missions")

    @api.get("/missions/{id}", name="get_mission")
    def get_mission(id: int):
        return {}

    urlconf = _make_urlconf(api)
    assert django_reverse("missions:get_mission", urlconf=urlconf, kwargs={"id": 5}) == "/missions/5"
    with pytest.raises(NoReverseMatch):
        django_reverse("get_mission", urlconf=urlconf, kwargs={"id": 5})


def test_reverse_catch_all_allows_slashes():
    """The path converter must accept slashes, matching Bolt's {*name} catch-all."""
    api = BoltAPI()

    @api.get("/files/{path:path}", name="serve_file")
    def serve_file(path: str):
        return {}

    urlconf = _make_urlconf(api)
    assert django_reverse("serve_file", urlconf=urlconf, kwargs={"path": "a/b/c.txt"}) == "/files/a/b/c.txt"


def test_reverse_unknown_name_raises():
    api = BoltAPI()

    @api.get("/ping", name="ping")
    def ping():
        return {}

    urlconf = _make_urlconf(api)
    with pytest.raises(NoReverseMatch):
        django_reverse("does_not_exist", urlconf=urlconf)


def test_same_path_multiple_methods_dedupes():
    """Two methods on one path share a name and must not collide-error."""
    api = BoltAPI()

    @api.get("/thing", name="thing")
    def read():
        return {}

    @api.post("/thing", name="thing")
    def write():
        return {}

    urlconf = _make_urlconf(api)
    assert django_reverse("thing", urlconf=urlconf) == "/thing"


def test_duplicate_explicit_names_on_different_paths_raise():
    api = BoltAPI()

    @api.get("/a", name="dup")
    def a():
        return {}

    @api.post("/b", name="dup")
    def b():
        return {}

    with pytest.raises(ImproperlyConfigured):
        build_urlpatterns(api)


def test_explicit_name_overrides_derived_collision():
    """An explicit name wins over a derived name that resolves to the same key."""
    api = BoltAPI()

    # Derived name "thing" on /derived (non-explicit).
    @api.get("/derived", name=None)
    def thing():
        return {}

    # Explicit name "thing" on /explicit.
    @api.get("/explicit", name="thing")
    def other():
        return {}

    urlconf = _make_urlconf(api)
    assert django_reverse("thing", urlconf=urlconf) == "/explicit"
