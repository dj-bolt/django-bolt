from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.middleware import Middleware
from django_bolt.openapi.config import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.responses import EventSourceResponse
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient


class Cat(msgspec.Struct, tag=True, tag_field="_type"):
    name: str
    meows: bool = True


class Dog(Serializer, tag=True, tag_field="_type"):
    name: str
    barks: bool = True


def test_union_response_model():
    api = BoltAPI()

    @api.get("/pet/{pet_type}", response_model=Cat | Dog)
    async def get_pet(pet_type: str):
        if pet_type == "cat":
            return {"name": "Whiskers", "meows": True, "type": "cat", "_type": "Cat"}
        return {"name": "Fido", "barks": True, "type": "dog", "_type": "Dog"}

    with TestClient(api) as client:
        # Test cat response
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        # Test dog response
        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_return_annotation():
    api = BoltAPI()

    @api.get("/pet/{pet_type}")
    async def get_pet(pet_type: str) -> Cat | Dog:
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_validation_error():
    api = BoltAPI(validate_response=True)

    @api.get("/bad-pet", response_model=Cat | Dog)
    async def get_bad_pet():
        return {"name": "Bird", "flies": True}

    with TestClient(api) as client:
        resp = client.get("/bad-pet")
        assert resp.status_code == 500
        assert b"Response validation error" in resp.content


def test_optional_response():
    api = BoltAPI()

    @api.get("/maybe-pet/{pet_type}", response_model=Cat | None)
    async def get_maybe_pet(pet_type: str):
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return None

    with TestClient(api) as client:
        resp = client.get("/maybe-pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/maybe-pet/none")
        assert resp.status_code == 200
        assert resp.json() is None


def test_typing_union_compatibility():
    api = BoltAPI()

    @api.get("/typing-union", response_model=Cat | Dog)
    async def get_typing_union():
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/typing-union")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


# ---------------------------------------------------------------------------
# Regression tests: the inline-encoding fast path must delegate edge cases to
# serialize_response. Each test asserts a behavior the slow path provided
# uniformly; the inline path used to short-circuit and emit wrong output.
# ---------------------------------------------------------------------------


class Tagged(msgspec.Struct, tag=True, tag_field="_type"):
    """Plain Struct used to force the struct-mode inline path."""

    name: str


def test_inline_path_204_with_none_returns_empty_body():
    """status_code=204 + response_model=Struct + handler returns None → empty 204."""
    api = BoltAPI()

    @api.delete("/items/{item_id}", status_code=204, response_model=Tagged)
    async def delete_item(item_id: int):
        return None

    with TestClient(api) as client:
        resp = client.delete("/items/1")
        assert resp.status_code == 204
        assert resp.content == b""
        # RFC 9110 §15.3.5: a 204 must not carry a body.


def test_inline_path_struct_with_response_class_sse():
    """response_class=EventSourceResponse must not be shadowed by the struct inline path."""
    api = BoltAPI()

    @api.get("/events", response_class=EventSourceResponse, response_model=Tagged)
    async def events():
        async def gen():
            yield Tagged(name="a")
            yield Tagged(name="b")

        return gen()

    with TestClient(api) as client:
        resp = client.get("/events")
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"expected SSE content-type, got {ct!r}"


def test_inline_path_with_generator_auto_wraps_to_streaming():
    """Async-generator return under a stream-typed annotation must auto-wrap.

    ``msgspec.convert`` refuses to consume an async generator, so the inline
    path must defer to ``_build_auto_streaming_response`` which wraps it as
    ndjson.
    """
    api = BoltAPI()

    @api.get("/feed")
    async def feed() -> AsyncIterator[Tagged]:
        async def gen():
            yield Tagged(name="a")
            yield Tagged(name="b")

        return gen()

    with TestClient(api) as client:
        resp = client.get("/feed")
        assert resp.status_code == 200
        assert b"_type" in resp.content


def test_inline_path_with_queryset_like_object_delegates_to_slow_path():
    """QuerySet-shaped object under list[Struct] must delegate to the slow path.

    The slow path detects ``_iterable_class``/``model`` and runs the values()
    projection via sync_to_async. A direct ``msgspec.convert`` on a non-list
    duck-type raises, so the inline path must defer here.
    """
    api = BoltAPI()

    class FakeQuerySet:
        """Mimics the QuerySet duck-type that coerce_to_response_type checks for."""

        _iterable_class = object
        model = object

        def values(self, *names):
            return []

    @api.get("/qs")
    async def qs_endpoint() -> list[Tagged]:
        return FakeQuerySet()

    with TestClient(api) as client:
        resp = client.get("/qs")
        # Either 200 (slow-path values() → []) or 500 is acceptable here, but the
        # response must NOT be a validation error citing the wrong wire shape —
        # that would mean the inline path swallowed the input and bypassed
        # slow-path QuerySet detection.
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            assert resp.json() == []


