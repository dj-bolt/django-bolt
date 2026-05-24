from __future__ import annotations

import pytest

pytestmark = pytest.mark.server_integration


FEED_API_BODY = """
import msgspec


class PostActivity(msgspec.Struct, tag="post"):
    id: int
    actor: str
    title: str
    body: str


class CommentActivity(msgspec.Struct, tag="comment"):
    id: int
    actor: str
    post_id: int
    text: str


class LikeActivity(msgspec.Struct, tag="like"):
    id: int
    actor: str
    target_id: int
    target_kind: str


FeedItem = PostActivity | CommentActivity | LikeActivity


@api.get("/feed/{item_id}", response_model=FeedItem)
async def feed_item(item_id: int) -> FeedItem:
    kind = item_id % 3
    if kind == 0:
        return PostActivity(id=item_id, actor="alice", title="hello", body="world")
    if kind == 1:
        return CommentActivity(id=item_id, actor="bob", post_id=item_id - 1, text="nice")
    return LikeActivity(id=item_id, actor="carol", target_id=item_id - 2, target_kind="post")


@api.get("/feed", response_model=list[FeedItem])
async def feed() -> list[FeedItem]:
    return [
        PostActivity(id=0, actor="alice", title="t0", body="b0"),
        CommentActivity(id=1, actor="bob", post_id=0, text="c1"),
        LikeActivity(id=2, actor="carol", target_id=0, target_kind="post"),
    ]
"""


def test_union_response_each_branch_carries_tag(make_server_project):
    project = make_server_project(project_api_body=FEED_API_BODY)

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
    project = make_server_project(project_api_body=FEED_API_BODY)

    with project.start() as server:
        response = server.get("/feed")

    assert response.status_code == 200
    body = response.json()
    assert [item["type"] for item in body] == ["post", "comment", "like"]
    assert body[0]["title"] == "t0"
    assert body[1]["post_id"] == 0
    assert body[2]["target_kind"] == "post"


def test_union_response_openapi_advertises_all_branches(make_server_project):
    project = make_server_project(project_api_body=FEED_API_BODY)

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
