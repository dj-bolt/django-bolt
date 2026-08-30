"""Layered ``include_in_schema`` (Litestar style): app > class > route.

The most specific layer wins. ``None`` means "inherit from the outer layer".
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.decorators import action
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.testing import TestClient
from django_bolt.views import APIView, ViewSet


def _paths(api: BoltAPI) -> set[str]:
    config = OpenAPIConfig(title="t", version="1")
    return set(SchemaGenerator(api, config).generate().to_schema()["paths"])


def test_api_include_in_schema_false_hides_every_route_but_serves_them():
    api = BoltAPI(include_in_schema=False)

    @api.get("/a")
    async def a():
        return {"a": 1}

    @api.get("/b", include_in_schema=True)
    async def b():
        return {"b": 1}

    assert _paths(api) == {"/b"}
    with TestClient(api) as client:
        assert client.get("/a").json() == {"a": 1}


def test_mounted_api_inherits_and_overrides():
    hidden = BoltAPI(include_in_schema=False)

    @hidden.get("/x")
    async def x():
        return {}

    @hidden.get("/shown", include_in_schema=True)
    async def shown():
        return {}

    inherit = BoltAPI()

    @inherit.get("/y")
    async def y():
        return {}

    parent = BoltAPI()
    parent.mount("/hidden", hidden)
    parent.mount("/inherit", inherit)
    assert _paths(parent) == {"/hidden/shown", "/inherit/y"}

    hidden_parent = BoltAPI(include_in_schema=False)
    hidden_parent.mount("/inherit", inherit)
    hidden_parent.mount("/hidden", hidden)
    assert _paths(hidden_parent) == {"/hidden/shown"}


def test_view_class_attribute_and_decorator_kwarg():
    api = BoltAPI()

    @api.view("/cls")
    class HiddenView(APIView):
        include_in_schema = False

        async def get(self):
            return {}

    @api.view("/kw", include_in_schema=True)
    class OverriddenView(APIView):
        include_in_schema = False

        async def get(self):
            return {}

    @api.viewset("/vs")
    class HiddenViewSet(ViewSet):
        include_in_schema = False

        async def list(self):
            return []

        @action(detail=False, methods=["GET"])
        async def custom(self):
            return {}

    @api.viewset("/vs2", include_in_schema=False)
    class KwViewSet(ViewSet):
        async def list(self):
            return []

    assert _paths(api) == {"/kw"}