def test_tagged_union_emits_per_arm_examples_in_openapi():
    """Tagged Struct unions must emit per-arm ``examples`` so Swagger renders a dropdown.

    A single ``Example Value`` (Swagger's default for ``oneOf``) shows only the
    first arm and hides the discriminator semantics. Per-arm examples surface
    each variant with its tag value.
    """

    class Cat(msgspec.Struct, tag=True):
        name: str
        meows: bool = True

    class Dog(msgspec.Struct, tag=True):
        name: str
        barks: bool = True

    api = BoltAPI(openapi_config=OpenAPIConfig(title="t", version="1"))

    @api.get("/pet", response_model=Cat | Dog)
    async def get_pet():
        return Cat(name="x")

    schema = SchemaGenerator(api, api._openapi_config).generate().to_schema()
    media = schema["paths"]["/pet"]["get"]["responses"]["200"]["content"]["application/json"]
    examples = media.get("examples")
    assert examples is not None, f"expected per-arm examples, got: {media}"
    assert set(examples.keys()) == {"Cat", "Dog"}, examples
    cat_example = examples["Cat"]["value"]
    assert cat_example["type"] == "Cat"
    assert cat_example["name"] == "string"
    assert cat_example["meows"] is True
    dog_example = examples["Dog"]["value"]
    assert dog_example["type"] == "Dog"
    assert dog_example["barks"] is True


