"""Validate generated OpenAPI specs against the official OpenAPI 3.1 schema.

The generator declares `"openapi": "3.1.0"`. `openapi-spec-validator`
runs the generated document through the official OpenAPI 3.1 JSON
Schema, catching structural problems (broken `$ref` paths, malformed
component shapes, invalid `type` values, missing required fields,
bad parameter `in` values, etc.) that targeted contract tests
wouldn't necessarily notice — this is in fact how the WebSocket
`Parameter`-vs-`OpenAPIHeader` bug fixed in this PR was found.

This is a *validity* check, not a *fidelity* check — it can't tell
us whether `str | None` was rendered with the right nullability arms,
only that whatever was rendered is well-formed OpenAPI 3.1. Pair
with the contract tests in `test_openapi_nested_schema.py` and
`test_openapi_schema_accuracy.py` for the field-level assertions.
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal

import msgspec
import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.param_functions import Cookie, Header, Path, Query
from django_bolt.serializers import Serializer, field
from django_bolt.testing import TestClient
from django_bolt.websocket.types import WebSocket

# --- Fixture types covering the surface area the generator handles ---


class ItemBase(msgspec.Struct):
    """Required field, optional+nullable field, defaulted field."""

    name: str
    description: str | None
    price_cents: int = 0


class ItemMutationIn(msgspec.Struct):
    name: str
    description: str | None = None


class Tag(msgspec.Struct):
    label: str


class ItemDetail(msgspec.Struct):
    """Nested struct + list-of-struct + null self-ref + Literal."""

    base: ItemBase
    tags: list[Tag]
    parent: ItemDetail | None = None
    visibility: Literal["public", "private", "draft"] = "draft"


class Status(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Filters(msgspec.Struct):
    """Query-param struct with Annotated constraints + nullable optionals."""

    page: Annotated[int, msgspec.Meta(ge=1)] = 1
    size: Annotated[int, msgspec.Meta(ge=1, le=100)] = 20
    status: Status | None = None


class Cat(msgspec.Struct, tag=True, tag_field="_type"):
    name: str
    meows: bool = True


class Dog(msgspec.Struct, tag=True, tag_field="_type"):
    name: str
    barks: bool = True


def _build_full_api() -> BoltAPI:
    """Wire up a representative slice of the generator's surface area
    so the validator exercises body+response, query params, path
    params, list responses, nested structs, unions, and enums."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Spec Validity Test", version="1.0.0"))

    @api.post("/items")
    async def create_item(request, data: ItemMutationIn) -> ItemBase:
        pass

    @api.get("/items/{item_id}")
    async def get_item(item_id: str) -> ItemDetail:
        pass

    @api.get("/items")
    async def list_items(query: Annotated[Filters, Query()]) -> list[ItemBase]:
        pass

    @api.put("/items/{item_id}")
    async def update_item(request, item_id: str, data: ItemMutationIn) -> ItemBase:
        pass

    @api.delete("/items/{item_id}")
    async def delete_item(item_id: str) -> dict:
        pass

    @api.websocket("/items/stream")
    async def stream_items(websocket: WebSocket) -> None:
        # Real-time stream — no message handling needed for the test;
        # the value here is exercising the WebSocket → OpenAPI path
        # (response headers under `101 Switching Protocols`).
        await websocket.accept()
        await websocket.close()

    @api.get("/items/cat_or_dog")
    async def union_items(request) -> Cat | Dog:
        pass

    return api


@pytest.fixture
def full_spec() -> dict:
    api = _build_full_api()
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200
        return response.json()


def test_generated_spec_is_valid_openapi_3_1(full_spec: dict) -> None:
    """The generated spec must validate against the official OpenAPI
    3.1 JSON Schema. `validate()` raises with a precise pointer when
    a constraint is violated."""
    validate(full_spec)


def test_generated_spec_declares_openapi_3_1(full_spec: dict) -> None:
    """Sanity check: the validator above only enforces 3.1 if the
    document declares 3.1. If the generator ever switches versions,
    the validator's strictness changes, so pin the version here."""
    assert full_spec.get("openapi", "").startswith("3.1.")


