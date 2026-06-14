"""Tests for URL reversing of Bolt routes.

Covers route-name derivation (every derived name is taken verbatim from its
Python identifier, exactly like an explicit ``name=``), opt-in per-API
namespaces, Bolt-to-Django path conversion, and the reverse-only urlpatterns
that ``django_bolt.urls`` contributes to ``ROOT_URLCONF`` so Django's native
``reverse()`` resolves Bolt names.
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


@pytest.fixture
def urlconf_factory():
    """Yield a ``_make_urlconf`` wrapper that cleans up its synthetic modules.

    Each call injects a module into ``sys.modules``; without teardown those
    accumulate for the whole session. This tracks every module it creates and
    pops them when the test finishes.
    """
    created: list[str] = []

    def make(api: BoltAPI) -> str:
        name = _make_urlconf(api)
        created.append(name)
        return name

    yield make
    for name in created:
        sys.modules.pop(name, None)


# --- Pure helpers ---------------------------------------------------------


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


def test_derived_name_is_verbatim():
    """A derived name is the bare function identifier, untransformed (like Starlette)."""
    api = BoltAPI()

    @api.get("/y")
    def get_mission():
        return {}

    meta = _route_metas(api)[0]
    assert meta["name"] == "get_mission"
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
    """A view()'s class-name fallback is the verbatim class name, and not explicit."""
    api = BoltAPI()

    @api.view("/items")
    class ItemView(APIView):
        async def get(self, request):
            return {}

        async def post(self, request):
            return {}

    metas = _route_metas(api)
    assert metas, "expected registered routes"
    assert all(m["name"] == "ItemView" for m in metas)
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
    assert "user-partial_update" in names
    assert all(m["name_explicit"] for m in _route_metas(api))


def test_unnamed_viewset_derives_class_name_and_is_not_explicit():
    api = BoltAPI()

    @api.viewset("/users")
    class UserViewSet(ViewSet):
        async def list(self, request):
            return []

    meta = _route_metas(api)[0]
    assert meta["name"] == "UserViewSet-list"
    assert meta["name_explicit"] is False


def test_custom_action_reverse_name():
    """@action routes reverse as {base}-{action}, taken verbatim from the method name."""
    api = BoltAPI()

    @api.viewset("/users", name="user")
    class UserViewSet(ViewSet):
        @action(["GET"], detail=False)
        async def recent(self, request):
            return []

    names = {m["name"] for m in _route_metas(api)}
    assert "user-recent" in names


def test_explicit_action_name_is_explicit_on_unnamed_viewset():
    """An @action(name=...) is user-intended, so it stays explicit even when the
    viewset base name is derived from the class name."""
    api = BoltAPI()

    @api.viewset("/users")
    class UserViewSet(ViewSet):
        @action(["GET"], detail=False, name="active")
        async def list_active(self, request):
            return []

    meta = next(m for m in _route_metas(api) if m["name"] == "UserViewSet-active")
    assert meta["name_explicit"] is True


# --- reverse() against the contributed urlpatterns ------------------------


def test_reverse_bare_name(urlconf_factory):
    api = BoltAPI()

    @api.get("/missions/{id}", name="get_mission")
    def get_mission(id: int):
        return {}

    urlconf = urlconf_factory(api)
    assert django_reverse("get_mission", urlconf=urlconf, kwargs={"id": 5}) == "/missions/5"


def test_reverse_namespaced(urlconf_factory):
    """A namespaced API is reversed as namespace:name, and only that way."""
    api = BoltAPI(namespace="missions")

    @api.get("/missions/{id}", name="get_mission")
    def get_mission(id: int):
        return {}

    urlconf = urlconf_factory(api)
    assert django_reverse("missions:get_mission", urlconf=urlconf, kwargs={"id": 5}) == "/missions/5"
    with pytest.raises(NoReverseMatch):
        django_reverse("get_mission", urlconf=urlconf, kwargs={"id": 5})


def test_reverse_catch_all_allows_slashes(urlconf_factory):
    """The path converter must accept slashes, matching Bolt's {*name} catch-all."""
    api = BoltAPI()

    @api.get("/files/{path:path}", name="serve_file")
    def serve_file(path: str):
        return {}

    urlconf = urlconf_factory(api)
    assert django_reverse("serve_file", urlconf=urlconf, kwargs={"path": "a/b/c.txt"}) == "/files/a/b/c.txt"


def test_reverse_unknown_name_raises(urlconf_factory):
    api = BoltAPI()

    @api.get("/ping", name="ping")
    def ping():
        return {}

    urlconf = urlconf_factory(api)
    with pytest.raises(NoReverseMatch):
        django_reverse("does_not_exist", urlconf=urlconf)


def test_same_path_multiple_methods_dedupes(urlconf_factory):
    """Two methods on one path share a name and must not collide-error."""
    api = BoltAPI()

    @api.get("/thing", name="thing")
    def read():
        return {}

    @api.post("/thing", name="thing")
    def write():
        return {}

    urlconf = urlconf_factory(api)
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


def test_explicit_name_overrides_derived_collision(urlconf_factory):
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

    urlconf = urlconf_factory(api)
    assert django_reverse("thing", urlconf=urlconf) == "/explicit"