def test_list_of_tagged_union_omits_per_arm_examples():
    """``list[Union[A, B, ...]]`` must NOT emit per-arm examples.

    A real list can contain a mix of variants; per-arm dropdowns of
    homogeneous lists ("10 Cats" / "10 Dogs") misrepresent that. Swagger
    UI's default rendering of ``items.oneOf`` already produces a
    heterogeneous example array with one of each arm — the truthful
    shape — so we just stay out of its way.
    """

    class Cat(msgspec.Struct, tag=True):
        name: str

    class Dog(msgspec.Struct, tag=True):
        name: str

    api = BoltAPI(openapi_config=OpenAPIConfig(title="t", version="1"))

    @api.get("/pets", response_model=list[Cat | Dog])
    async def list_pets():
        return [Cat(name="x"), Dog(name="y")]

    schema = SchemaGenerator(api, api._openapi_config).generate().to_schema()
    media = schema["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]
    # No `examples` block — Swagger uses its default item-per-arm rendering.
    assert media.get("examples") is None, f"expected no examples for list[union], got: {media}"
    # But the schema itself must still emit `oneOf` so each variant is documented.
    items = media["schema"]["items"]
    refs = {item["$ref"].rsplit("/", 1)[-1] for item in items["oneOf"]}
    assert refs == {"Cat", "Dog"}


class Row(msgspec.Struct):
    id: int
    note: str = "default"


class RowObject:
    """An object with attributes in place of keys, as a model instance has."""

    def __init__(self, id: int):
        self.id = id


ROWS_JSON = [{"id": 1, "note": "default"}, {"id": 2, "note": "default"}]


class PassThroughMiddleware(Middleware):
    """Python middleware: the route cannot use the sync dispatch of Rust."""

    async def process_request(self, request):
        return await self.get_response(request)


@pytest.mark.parametrize(
    "api_kwargs",
    [
        {},
        {"middleware": [PassThroughMiddleware]},
        {"django_middleware": ["django.middleware.clickjacking.XFrameOptionsMiddleware"]},
    ],
    ids=["plain", "python-middleware", "django-middleware"],
)
def test_inline_list_path_converts_a_later_object_by_attribute(api_kwargs):
    """The inline list path checks only the first element.

    A list that starts with a dict and holds an object later gets the result of
    the slow path, which converts the object by attribute.
    """
    api = BoltAPI(**api_kwargs)

    @api.get("/trivial")
    async def trivial() -> list[Row]:
        return [{"id": 1}, RowObject(2)]

    @api.get("/trivial-query")
    async def trivial_query(start: int = 1) -> list[Row]:
        return [{"id": start}, RowObject(start + 1)]

    @api.get("/awaits")
    async def awaits() -> list[Row]:
        await asyncio.sleep(0)
        return [{"id": 1}, RowObject(2)]

    @api.get("/awaits-query")
    async def awaits_query(start: int = 1) -> list[Row]:
        await asyncio.sleep(0)
        return [{"id": start}, RowObject(start + 1)]

    @api.get("/sync")
    def sync_rows() -> list[Row]:
        return [{"id": 1}, RowObject(2)]

    @api.get("/sync-query")
    def sync_rows_query(start: int = 1) -> list[Row]:
        return [{"id": start}, RowObject(start + 1)]

    with TestClient(api) as client:
        for path in ("/trivial", "/trivial-query", "/awaits", "/awaits-query", "/sync", "/sync-query"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text)
            assert resp.json() == ROWS_JSON, path
        # The handler gets its query parameter also when the list takes the slow path.
        for path in ("/trivial-query", "/awaits-query", "/sync-query"):
            resp = client.get(path, params={"start": 4})
            assert resp.status_code == 200, (path, resp.text)
            assert resp.json() == [{"id": 4, "note": "default"}, {"id": 5, "note": "default"}], path


def test_inline_list_path_reports_an_invalid_element(settings):
    """An invalid element still gives the validation error of that element."""
    # The 500 shows the validation errors only when DEBUG is on. This test reads them.
    settings.DEBUG = True
    api = BoltAPI()

    @api.get("/rows")
    async def rows() -> list[Row]:
        return [{"id": 1}, {"id": "x"}]

    with TestClient(api) as client:
        resp = client.get("/rows")
        assert resp.status_code == 500
        assert resp.json()["extra"]["validation_errors"] == [
            {"loc": ["response", "1", "id"], "msg": "Expected `int`, got `str`", "type": "validation_error"}
        ]


def test_list_route_that_returns_one_struct_gives_a_response_validation_error():
    """A list route checks the shape of the result, not only the type of the Struct."""
    api = BoltAPI()

    @api.get("/rows")
    async def rows() -> list[Row]:
        return Row(id=1)

    with TestClient(api) as client:
        resp = client.get("/rows")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Response validation error"


class Left(msgspec.Struct):
    left: int


class Right(msgspec.Struct):
    right: int


class Checked(msgspec.Struct):
    n: int

    def __post_init__(self):
        if self.n < 0:
            # msgspec wraps a TypeError or a ValueError of __post_init__ in a
            # ValidationError, but not this error.
            raise RuntimeError("n must not be negative")


@pytest.mark.parametrize(
    "api_kwargs", [{}, {"middleware": [PassThroughMiddleware]}], ids=["plain", "python-middleware"]
)
def test_inline_list_path_reports_each_error_as_the_slow_path_does(api_kwargs, settings):
    """Each error of the inline list path gives the error body of the slow path.

    An untagged union of Structs makes msgspec raise TypeError. An error in
    __post_init__ escapes msgspec as it is.
    """
    # The 500 shows the validation errors only when DEBUG is on. This test reads them.
    settings.DEBUG = True
    api = BoltAPI(**api_kwargs)

    @api.get("/slow-union")
    async def slow_union(request) -> list[Left | Right]:
        return [{"left": 1}]

    @api.get("/slow-checked")
    async def slow_checked(request) -> list[Checked]:
        return [{"n": -1}]

    @api.get("/trivial")
    async def trivial() -> list[Left | Right]:
        return [{"left": 1}]

    @api.get("/trivial-query")
    async def trivial_query(start: int = 1) -> list[Left | Right]:
        return [{"left": start}]

    @api.get("/awaits")
    async def awaits() -> list[Left | Right]:
        await asyncio.sleep(0)
        return [{"left": 1}]

    @api.get("/awaits-query")
    async def awaits_query(start: int = 1) -> list[Left | Right]:
        await asyncio.sleep(0)
        return [{"left": start}]

    @api.get("/sync")
    def sync_rows() -> list[Left | Right]:
        return [{"left": 1}]

    @api.get("/sync-query")
    def sync_rows_query(start: int = 1) -> list[Left | Right]:
        return [{"left": start}]

    @api.get("/checked")
    async def checked() -> list[Checked]:
        return [{"n": -1}]

    @api.get("/checked-sync")
    def checked_sync() -> list[Checked]:
        return [{"n": -1}]

    with TestClient(api) as client:
        slow_union_body = client.get("/slow-union").json()
        slow_checked_body = client.get("/slow-checked").json()
        assert slow_union_body["detail"] == "Response validation error"
        assert slow_checked_body["extra"]["validation_errors"][0]["msg"] == "n must not be negative"

        for path in ("/trivial", "/trivial-query", "/awaits", "/awaits-query", "/sync", "/sync-query"):
            resp = client.get(path)
            assert (resp.status_code, resp.json()) == (500, slow_union_body), path
        for path in ("/checked", "/checked-sync"):
            resp = client.get(path)
            assert (resp.status_code, resp.json()) == (500, slow_checked_body), path


class RowSerializer(Serializer):
    id: int


def test_response_validation_error_locates_the_error_in_the_response(settings):
    """A response validation error has the root "response", not the root of the request body."""
    # The 500 shows the validation errors only when DEBUG is on. This test reads them.
    settings.DEBUG = True
    api = BoltAPI()

    @api.get("/list")
    async def rows(request) -> list[Row]:
        return [{"id": 1}, {"id": "x"}]

    @api.get("/struct")
    def row() -> Row:
        return {"id": "x"}

    @api.get("/serializer")
    async def serialized() -> RowSerializer:
        return {"id": "x"}

    # The struct coercer wraps the msgspec error, so the field path of /struct
    # is in the message, not in the location.
    expected = {"/list": ["response", "1", "id"], "/struct": ["response"], "/serializer": ["response", "id"]}
    with TestClient(api) as client:
        for path, loc in expected.items():
            resp = client.get(path)
            assert resp.status_code == 500, path
            errors = resp.json()["extra"]["validation_errors"]
            assert [error["loc"] for error in errors] == [loc], (path, errors)
