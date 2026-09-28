"""App under test for query-string decoding over a real server.

The routes echo query values, a path value, and the URLs that the request builds.
A test can then see that a query decodes as Django `QueryDict` does, that a
`+` in a path stays a `+`, and that the URLs keep the query as it was sent.
"""

from __future__ import annotations

import json

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/search")
async def search(q: str):
    return {"q": q}


@api.get("/items/{name}")
def item(name: str):
    return {"name": name}


@api.get("/echo")
async def echo(request):
    return {
        "query": dict(request.query),
        "full_path": request.get_full_path(),
        "query_string": request.META["QUERY_STRING"],
    }


@api.websocket("/ws/search")
async def search_ws(websocket: WebSocket, q: str, flag: str | None = None):
    await websocket.accept()
    await websocket.send_text(json.dumps({"q": q, "flag": flag}))
