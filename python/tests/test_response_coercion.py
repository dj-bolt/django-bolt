"""Response validation of a handler result against its response type.

Each test sends a request and checks the body. A 500 with "Response
validation error" is the failure response; its message names the cause.
"""

from __future__ import annotations

import asyncio
from typing import List  # noqa: UP035 - the test needs the bare typing.List

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.responses import JSON, Response
from django_bolt.serializers import Serializer, field
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


class Envelope[T](msgspec.Struct):
    """A parametrized Struct, as the pagination envelope is."""

    items: list[T]
    total: int


class Camel(msgspec.Struct, rename="camel"):
    item_id: int


class ArticleOut(msgspec.Struct):
    id: int
    title: str


class ArticleCamel(msgspec.Struct, rename="camel"):
    id: int
    is_published: bool


class ArticleWrongType(msgspec.Struct):
    """The title column holds a string, so each row fails validation."""

    id: int
    title: int


class PublicUser(Serializer):
    id: int
    username: str
    password: str = field(write_only=True, default="")


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

    # The awaiting handlers run the async response handlers. The others run
    # the sync response handlers.
    @api.get("/struct/serializer")
    async def struct_serializer() -> Item:
        await asyncio.sleep(0)
        return PublicUser(id=1, username="a", password="p")

    @api.get("/struct/serializer-sync", status_code=201)
    def struct_serializer_sync() -> Item:
        return PublicUser(id=1, username="a", password="p")

    @api.get("/json/bad")
    async def json_bad() -> Item:
        await asyncio.sleep(0)
        return JSON({"id": "x"})

    @api.get("/json/bad-sync")
    def json_bad_sync() -> Item:
        return JSON({"id": "x"})

    @api.get("/response/bad")
    async def response_bad() -> Item:
        await asyncio.sleep(0)
        return Response({"id": "x"})

    @api.get("/response/bad-sync")
    def response_bad_sync() -> Item:
        return Response({"id": "x"})

    @api.get("/response/ok")
    async def response_ok() -> Item:
        await asyncio.sleep(0)
        return Response({"id": 1}, headers={"X-A": "1"})

    @api.get("/response/ok-sync")
    def response_ok_sync() -> Item:
        return Response({"id": 1})

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

    @api.get("/list-bare/ok")
    async def list_bare_ok() -> List:  # noqa: UP006
        return [1, "a"]

    @api.get("/list-bare/dict")
    async def list_bare_dict() -> List:  # noqa: UP006
        return {"a": 1}

    @api.get("/generic/dict")
    async def generic_dict() -> Envelope[int]:
        return {"items": [1, 2], "total": 2}

    @api.get("/generic/bad-dict")
    async def generic_bad_dict() -> Envelope[int]:
        return {"items": ["x"], "total": 1}

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

    @api.get("/articles/wrong-type")
    async def articles_wrong_type() -> list[ArticleWrongType]:
        await asyncio.sleep(0)
        return Article.objects.order_by("id")

    @api.get("/articles/wrong-type-sync")
    def articles_wrong_type_sync() -> list[ArticleWrongType]:
        return Article.objects.order_by("id")

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


def test_validation_error_entry_has_the_response_location_and_the_error_type(client):
    response = client.get("/struct/list")

    assert response.status_code == 500, response.text
    assert response.json()["extra"]["validation_errors"] == [
        {
            "loc": ["response"],
            "msg": (
                "Response type mismatch: expected a single Item, but handler returned a list. "
                "Did you mean to annotate with list[Item]?"
            ),
            "type": "validation_error",
        }
    ]


@pytest.mark.parametrize(
    ("path", "status_code"),
    [("/struct/serializer", 200), ("/struct/serializer-sync", 201)],
)
def test_serializer_for_a_struct_is_dumped_then_validated(client, path, status_code):
    response = client.get(path)

    assert response.status_code == status_code, response.text
    assert response.json() == {"id": 1, "note": "default"}


@pytest.mark.parametrize("path", ["/json/bad", "/json/bad-sync", "/response/bad", "/response/bad-sync"])
def test_invalid_json_or_response_content_names_the_cause(client, path):
    message = _validation_message(client.get(path))
    assert message == "Response validation failed for Item: Expected `int`, got `str` - at `$.id`"


def test_response_content_is_validated_and_keeps_the_headers(client):
    response = client.get("/response/ok")

    assert response.status_code == 200, response.text
    assert response.json() == {"id": 1, "note": "default"}
    assert response.headers["x-a"] == "1"


def test_response_content_is_validated_in_a_sync_handler(client):
    response = client.get("/response/ok-sync")

    assert response.status_code == 200, response.text
    assert response.json() == {"id": 1, "note": "default"}


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


def test_bare_list_accepts_any_item_and_names_its_item_type_in_a_mismatch(client):
    assert client.get("/list-bare/ok").json() == [1, "a"]
    message = _validation_message(client.get("/list-bare/dict"))
    assert message.startswith("Response type mismatch: expected list[Any], but handler returned dict.")


def test_parametrized_struct_from_a_dict_is_validated(client):
    assert client.get("/generic/dict").json() == {"items": [1, 2], "total": 2}
    message = _validation_message(client.get("/generic/bad-dict"))
    assert message.startswith("Response validation failed: Expected `int`, got `str`")


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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["/articles/wrong-type", "/articles/wrong-type-sync"])
def test_queryset_rows_are_validated_against_the_struct(client, path):
    Article.objects.create(title="One", content="c", author="a")

    message = _validation_message(client.get(path))

    assert message == "Expected `int`, got `str`"


def test_unvalidated_objects_use_the_wire_names_of_a_renamed_struct(client):
    # Without validation, a missing attribute is null.
    assert client.get("/camel/objects-unvalidated").json() == [
        {"id": 5, "isPublished": True},
        {"id": 6, "isPublished": None},
    ]
