from __future__ import annotations

import pytest

from django_bolt.testing import TestClient

from .apps import app_module, union_response

pytestmark = pytest.mark.server_integration


def test_union_response_each_branch_carries_tag(make_server_project):
    project = make_server_project(api_module=app_module("union_response"))

    with project.start() as server:
        post = server.get("/feed/0")
        comment = server.get("/feed/1")
        like = server.get("/feed/2")

    assert post.status_code == 200
    assert post.json() == {
        "type": "post",
        "id": 0,
        "actor": "alice",
        "title": "hello",
        "body": "world",
    }

    assert comment.status_code == 200
    assert comment.json() == {
        "type": "comment",
        "id": 1,
        "actor": "bob",
        "post_id": 0,
        "text": "nice",
    }

    assert like.status_code == 200
    assert like.json() == {
        "type": "like",
        "id": 2,
        "actor": "carol",
        "target_id": 0,
        "target_kind": "post",
    }


def test_union_response_list_serializes_mixed_tags(make_server_project):
    project = make_server_project(api_module=app_module("union_response"))

    with project.start() as server:
        response = server.get("/feed")

    assert response.status_code == 200
    body = response.json()
    assert [item["type"] for item in body] == ["post", "comment", "like"]
    assert body[0]["title"] == "t0"
    assert body[1]["post_id"] == 0
    assert body[2]["target_kind"] == "post"


def test_union_response_openapi_advertises_all_branches(make_server_project):
    project = make_server_project(api_module=app_module("union_response"))

    with project.start() as server:
        response = server.get("/docs/openapi.json")

    assert response.status_code == 200
    spec = response.json()

    feed_item_schema = spec["paths"]["/feed/{item_id}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    union_entries = feed_item_schema.get("oneOf") or feed_item_schema.get("anyOf")
    assert union_entries is not None, f"expected oneOf/anyOf in union response schema, got {feed_item_schema!r}"

    schemas = spec["components"]["schemas"]
    branch_names = set()
    for entry in union_entries:
        ref = entry.get("$ref")
        assert ref, f"expected $ref in union entry, got {entry!r}"
        name = ref.rsplit("/", 1)[-1]
        assert name in schemas, f"$ref {ref} not registered as a component"
        branch_names.add(name)
    assert branch_names == {"PostActivity", "CommentActivity", "LikeActivity"}

    feed_list_schema = spec["paths"]["/feed"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert feed_list_schema.get("type") == "array"
    items_schema = feed_list_schema["items"]
    list_union = items_schema.get("oneOf") or items_schema.get("anyOf")
    assert list_union is not None, f"expected oneOf/anyOf in list[Union] items schema, got {items_schema!r}"


def test_union_response_each_branch_carries_tag_in_process():
    """Same app module, in-process — fast confirmation that each union branch
    serializes with its tag. The subprocess tests cover the real TCP + OpenAPI path."""
    with TestClient(union_response.api) as client:
        post = client.get("/feed/0")
        comment = client.get("/feed/1")
        like = client.get("/feed/2")

    assert post.json() == {"type": "post", "id": 0, "actor": "alice", "title": "hello", "body": "world"}
    assert comment.json() == {"type": "comment", "id": 1, "actor": "bob", "post_id": 0, "text": "nice"}
    assert like.json() == {"type": "like", "id": 2, "actor": "carol", "target_id": 0, "target_kind": "post"}
