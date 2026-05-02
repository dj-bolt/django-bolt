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
from django_bolt.param_functions import Query
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
