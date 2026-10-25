"""Response validation of a handler result against its response type.

Each test sends a request and checks the body. A 500 with "Response
validation error" is the failure response; its message names the cause.
"""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.responses import JSON
from django_bolt.testing import TestClient

from .test_models import Article


class Obj:
    """An object with attributes, as a Django model instance has."""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class Item(msgspec.Struct):
    id: int
    note: str = "default"


class ItemFull(msgspec.Struct):
    id: int
    note: str
    secret: str


class Camel(msgspec.Struct, rename="camel"):
    item_id: int


class ArticleOut(msgspec.Struct):
    id: int
    title: str


class ArticleCamel(msgspec.Struct, rename="camel"):
    id: int
    is_published: bool


def _validation_message(response) -> str:
    assert response.status_code == 500, response.text
    body = response.json()
    assert body["detail"] == "Response validation error"
    return body["extra"]["validation_errors"][0]["msg"]


# Function scope: each test registers the routes, so a mutation tool sees
# each test reach the compiled coercers.
@pytest.fixture
def client():
    api = BoltAPI()

    @api.get("/struct/dict")
    async def struct_dict() -> Item:
        return {"id": 1}

    @api.get("/struct/bad-dict")
    async def struct_bad_dict() -> Item:
        return {"id": "x"}

    @api.get("/struct/object")
    async def struct_object() -> Item:
        return Obj(id=1, note="n")

    @api.get("/struct/object-without-default")
    async def struct_object_without_default() -> Item:
        return Obj(id=1)

    @api.get("/struct/renamed-object")
    async def struct_renamed_object() -> Camel:
        return Obj(item_id=7)

    @api.get("/struct/other-struct")
    async def struct_other_struct() -> Item:
        return ItemFull(id=2, note="n", secret="s")

    @api.get("/struct/list")
    async def struct_list() -> Item:
        return [{"id": 1}]

    @api.get("/optional/object")
    async def optional_object() -> Item | None:
        return Obj(id=3)

    @api.get("/list/objects")
    async def list_objects() -> list[Item]:
        return [Obj(id=1, note="a"), Obj(id=2)]

    @api.get("/list/renamed-objects")
    async def list_renamed_objects() -> list[Camel]:
        return [Obj(item_id=1), Obj(item_id=2)]

    @api.get("/list/json-objects")
    async def list_json_objects() -> list[Item]:
        return JSON([Obj(id=4)])

    @api.get("/list/none")
    async def list_none() -> list[Item]:
        return JSON(None)

    @api.get("/list/dict")
    async def list_dict() -> list[Item]:
        return {"id": 1}

    @api.get("/list/union-dict")
    async def list_union_dict() -> list[Item | Camel]:
        return {"id": 1}

    @api.get("/list-int/ok")
    async def list_int_ok() -> list[int]:
        return [1, 2]

    @api.get("/list-int/bad-item")
    async def list_int_bad_item() -> list[int]:
        return [1, "x"]

    @api.get("/list-int/dict")
    async def list_int_dict() -> list[int]:
        return {"a": 1}

    @api.get("/mapping/bad")
    async def mapping_bad() -> dict[str, int]:
        return {"a": "x"}

    @api.get("/articles/async")
    async def articles_async() -> list[ArticleOut]:
        return JSON(Article.objects.order_by("id"))

    @api.get("/articles/sync")
    def articles_sync() -> list[ArticleOut]:
        return JSON(Article.objects.order_by("id"))

    @api.get("/camel/queryset")
    async def camel_queryset() -> list[ArticleCamel]:
        return Article.objects.order_by("id")

    @api.get("/camel/queryset-sync")
    def camel_queryset_sync() -> list[ArticleCamel]:
        return Article.objects.order_by("id")

    @api.get("/camel/json-queryset")
    async def camel_json_queryset() -> list[ArticleCamel]:
        return JSON(Article.objects.order_by("id"))

    @api.get("/camel/json-queryset-sync")
    def camel_json_queryset_sync() -> list[ArticleCamel]:
        return JSON(Article.objects.order_by("id"))

    @api.get("/camel/queryset-unvalidated", validate_response=False)
    async def camel_queryset_unvalidated() -> list[ArticleCamel]:
        return Article.objects.order_by("id")

    # The request parameter sends the result through serialize_response, which
    # projects objects when no validator runs.
    @api.get("/camel/objects-unvalidated", validate_response=False)
    async def camel_objects_unvalidated(request) -> list[ArticleCamel]:
        return [Obj(id=5, is_published=True), Obj(id=6)]

    return TestClient(api)