def test_query_operation_declares_openapi_3_2_and_validates() -> None:
    """A QUERY route is only representable as a Path Item operation in
    OpenAPI 3.2.0+. Under the default 3.1.0 declaration the generated
    `query` operation fails strict 3.1 validation ("unevaluatedProperties:
    false"), so the generator must bump the declared version to 3.2.0
    whenever a QUERY route is present — and the result must still validate."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Query API", version="1.0.0"))

    @api.query("/search")
    async def search(request, data: ItemMutationIn) -> ItemBase:
        pass

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()

    assert spec.get("openapi") == "3.2.0"
    assert "query" in spec["paths"]["/search"]
    validate(spec)


@pytest.mark.parametrize(("marker", "param_in"), [(Header, "header"), (Cookie, "cookie"), (Query, "query")])
@pytest.mark.parametrize("empty", [[], (), frozenset()], ids=["list", "tuple", "frozenset"])
def test_list_parameter_with_an_empty_default_validates(marker, param_in, empty) -> None:
    """An empty sequence default must be a valid value of the parameter schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="List API", version="1.0.0"))

    async def tagged(tags=empty):
        pass

    async def named(names=["a"]):  # noqa: B006
        pass

    # The module has postponed annotations, so the test sets the evaluated types.
    tagged.__annotations__ = {"tags": Annotated[list[str], marker()], "return": dict}
    named.__annotations__ = {"names": Annotated[list[str], marker()], "return": dict}
    api.get("/tagged")(tagged)
    api.get("/named")(named)

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()

    validate(spec)
    (tags,) = spec["paths"]["/tagged"]["get"]["parameters"]
    assert tags["in"] == param_in
    assert tags["schema"]["default"] == []
    assert "minItems" not in tags["schema"]
    # A header or a cookie that is present gives one value. A default with one item keeps the minimum.
    (names,) = spec["paths"]["/named"]["get"]["parameters"]
    assert names["schema"].get("minItems") == (None if param_in == "query" else 1)


class EmptyTagsModel(msgspec.Struct):
    tags: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()


@pytest.mark.parametrize("marker", [Header, Cookie])
def test_model_sequence_field_with_an_empty_default_validates(marker) -> None:
    """An empty tuple or frozenset default of a model field must be a valid value of the field schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="List API", version="1.0.0"))

    async def tagged(model):
        pass

    tagged.__annotations__ = {"model": Annotated[EmptyTagsModel, marker()], "return": dict}
    api.get("/tagged")(tagged)

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()

    validate(spec)
    for parameter in spec["paths"]["/tagged"]["get"]["parameters"]:
        assert parameter["schema"]["default"] == []
        assert "minItems" not in parameter["schema"]


@pytest.mark.parametrize(
    ("marker", "headers", "expected"),
    [
        (Header, [("tags", "a"), ("tags", "b")], ["b"]),
        (Header, {"tags": "a,b"}, ["a,b"]),
        (Cookie, {"cookie": "tags=a; tags=b"}, ["b"]),
        (Cookie, {"cookie": "tags=a,b"}, ["a,b"]),
    ],
    ids=["header-repeated", "header-comma", "cookie-repeated", "cookie-comma"],
)
def test_header_or_cookie_list_gets_one_item_as_its_schema_says(marker, headers, expected) -> None:
    """Bolt keeps the last value of a repeated name and does not split a comma list, so ``maxItems: 1`` is true (#871)."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="List API", version="1.0.0"))

    async def tagged(tags=["default"]):  # noqa: B006
        return tags

    tagged.__annotations__ = {"tags": Annotated[list[str], marker()]}
    api.get("/tagged")(tagged)

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()
        assert client.get("/tagged", headers=headers).json() == expected
        assert client.get("/tagged").json() == ["default"]

    validate(spec)
    (tags,) = spec["paths"]["/tagged"]["get"]["parameters"]
    assert tags["schema"]["maxItems"] == 1
    assert tags["schema"]["default"] == ["default"]


class _TagList(list):
    pass


@pytest.mark.parametrize("marker", [Path, Header, Cookie])
@pytest.mark.parametrize(
    "default",
    [["a", "b"], ("a", "b"), {"a", "b"}, _TagList(["a", "b"])],
    ids=["list", "tuple", "set", "list-subclass"],
)
def test_one_value_list_parameter_refuses_a_default_of_two_items(marker, default) -> None:
    """A path, header or cookie list gets at most one item, so a default of two items fails at registration (#871)."""
    api = BoltAPI()

    async def tagged(tags=default):
        return tags

    tagged.__annotations__ = {"tags": Annotated[list[str], marker()]}
    route = "/tagged/{tags}" if marker is Path else "/tagged"
    with pytest.raises(TypeError, match=r"'tags'.*default .* has 2 items"):
        api.get(route)(tagged)


@pytest.mark.parametrize("marker", [Path, Header, Cookie])
def test_one_value_pair_parameter_names_its_type_before_its_default(marker) -> None:
    """A type that cannot hold one item gives that error, not the error of its default."""
    api = BoltAPI()

    async def paired(pair=("a", "b")):
        return pair

    paired.__annotations__ = {"pair": Annotated[tuple[str, str], marker()]}
    route = "/paired/{pair}" if marker is Path else "/paired"
    with pytest.raises(TypeError, match="cannot hold one item"):
        api.get(route)(paired)


@pytest.mark.parametrize("marker", [Header, Cookie])
@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[list[str], msgspec.Meta(min_length=0)],
        Annotated[list[str], msgspec.Meta(min_length=0)] | None,
    ],
    ids=["constrained", "optional-constrained"],
)
def test_constrained_one_value_list_refuses_a_default_of_two_items(marker, annotation) -> None:
    """A msgspec.Meta constraint keeps Annotated on the type. The check still finds the list."""
    api = BoltAPI()

    async def tagged(tags=["a", "b"]):  # noqa: B006
        return tags

    tagged.__annotations__ = {"tags": Annotated[annotation, marker()]}
    with pytest.raises(TypeError, match=r"'tags'.*default .* has 2 items"):
        api.get("/tagged")(tagged)


