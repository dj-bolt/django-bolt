"""App under test for Rust's sync-dispatch bypass and bare-bytes response wire.

These routes are sync-dispatch eligible (sync or trivially-async handlers, no
middleware/signals), so in the real server they run through
``handle_request``'s ``can_sync_dispatch`` branch — including the bare-bytes
JSON fast path and Rust-prebound optional/required params. ``TestClient``
always uses the async dispatch, so only a ``runbolt`` subprocess exercises
this code; the routes are intentionally spread across the wire shapes:

- default-status JSON (bare-bytes wire)
- route-level status_code=201 JSON (bare-bytes wire with per-route status)
- PlainText (tuple wire)
- required path param + optional query params (Rust arg prebinding)
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.responses import PlainText

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/plain-json")
async def plain_json():
    """Trivially-async, no params → bare-bytes JSON wire."""
    return {"message": "sync-dispatch", "n": 1}


@api.get("/created", status_code=201)
async def created():
    """Route-level status_code=201: the bare-bytes wire must reproduce the
    per-route default status (Rust reads RouteMetadata.default_status_code)."""
    return {"created": True}


@api.get("/text")
def text():
    """Sync handler, non-JSON response → tuple wire with plaintext meta."""
    return PlainText("hello-sync")


@api.get("/items/{item_id}")
async def get_item(item_id: int, q: str = "none", limit: int | None = None):
    """Required path param + optional query params → Rust arg prebinding.

    ``q``/``limit`` exercise omit-on-missing (the function defaults apply).
    Trivially-async → sync dispatch + bare-bytes wire.
    """
    return {"id": item_id, "q": q, "limit": limit}


@api.get("/search")
async def search(term: str | None):
    """Optional annotation with NO function default → Rust injects None
    explicitly when the query param is missing (inject_none binding)."""
    return {"term": term}


@api.get("/strict")
async def strict(token: str):
    """Required query param: when missing, Rust prebinding bails and the
    Python injector fallback must still raise the structured 422."""
    return {"token": token}
