from __future__ import annotations

import msgspec

from django_bolt import BoltAPI
from django_bolt.decorators import action
from django_bolt.responses import JSON, Response
from django_bolt.testing import TestClient
from django_bolt.views import APIView, ViewSet


class Item(msgspec.Struct):
    name: str
    price: float


def _get_meta(api: BoltAPI, path: str, method: str = "GET"):
    for route_method, route_path, handler_id, _handler in api._routes:
        if route_method == method and route_path == path:
            return api._handler_meta[handler_id]
    raise AssertionError(f"Route not found: {method} {path}")


def test_validate_response_route_and_api_defaults():
    api = BoltAPI(validate_response=False)

    @api.get("/default-off")
    async def default_off() -> list[Item]:
        return [{"name": "missing-price"}]

    @api.get("/override-on", validate_response=True)
    async def override_on() -> list[Item]:
        return [{"name": "missing-price"}]

    @api.get("/model-default-off", response_model=list[Item])
    async def model_default_off():
        return [{"name": "missing-price"}]

    @api.get("/model-override-on", response_model=list[Item], validate_response=True)
    async def model_override_on():
        return [{"name": "missing-price"}]

    assert _get_meta(api, "/default-off")["_has_response_validation"] is False
    assert _get_meta(api, "/override-on")["_has_response_validation"] is True
    assert _get_meta(api, "/model-default-off")["_has_response_validation"] is False
    assert _get_meta(api, "/model-override-on")["_has_response_validation"] is True

    with TestClient(api) as client:
        assert client.get("/default-off").json() == [{"name": "missing-price"}]
        assert client.get("/model-default-off").json() == [{"name": "missing-price"}]

        strict_response = client.get("/override-on")
        assert strict_response.status_code == 500
        assert b"Response validation error" in strict_response.content

        strict_model_response = client.get("/model-override-on")
        assert strict_model_response.status_code == 500
        assert b"Response validation error" in strict_model_response.content


def test_response_model_none_skips_return_annotation_validation():
    api = BoltAPI()

    @api.get("/explicit-none", response_model=None)
    async def explicit_none() -> list[Item]:
        return [{"name": "missing-price"}]

    meta = _get_meta(api, "/explicit-none")
    assert meta["response_type"] is None
    assert meta["_has_response_validation"] is False

    with TestClient(api) as client:
        response = client.get("/explicit-none")
        assert response.status_code == 200
        assert response.json() == [{"name": "missing-price"}]


def test_validate_response_view_and_viewset_inheritance():
    api = BoltAPI()

    @api.view("/loose")
    class LooseView(APIView):
        validate_response = False

        async def get(self, request) -> list[Item]:
            return [{"name": "missing-price"}]

    @api.view("/strict", validate_response=True)
    class StrictView(APIView):
        validate_response = False

        async def get(self, request) -> list[Item]:
            return [{"name": "missing-price"}]

    @api.viewset("/widgets")
    class WidgetViewSet(ViewSet):
        validate_response = False

        async def list(self, request) -> list[Item]:
            return [{"name": "missing-price"}]

        @action(methods=["GET"], detail=False, path="strict", validate_response=True)
        async def strict_action(self, request) -> list[Item]:
            return [{"name": "missing-price"}]

    assert _get_meta(api, "/loose")["_has_response_validation"] is False
    assert _get_meta(api, "/strict")["_has_response_validation"] is True
    assert _get_meta(api, "/widgets")["_has_response_validation"] is False
    assert _get_meta(api, "/widgets/strict")["_has_response_validation"] is True

    with TestClient(api) as client:
        assert client.get("/loose").status_code == 200
        assert client.get("/loose").json() == [{"name": "missing-price"}]

        strict_view = client.get("/strict")
        assert strict_view.status_code == 500
        assert b"Response validation error" in strict_view.content

        assert client.get("/widgets").status_code == 200
        assert client.get("/widgets").json() == [{"name": "missing-price"}]

        strict_action = client.get("/widgets/strict")
        assert strict_action.status_code == 500
        assert b"Response validation error" in strict_action.content


def test_return_annotation_validates_only_the_default_status():
    """The return annotation describes the body of the default status.

    A ``JSON`` or ``Response`` with another status code carries a different
    body, such as a field error map. Bolt encodes it as-is.
    """
    api = BoltAPI()

    @api.post("/items", status_code=201)
    async def create_item(flag: str = "ok") -> Item:
        if flag == "bad":
            return JSON({"price": ["Must be positive."]}, status_code=400)
        if flag == "response":
            return Response({"price": ["Must be positive."]}, status_code=422)
        if flag == "wrong":
            return JSON({"name": "no-price"}, status_code=201)
        return JSON({"name": "x", "price": 1.0}, status_code=201)

    @api.get("/sync-items")
    def sync_item(flag: str = "ok") -> Item:
        if flag == "bad":
            return JSON({"price": ["Must be positive."]}, status_code=400)
        return Response({"name": "x", "price": 1.0})

    with TestClient(api) as client:
        bad = client.post("/items?flag=bad")
        assert bad.status_code == 400
        assert bad.json() == {"price": ["Must be positive."]}

        unprocessable = client.post("/items?flag=response")
        assert unprocessable.status_code == 422
        assert unprocessable.json() == {"price": ["Must be positive."]}

        assert client.post("/items?flag=wrong").status_code == 500
        assert client.post("/items").status_code == 201

        sync_bad = client.get("/sync-items?flag=bad")
        assert sync_bad.status_code == 400
        assert sync_bad.json() == {"price": ["Must be positive."]}
        assert client.get("/sync-items").status_code == 200