def test_dict_converts_to_the_struct(client):
    assert client.get("/struct/dict").json() == {"id": 1, "note": "default"}


def test_invalid_dict_names_the_struct_and_the_field(client):
    message = _validation_message(client.get("/struct/bad-dict"))
    assert message == "Response validation failed for Item: Expected `int`, got `str` - at `$.id`"


def test_object_converts_by_attribute(client):
    assert client.get("/struct/object").json() == {"id": 1, "note": "n"}


def test_object_without_an_attribute_gets_the_struct_default(client):
    assert client.get("/struct/object-without-default").json() == {"id": 1, "note": "default"}


def test_object_converts_to_a_renamed_struct(client):
    assert client.get("/struct/renamed-object").json() == {"itemId": 7}


def test_other_struct_keeps_only_the_declared_fields(client):
    assert client.get("/struct/other-struct").json() == {"id": 2, "note": "n"}


def test_list_for_a_single_struct_is_a_type_mismatch(client):
    message = _validation_message(client.get("/struct/list"))
    assert message.startswith("Response type mismatch: expected a single Item, but handler returned a list.")


def test_object_converts_to_an_optional_struct(client):
    assert client.get("/optional/object").json() == {"id": 3, "note": "default"}


def test_list_of_objects_converts_by_attribute(client):
    assert client.get("/list/objects").json() == [{"id": 1, "note": "a"}, {"id": 2, "note": "default"}]


def test_list_of_objects_converts_to_a_renamed_struct(client):
    assert client.get("/list/renamed-objects").json() == [{"itemId": 1}, {"itemId": 2}]


def test_json_with_a_list_of_objects_is_validated(client):
    assert client.get("/list/json-objects").json() == [{"id": 4, "note": "default"}]


def test_none_for_a_list_is_an_empty_list(client):
    assert client.get("/list/none").json() == []


@pytest.mark.parametrize(
    ("path", "type_name"),
    [("/list/dict", "Item"), ("/list-int/dict", "int"), ("/list/union-dict", repr(Item | Camel))],
)
def test_non_list_for_a_list_is_a_type_mismatch(client, path, type_name):
    message = _validation_message(client.get(path))
    assert message.startswith(f"Response type mismatch: expected list[{type_name}], but handler returned dict.")


def test_list_of_scalars_is_validated(client):
    assert client.get("/list-int/ok").json() == [1, 2]
    message = _validation_message(client.get("/list-int/bad-item"))
    assert "Expected `int`, got `str`" in message


def test_other_generic_type_is_validated(client):
    message = _validation_message(client.get("/mapping/bad"))
    assert message.startswith("Response validation failed: Expected `int`, got `str`")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["/articles/async", "/articles/sync"])
def test_queryset_in_json_loads_the_declared_fields(client, path):
    first = Article.objects.create(title="One", content="c", author="a")
    second = Article.objects.create(title="Two", content="c", author="a")

    assert client.get(path).json() == [
        {"id": first.id, "title": "One"},
        {"id": second.id, "title": "Two"},
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "path",
    [
        "/camel/queryset",
        "/camel/queryset-sync",
        "/camel/json-queryset",
        "/camel/json-queryset-sync",
        "/camel/queryset-unvalidated",
    ],
)
def test_queryset_rows_use_the_wire_names_of_a_renamed_struct(client, path):
    article = Article.objects.create(title="One", content="c", author="a", is_published=True)

    response = client.get(path)

    assert response.status_code == 200, response.text
    assert response.json() == [{"id": article.id, "isPublished": True}]


def test_unvalidated_objects_use_the_wire_names_of_a_renamed_struct(client):
    # Without validation, a missing attribute is null.
    assert client.get("/camel/objects-unvalidated").json() == [
        {"id": 5, "isPublished": True},
        {"id": 6, "isPublished": None},
    ]
