from __future__ import annotations

from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.decorators import action
from django_bolt.responses import JSON, Redirect, Response
from django_bolt.testing import TestClient
from django_bolt.views import APIView, ViewSet


class Item(msgspec.Struct):
    name: str
    price: float


type ItemOrJSON = Item | JSON
type AnyJSON = Annotated[JSON, "A JSON body."]
type OptionalJSON = JSON | None
type OptionalItem = Item | None


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

    with TestClient(api, raise_server_exceptions=False) as client:
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

    with TestClient(api, raise_server_exceptions=False) as client:
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

    with TestClient(api, raise_server_exceptions=False) as client:
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


def test_response_class_return_annotation_sends_the_body_as_it_is():
    """``-> JSON`` and ``-> Response`` name the response class, not the body (#912).

    Bolt does not validate the body against the class, at the default
    status code or at another one.
    """
    api = BoltAPI()

    @api.get("/json")
    async def json_default() -> JSON:
        return JSON({"a": 1})

    @api.post("/json-created", status_code=201)
    async def json_created() -> JSON:
        return JSON({"a": 1}, status_code=201)

    @api.get("/json-sync")
    def json_sync() -> JSON:
        return JSON([1, 2])

    @api.get("/response")
    async def response_default() -> Response:
        return Response({"b": 2})

    @api.post("/response-created", status_code=201)
    def response_created() -> Response:
        return Response({"b": 2}, status_code=201)

    @api.get("/optional")
    async def optional_json() -> JSON | None:
        return JSON({"c": 3})

    with TestClient(api) as client:
        default = client.get("/json")
        assert default.status_code == 200
        assert default.json() == {"a": 1}

        created = client.post("/json-created")
        assert created.status_code == 201
        assert created.json() == {"a": 1}

        sync = client.get("/json-sync")
        assert sync.status_code == 200
        assert sync.json() == [1, 2]

        response = client.get("/response")
        assert response.status_code == 200
        assert response.json() == {"b": 2}

        response_created = client.post("/response-created")
        assert response_created.status_code == 201
        assert response_created.json() == {"b": 2}

        optional = client.get("/optional")
        assert optional.status_code == 200
        assert optional.json() == {"c": 3}


def test_union_of_response_classes_sends_the_body_as_it_is():
    """``-> JSON | Redirect`` names two response classes, not the body (#912).

    A JSON result and a plain dict go to the client as they are.
    """
    api = BoltAPI()

    @api.get("/json-or-redirect")
    async def json_or_redirect(go: bool = False) -> JSON | Redirect:
        if go:
            return Redirect("/elsewhere")
        return JSON({"a": 1})

    @api.get("/dict-or-redirect")
    def dict_or_redirect() -> Response | Redirect | None:
        return {"b": 2}

    with TestClient(api, raise_server_exceptions=False) as client:
        sent = client.get("/json-or-redirect")
        assert sent.status_code == 200
        assert sent.json() == {"a": 1}

        redirected = client.get("/json-or-redirect?go=true", follow_redirects=False)
        assert redirected.status_code == 307
        assert redirected.headers["location"] == "/elsewhere"

        plain = client.get("/dict-or-redirect")
        assert plain.status_code == 200
        assert plain.json() == {"b": 2}


def test_union_of_a_body_type_and_a_response_class_validates_the_body_type():
    """``-> Item | JSON`` gave a 500 for each result: msgspec cannot validate a union with ``JSON``.

    The response class names the class of the result. Thus Bolt validates the
    body against ``Item``, as for ``-> Item``. A field that ``Item`` does not
    declare does not reach the client.
    """
    api = BoltAPI()

    @api.get("/item-or-json/{item_id}")
    async def item_or_json(item_id: int) -> Item | JSON:
        if item_id == 1:
            return Item(name="item", price=1.0)
        if item_id == 2:
            return JSON({"name": "item", "price": 2.0, "password": "secret"})
        if item_id == 3:
            return {"name": "item", "price": 3.0, "password": "secret"}
        if item_id == 4:
            return JSON({"name": "missing-price"})
        return JSON({"detail": "not found"}, status_code=404)

    @api.get("/item-or-response-or-none")
    def item_or_response_or_none() -> Item | Response | None:
        return None

    with TestClient(api, raise_server_exceptions=False) as client:
        item = client.get("/item-or-json/1")
        assert item.status_code == 200
        assert item.json() == {"name": "item", "price": 1.0}

        wrapped = client.get("/item-or-json/2")
        assert wrapped.status_code == 200
        assert wrapped.json() == {"name": "item", "price": 2.0}

        plain = client.get("/item-or-json/3")
        assert plain.status_code == 200
        assert plain.json() == {"name": "item", "price": 3.0}

        invalid = client.get("/item-or-json/4")
        assert invalid.status_code == 500
        assert invalid.json() == {"detail": "Response validation error"}

        missing = client.get("/item-or-json/5")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "not found"}

        nothing = client.get("/item-or-response-or-none")
        assert nothing.status_code == 200
        assert nothing.json() is None


