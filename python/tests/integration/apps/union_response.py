"""App under test for tagged-union response serialization + OpenAPI.

Each branch is a tagged ``msgspec.Struct``; handlers return a single union value
and a ``list`` of union values, with ``response_model`` advertising all branches.
"""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


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
