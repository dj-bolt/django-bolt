"""
Tests for OpenAPI schema generation with nested msgspec Structs.

Regression test for: nested serializers showing as empty objects {} in OpenAPI
schema instead of expanding their fields.
"""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.testing import TestClient


class Address(msgspec.Struct):
    street: str
    city: str


class Child(msgspec.Struct):
    id: str
    name: str


class ChildWithAddress(msgspec.Struct):
    id: str
    name: str
    address: Address


class Parent(msgspec.Struct):
    id: str
    child: Child


class ParentWithList(msgspec.Struct):
    id: str
    children: list[Child]


class ParentWithOptional(msgspec.Struct):
    id: str
    maybe_child: Child | None = None


class ParentDeepNested(msgspec.Struct):
    id: str
    child: ChildWithAddress


class TreeNode(msgspec.Struct):
    value: str
    parent: TreeNode | None = None


class Cat(msgspec.Struct, tag="cat"):
    meow: str


class Dog(msgspec.Struct, tag="dog"):
    bark: str


class PetOwner(msgspec.Struct):
    name: str
    pet: Cat | Dog


class SharedParent(msgspec.Struct):
    first: Child
    second: Child
    others: list[Child]


def _get_schema(api: BoltAPI) -> dict:
    """Helper to get OpenAPI schema dict from an API instance."""
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200
        return response.json()


def test_nested_struct_has_properties():
    """Nested struct fields must include their properties in the schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/parent")
    async def get_parent() -> Parent:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Child schema must exist and have its fields
    assert "Child" in schemas, f"Child schema missing from components. Got: {list(schemas.keys())}"
    child_schema = schemas["Child"]
    assert "properties" in child_schema, f"Child schema has no properties: {child_schema}"
    assert "id" in child_schema["properties"], "Child.id missing"
    assert "name" in child_schema["properties"], "Child.name missing"
    assert child_schema["properties"]["id"]["type"] == "string"
    assert child_schema["properties"]["name"]["type"] == "string"

    # Parent.child should reference the Child component
    parent_schema = schemas["Parent"]
    child_field = parent_schema["properties"]["child"]
    assert "$ref" in child_field, f"Parent.child should be a $ref, got: {child_field}"
    assert child_field["$ref"] == "#/components/schemas/Child"


def test_list_of_nested_structs_has_properties():
    """list[Struct] fields must reference the struct schema with full properties."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/parents")
    async def get_parents() -> ParentWithList:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Child schema must exist with fields
    assert "Child" in schemas
    assert "id" in schemas["Child"]["properties"]
    assert "name" in schemas["Child"]["properties"]

    # ParentWithList.children should be array of $ref
    parent_schema = schemas["ParentWithList"]
    children_field = parent_schema["properties"]["children"]
    assert children_field["type"] == "array"
    assert "$ref" in children_field["items"], f"List items should be $ref, got: {children_field['items']}"
    assert children_field["items"]["$ref"] == "#/components/schemas/Child"


def test_optional_nested_struct_has_properties():
    """Optional[Struct] fields must reference the struct schema with full properties."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/parent")
    async def get_parent() -> ParentWithOptional:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Child schema must exist with fields
    assert "Child" in schemas
    assert "id" in schemas["Child"]["properties"]
    assert "name" in schemas["Child"]["properties"]


def test_deeply_nested_structs_have_properties():
    """Structs nested multiple levels deep must all have their properties."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/parent")
    async def get_parent() -> ParentDeepNested:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Address (2 levels deep) must exist with fields
    assert "Address" in schemas, f"Address schema missing. Got: {list(schemas.keys())}"
    address_schema = schemas["Address"]
    assert "street" in address_schema["properties"], "Address.street missing"
    assert "city" in address_schema["properties"], "Address.city missing"

    # ChildWithAddress.address should reference Address
    child_schema = schemas["ChildWithAddress"]
    assert child_schema["properties"]["address"]["$ref"] == "#/components/schemas/Address"

    # ParentDeepNested.child should reference ChildWithAddress
    parent_schema = schemas["ParentDeepNested"]
    assert parent_schema["properties"]["child"]["$ref"] == "#/components/schemas/ChildWithAddress"


def test_nested_struct_as_request_body():
    """Nested structs in request bodies must also have their properties expanded."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.post("/parent")
    async def create_parent(data: Parent) -> Parent:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Child schema must exist with fields for both request and response
    assert "Child" in schemas
    assert "id" in schemas["Child"]["properties"]
    assert "name" in schemas["Child"]["properties"]


def test_self_referential_struct_does_not_recurse():
    """Self-referential structs must not cause infinite recursion."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/tree")
    async def get_tree() -> TreeNode:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    assert "TreeNode" in schemas
    tree_schema = schemas["TreeNode"]
    assert "value" in tree_schema["properties"]
    # Optional self-references are wrapped so the schema can carry default: null.
    parent = tree_schema["properties"]["parent"]
    assert parent["default"] is None
    assert parent["allOf"] == [{"$ref": "#/components/schemas/TreeNode"}]


def test_multi_type_union_produces_any_of():
    """Union[A, B] (tagged) should produce anyOf with refs to both schemas."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/owner")
    async def get_owner() -> PetOwner:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Both Cat and Dog schemas must be registered with their fields
    assert "Cat" in schemas
    assert "meow" in schemas["Cat"]["properties"]
    assert "Dog" in schemas
    assert "bark" in schemas["Dog"]["properties"]

    # pet field should use anyOf
    pet_field = schemas["PetOwner"]["properties"]["pet"]
    assert "anyOf" in pet_field, f"Expected anyOf for union field, got: {pet_field}"
    refs = {item["$ref"] for item in pet_field["anyOf"]}
    assert refs == {"#/components/schemas/Cat", "#/components/schemas/Dog"}


def test_shared_nested_struct_registered_once():
    """A struct used in multiple fields should be registered as a single component."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test", version="1.0.0"))

    @api.get("/shared")
    async def get_shared() -> SharedParent:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]

    # Child must appear exactly once as a component
    assert "Child" in schemas
    child_schema = schemas["Child"]
    assert "id" in child_schema["properties"]
    assert "name" in child_schema["properties"]

    # All three fields should reference the same component
    shared = schemas["SharedParent"]["properties"]
    assert shared["first"]["$ref"] == "#/components/schemas/Child"
    assert shared["second"]["$ref"] == "#/components/schemas/Child"
    assert shared["others"]["items"]["$ref"] == "#/components/schemas/Child"