def test_annotated_response_class_return_annotation_names_the_class_of_the_result():
    """``Annotated`` and a ``type`` alias do not change what a response class in a return annotation means (#930).

    ``-> Annotated[JSON, ...]`` sends the body as it is, as ``-> JSON`` does.
    ``-> Annotated[Item | JSON, ...]`` validates the body against ``Item``, as
    ``-> Item | JSON`` does. A ``response_model`` gives the same result.
    """
    api = BoltAPI()

    @api.get("/json")
    async def annotated_json() -> Annotated[JSON, "A JSON body."]:
        return JSON([1, 2])

    @api.get("/json-or-none")
    def annotated_json_or_none() -> Annotated[JSON, "A JSON body."] | None:
        return JSON({"a": 1})

    @api.get("/response")
    def annotated_response() -> Annotated[Response | None, "Any body."]:
        return {"b": 2}

    @api.get("/item-or-json/{item_id}")
    async def annotated_item_or_json(item_id: int) -> Annotated[Item | JSON, msgspec.Meta(description="An item.")]:
        if item_id == 1:
            return JSON({"name": "item", "price": 1.0, "password": "secret"})
        return JSON({"name": "missing-price"})

    @api.get("/model", response_model=Annotated[JSON, "A JSON body."])
    def model_json():
        return JSON({"c": 3})

    @api.get("/alias")
    def alias_json() -> AnyJSON | None:
        return JSON([3])

    @api.get("/item-alias")
    async def alias_item_or_json() -> ItemOrJSON:
        return JSON({"name": "item", "price": 4.0, "password": "secret"})

    with TestClient(api, raise_server_exceptions=False) as client:
        sent = client.get("/json")
        assert sent.status_code == 200
        assert sent.json() == [1, 2]

        optional = client.get("/json-or-none")
        assert optional.status_code == 200
        assert optional.json() == {"a": 1}

        plain = client.get("/response")
        assert plain.status_code == 200
        assert plain.json() == {"b": 2}

        item = client.get("/item-or-json/1")
        assert item.status_code == 200
        assert item.json() == {"name": "item", "price": 1.0}

        invalid = client.get("/item-or-json/2")
        assert invalid.status_code == 500
        assert invalid.json() == {"detail": "Response validation error"}

        model = client.get("/model")
        assert model.status_code == 200
        assert model.json() == {"c": 3}

        alias = client.get("/alias")
        assert alias.status_code == 200
        assert alias.json() == [3]

        item_alias = client.get("/item-alias")
        assert item_alias.status_code == 200
        assert item_alias.json() == {"name": "item", "price": 4.0}


@pytest.mark.parametrize("validate_response", [True, False])
def test_none_result_of_an_annotated_or_aliased_optional_return_type_is_json_null(validate_response):
    """A None result is the JSON null when ``Annotated`` or a ``type`` alias holds the ``| None`` (#930)."""
    api = BoltAPI(validate_response=validate_response)

    @api.get("/annotated-json")
    def annotated_json() -> Annotated[JSON | None, "A JSON body."]:
        return None

    @api.get("/alias-json")
    async def alias_json() -> OptionalJSON:
        return None

    @api.get("/annotated-item")
    def annotated_item() -> Annotated[Item | None, "An item."]:
        return None

    @api.get("/alias-item")
    async def alias_item() -> OptionalItem:
        return None

    @api.get("/nested-none")
    def nested_none() -> Item | Annotated[JSON | None, "Optional."]:
        return None

    with TestClient(api) as client:
        for path in ("/annotated-json", "/alias-json", "/annotated-item", "/alias-item", "/nested-none"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.json() is None, path
