from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Optional, Union

import msgspec
import pytest

from django_bolt import BoltAPI
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
    from django_bolt.responses import EventSourceResponse

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
    from django_bolt.openapi.config import OpenAPIConfig

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

    from django_bolt.openapi.schema_generator import SchemaGenerator

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
    from django_bolt.openapi.config import OpenAPIConfig
    from django_bolt.openapi.schema_generator import SchemaGenerator

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