@pytest.mark.parametrize(
    ("marker", "annotation"),
    [
        (Header, list[str]),
        (Cookie, list[str]),
        (Header, Annotated[list[str], msgspec.Meta(min_length=0)]),
        (Cookie, Annotated[list[str], msgspec.Meta(min_length=0)]),
    ],
    ids=["header", "cookie", "header-constrained", "cookie-constrained"],
)
def test_one_value_list_parameter_accepts_a_default_of_one_item(marker, annotation) -> None:
    """A default of one item is valid for ``maxItems: 1``."""
    api = BoltAPI()

    async def tagged(tags=["a"]):  # noqa: B006
        return tags

    tagged.__annotations__ = {"tags": Annotated[annotation, marker()]}
    api.get("/tagged")(tagged)

    with TestClient(api) as client:
        assert client.get("/tagged").json() == ["a"]


class TaggedModel(msgspec.Struct):
    tags: tuple[str, ...] = ("a", "b")


class TaggedSerializer(Serializer):
    tags: list[str] = field(default=["a", "b"])


class TaggedFactorySerializer(Serializer):
    tags: list[str] = field(default_factory=lambda: ["a", "b"])


@pytest.mark.parametrize("marker", [Header, Cookie])
@pytest.mark.parametrize(
    "model",
    [TaggedModel, TaggedSerializer, TaggedFactorySerializer],
    ids=["struct", "serializer-default", "serializer-default-factory"],
)
def test_header_or_cookie_model_refuses_a_list_field_default_of_two_items(marker, model) -> None:
    """A list field of a header or cookie model gets at most one item, as a plain parameter does (#871)."""
    api = BoltAPI()

    async def tagged(model):
        return model.tags

    tagged.__annotations__ = {"model": Annotated[model, marker()]}
    with pytest.raises(TypeError, match=r"'tags'.*default .* has 2 items"):
        api.get("/tagged")(tagged)


class TaggedFactoryModel(msgspec.Struct):
    tags: list[str] = msgspec.field(default_factory=lambda: ["a", "b"])


@pytest.mark.parametrize("marker", [Header, Cookie])
def test_header_or_cookie_model_with_a_default_factory_registers(marker) -> None:
    """The schema of a field with a ``default_factory`` has no default, so it stays valid."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="List API", version="1.0.0"))

    async def tagged(model):
        return model.tags

    tagged.__annotations__ = {"model": Annotated[TaggedFactoryModel, marker()]}
    api.get("/tagged")(tagged)

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()

    validate(spec)
    (tags,) = spec["paths"]["/tagged"]["get"]["parameters"]
    assert "default" not in tags["schema"]


class HiddenTagsSerializer(Serializer):
    tags: list[str] = field(default=["a", "b"], include_in_schema=False)


@pytest.mark.parametrize("marker", [Header, Cookie])
def test_header_or_cookie_model_skips_a_field_that_the_schema_hides(marker) -> None:
    """A field with ``include_in_schema=False`` has no array schema, so its default can have many items."""
    api = BoltAPI()

    async def handler(model):
        return model.tags

    handler.__annotations__ = {"model": Annotated[HiddenTagsSerializer, marker()]}
    api.get("/model")(handler)


def test_query_free_spec_keeps_openapi_3_1(full_spec: dict) -> None:
    """The 3.2.0 bump is conditional: APIs without a QUERY route must keep
    declaring 3.1.0 for maximum downstream tooling compatibility."""
    assert full_spec.get("openapi", "").startswith("3.1.")


def test_no_dangling_refs_in_components(full_spec: dict) -> None:
    """Every `$ref` in the spec must resolve to a `components.schemas`
    entry. Catches the bug class where a Struct is referenced inside
    a property but never registered as a component (e.g. via the
    msgspec round-trip path emitting a $ref to a nested type that
    bolt didn't add to components)."""
    schemas = full_spec.get("components", {}).get("schemas", {})

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref[len("#/components/schemas/") :]
                assert name in schemas, (
                    f"$ref to {name!r} but no such component schema. Available: {sorted(schemas.keys())}"
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(full_spec)
