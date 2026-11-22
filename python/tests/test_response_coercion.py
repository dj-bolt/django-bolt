"""Response validation of a handler result against its response type.

Each test sends a request and checks the body. A 500 with "Response
validation error" is the failure response; its message names the cause.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import enum
import uuid
from typing import Annotated, Any, List  # noqa: UP035 - the test needs the bare typing.List

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
def client(settings):
    # The 500 shows the validation errors only when DEBUG is on. These tests read them.
    settings.DEBUG = True
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
    response = client.get("/struct/bad-dict")
    assert _validation_message(response) == "Response validation failed for Item: Expected `int`, got `str`"
    assert response.json()["extra"]["validation_errors"][0]["loc"] == ["response", "id"]


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
    response = client.get(path)
    assert _validation_message(response) == "Response validation failed for Item: Expected `int`, got `str`"
    assert response.json()["extra"]["validation_errors"][0]["loc"] == ["response", "id"]


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


class Color(enum.Enum):
    RED = "red"


_JSON_SCALARS = [
    (int, 3, b"3"),
    (float, 1.5, b"1.5"),
    (bool, True, b"true"),
    (None, None, b"null"),
    (decimal.Decimal, decimal.Decimal("1.50"), b'"1.50"'),
    (datetime.datetime, datetime.datetime(2020, 1, 2, 3, 4, 5), b'"2020-01-02T03:04:05"'),
    (datetime.date, datetime.date(2020, 1, 2), b'"2020-01-02"'),
    (uuid.UUID, uuid.UUID(int=1), b'"00000000-0000-0000-0000-000000000001"'),
    (Color, Color.RED, b'"red"'),
    (int | None, None, b"null"),
]


def _scalar_route(api: BoltAPI, path: str, value: Any, *, is_async: bool, **annotations: Any) -> None:
    """Register a route that returns ``value``. ``annotations`` can hold the return type."""
    if is_async:

        async def handler():
            return value
    else:

        def handler():
            return value

    handler.__annotations__.update(annotations)
    api.get(path)(handler)


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize(("return_type", "value", "body"), _JSON_SCALARS, ids=[str(case[0]) for case in _JSON_SCALARS])
def test_json_scalar_is_sent_as_json_with_and_without_the_return_type(return_type, value, body, is_async):
    api = BoltAPI()
    _scalar_route(api, "/declared", value, is_async=is_async, **{"return": return_type})
    paths = ["/declared"]
    if return_type is not None:
        # response_model=None means that the route has no response type.
        api.get("/unvalidated", validate_response=False, response_model=return_type)(lambda: value)
        paths.append("/unvalidated")
    if value is not None:
        # A None result with no response type is an error: the handler has no return.
        _scalar_route(api, "/bare", value, is_async=is_async)
        paths.append("/bare")

    with TestClient(api) as scalar_client:
        for path in paths:
            response = scalar_client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"] == "application/json"
            assert response.content == body


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize(
    ("return_type", "value", "expected"),
    [(int, 1.5, "Expected `int`, got `float`"), (bool, 1, "Expected `bool`, got `int`"), (float, None, "got `null`")],
)
def test_declared_json_scalar_type_is_validated(settings, return_type, value, expected, is_async):
    settings.DEBUG = True
    api = BoltAPI()
    _scalar_route(api, "/bad", value, is_async=is_async, **{"return": return_type})

    with TestClient(api) as scalar_client:
        assert expected in _validation_message(scalar_client.get("/bad"))


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize(
    ("return_type", "value", "expected"),
    [
        (int, "abc", "Expected `int`, got `str`"),
        (decimal.Decimal, "abc", "Invalid decimal string"),
        (Item, "abc", "Expected `object`, got `str`"),
        (int | None, "abc", "Expected `int | null`, got `str`"),
        (int, b"abc", "Expected `int`, got `bytes`"),
        (Item, bytearray(b"abc"), "Expected `object`, got `bytes`"),
    ],
)
def test_str_or_bytes_for_a_type_that_does_not_allow_it_is_a_validation_error(
    settings, return_type, value, expected, is_async
):
    settings.DEBUG = True
    api = BoltAPI()
    _scalar_route(api, "/bad", value, is_async=is_async, **{"return": return_type})
    api.get("/unvalidated", validate_response=False, response_model=return_type)(lambda: value)

    with TestClient(api) as scalar_client:
        assert expected in _validation_message(scalar_client.get("/bad"))
        # A route with no validation sends the result as before.
        assert scalar_client.get("/unvalidated").content == b"abc"


_SHORT_TEXT = Annotated[str, msgspec.Meta(max_length=5)]


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize(
    ("return_type", "value", "content_type"),
    [
        (str, "abc", "text/plain; charset=utf-8"),
        (str | None, "abc", "text/plain; charset=utf-8"),
        (str | Item, "abc", "text/plain; charset=utf-8"),
        (Any, "abc", "text/plain; charset=utf-8"),
        (_SHORT_TEXT, "abc", "text/plain; charset=utf-8"),
        (bytes, b"abc", "application/octet-stream"),
        (bytes | None, b"abc", "application/octet-stream"),
        (bytes | Item, bytearray(b"abc"), "application/octet-stream"),
    ],
    ids=["str", "str-or-none", "str-or-struct", "any", "annotated-str", "bytes", "bytes-or-none", "bytes-or-struct"],
)
def test_str_or_bytes_for_a_type_that_allows_it_keeps_its_content_type(return_type, value, content_type, is_async):
    api = BoltAPI()
    _scalar_route(api, "/declared", value, is_async=is_async, **{"return": return_type})
    _scalar_route(api, "/bare", value, is_async=is_async)

    with TestClient(api) as scalar_client:
        for path in ("/declared", "/bare"):
            response = scalar_client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"] == content_type
            assert response.content == b"abc"


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
def test_str_that_converts_to_the_declared_type_is_sent_as_json(settings, is_async):
    settings.DEBUG = True
    api = BoltAPI()
    _scalar_route(api, "/uuid", str(uuid.UUID(int=1)), is_async=is_async, **{"return": uuid.UUID})
    _scalar_route(api, "/long", "abcdef", is_async=is_async, **{"return": _SHORT_TEXT})

    with TestClient(api) as scalar_client:
        response = scalar_client.get("/uuid")
        assert response.headers["content-type"] == "application/json"
        assert response.content == b'"00000000-0000-0000-0000-000000000001"'
        assert "Expected `str` of length <= 5" in _validation_message(scalar_client.get("/long"))


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize("value", [3, "abc", {"id": 1, "username": "a"}], ids=["int", "str", "dict"])
def test_a_result_that_is_not_a_list_for_a_list_of_serializers_is_a_validation_error(settings, value, is_async):
    settings.DEBUG = True
    api = BoltAPI()
    _scalar_route(api, "/bad", value, is_async=is_async, **{"return": list[PublicUser]})
    _scalar_route(api, "/tuple", ({"id": 1, "username": "a"},), is_async=is_async, **{"return": list[PublicUser]})
    _scalar_route(api, "/none", None, is_async=is_async, **{"return": list[PublicUser]})
    api.get("/unvalidated", validate_response=False, response_model=list[PublicUser])(lambda: value)

    with TestClient(api) as scalar_client:
        message = _validation_message(scalar_client.get("/bad"))
        assert message == (
            f"Response type mismatch: expected list[PublicUser], but handler returned {type(value).__name__}. "
            "Make sure your handler returns a list."
        )
        assert scalar_client.get("/tuple").json() == [{"id": 1, "username": "a"}]
        # As for a list of Structs: None is the empty list.
        assert scalar_client.get("/none").json() == []
        # A route with no validation sends the result as before.
        assert scalar_client.get("/unvalidated").status_code == 200


def _mismatch_routes(api: BoltAPI, return_type: Any, value: Any, **route_options: Any) -> list[str]:
    """Register one route for each executor that Bolt compiles. Each route returns ``value``."""

    async def async_no_params():
        return value

    def sync_no_params():
        return value

    async def async_with_params(page: int = 1):
        return value

    def sync_with_params(page: int = 1):
        return value

    async def async_request_only(request):
        return value

    async def async_that_awaits():
        await asyncio.sleep(0)
        return value

    handlers = (
        async_no_params,
        sync_no_params,
        async_with_params,
        sync_with_params,
        async_request_only,
        async_that_awaits,
    )
    for handler in handlers:
        handler.__annotations__["return"] = return_type
        api.get(f"/{handler.__name__}", **route_options)(handler)
    return [f"/{handler.__name__}" for handler in handlers]


@pytest.mark.parametrize(
    ("return_type", "value", "expected"),
    [
        (dict, "abc", "expected dict, but handler returned str"),
        (dict, b"abc", "expected dict, but handler returned bytes"),
        (list, "abc", "expected list, but handler returned str"),
        (list, bytearray(b"abc"), "expected list, but handler returned bytearray"),
        (bytes, "abc", "expected bytes, but handler returned str"),
        (str, b"abc", "expected str, but handler returned bytes"),
    ],
    ids=["dict-str", "dict-bytes", "list-str", "list-bytearray", "bytes-str", "str-bytes"],
)
def test_str_or_bytes_for_dict_or_list_is_a_validation_error_on_each_executor(settings, return_type, value, expected):
    """The executor of a route with no parameters sent the JSON `"abc"`. The other executors sent text/plain."""
    settings.DEBUG = True
    api = BoltAPI()
    paths = _mismatch_routes(api, return_type, value)

    with TestClient(api) as scalar_client:
        for path in paths:
            assert expected in _validation_message(scalar_client.get(path)), path


@pytest.mark.parametrize(
    ("return_type", "value", "expected"),
    [
        (dict, [1], "expected dict, but handler returned list"),
        (dict, (1, 2), "expected dict, but handler returned tuple"),
        (dict, 3, "expected dict, but handler returned int"),
        (dict, None, "expected dict, but handler returned NoneType"),
        (list, {"a": 1}, "expected list, but handler returned dict"),
        (list, 3, "expected list, but handler returned int"),
        (list, None, "expected list, but handler returned NoneType"),
    ],
    ids=["dict-list", "dict-tuple", "dict-int", "dict-none", "list-dict", "list-int", "list-none"],
)
def test_a_result_of_another_kind_for_dict_or_list_is_a_validation_error_on_each_executor(
    settings, return_type, value, expected
):
    """Bolt sent a list or an int of a ``-> dict`` route, and a dict of a ``-> list`` route, as JSON."""
    settings.DEBUG = True
    api = BoltAPI()
    paths = _mismatch_routes(api, return_type, value)

    with TestClient(api) as scalar_client:
        for path in paths:
            assert expected in _validation_message(scalar_client.get(path)), path


def test_a_tuple_for_a_bare_list_is_a_json_array_on_each_executor():
    api = BoltAPI()
    paths = _mismatch_routes(api, list, (1, "a"))

    with TestClient(api) as scalar_client:
        for path in paths:
            response = scalar_client.get(path)
            assert response.headers["content-type"] == "application/json", path
            assert response.json() == [1, "a"], path


@pytest.mark.parametrize(
    ("return_type", "value"),
    [(dict, [1]), (dict, 3), (list, {"a": 1}), (list, 3)],
    ids=["dict-list", "dict-int", "list-dict", "list-int"],
)
def test_a_dict_or_list_route_with_no_validation_sends_a_result_of_another_kind(return_type, value):
    api = BoltAPI()
    paths = _mismatch_routes(api, return_type, value, validate_response=False)

    with TestClient(api) as scalar_client:
        for path in paths:
            assert scalar_client.get(path).json() == value, path


@pytest.mark.parametrize(
    ("return_type", "value", "content_type"),
    [
        (dict, "abc", "text/plain; charset=utf-8"),
        (dict, b"abc", "application/octet-stream"),
        (dict[str, int], "abc", "text/plain; charset=utf-8"),
        (list[int], b"abc", "application/octet-stream"),
    ],
    ids=["dict-str", "dict-bytes", "typed-dict-str", "typed-list-bytes"],
)
def test_str_or_bytes_of_a_route_with_no_validation_has_one_content_type_on_each_executor(
    return_type, value, content_type
):
    api = BoltAPI()
    paths = _mismatch_routes(api, return_type, value, validate_response=False)

    with TestClient(api) as scalar_client:
        for path in paths:
            response = scalar_client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"] == content_type, path
            assert response.content == b"abc", path


@pytest.mark.parametrize(
    ("return_type", "value"),
    [(dict, {"a": 1}), (list, [1, "a"]), (dict[str, int], {"a": 1}), (list[int], [1, 2])],
    ids=["dict", "list", "typed-dict", "typed-list"],
)
@pytest.mark.parametrize("validate_response", [True, False], ids=["validated", "unvalidated"])
def test_dict_or_list_result_is_json_on_each_executor(return_type, value, validate_response):
    api = BoltAPI()
    paths = _mismatch_routes(api, return_type, value, validate_response=validate_response)

    with TestClient(api) as scalar_client:
        for path in paths:
            response = scalar_client.get(path)
            assert response.headers["content-type"] == "application/json", path
            assert response.json() == value, path


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


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
@pytest.mark.parametrize(
    ("value", "got"), [({"a": 1}, "dict"), ([1], "list"), (3, "int"), (Item(id=1), "Item")], ids=repr
)
def test_none_response_type_rejects_a_result_that_is_not_none(settings, value, got, is_async):
    settings.DEBUG = True
    api = BoltAPI()
    _scalar_route(api, "/bad", value, is_async=is_async, **{"return": None})
    api.get("/unvalidated", validate_response=False, response_model=type(None))(lambda: value)

    with TestClient(api) as none_client:
        assert f"expected None, but handler returned {got}" in _validation_message(none_client.get("/bad"))
        # A route with no validation sends the result as before.
        assert none_client.get("/unvalidated").status_code == 200


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
def test_none_response_type_does_not_validate_a_response_object(is_async):
    """A response object has its own body. A -> None handler can return one."""
    api = BoltAPI()
    _scalar_route(api, "/json", JSON({"deleted": True}), is_async=is_async, **{"return": None})
    _scalar_route(api, "/response", Response({"deleted": True}), is_async=is_async, **{"return": None})
    _scalar_route(api, "/none", None, is_async=is_async, **{"return": None})

    with TestClient(api) as none_client:
        assert none_client.get("/json").json() == {"deleted": True}
        assert none_client.get("/response").json() == {"deleted": True}
        response = none_client.get("/none")
        assert (response.status_code, response.content) == (200, b"null")


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
