"""The ``headers=`` dict of a response belongs to the caller."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.responses import HTML, JSON, File, FileResponse, PlainText, Redirect, Response, StreamingResponse
from django_bolt.testing import TestClient


@pytest.mark.parametrize(
    "make_response",
    [
        lambda shared: JSON({"ok": True}, headers=shared),
        lambda shared: Response({"ok": True}, headers=shared),
        lambda shared: PlainText("ok", headers=shared),
        lambda shared: HTML("<p>ok</p>", headers=shared),
        lambda shared: Redirect("/other", headers=shared),
        lambda shared: File(__file__, headers=shared),
        lambda shared: FileResponse(__file__, headers=shared),
        lambda shared: StreamingResponse(iter([b"ok"]), headers=shared),
    ],
    ids=["JSON", "Response", "PlainText", "HTML", "Redirect", "File", "FileResponse", "StreamingResponse"],
)
def test_a_handler_header_does_not_go_into_the_headers_dict_of_the_caller(make_response):
    """A header that the handler writes on its response stays on that response (#867)."""
    api = BoltAPI()
    shared = {"X-Service": "api"}
    count = 0

    @api.get("/tea")
    async def tea():
        nonlocal count
        count += 1
        response = make_response(shared)
        response.headers[f"X-Request-{count}"] = "yes"
        return response

    with TestClient(api) as client:
        first = client.get("/tea", follow_redirects=False)
        second = client.get("/tea", follow_redirects=False)

    assert shared == {"X-Service": "api"}
    assert first.headers["x-service"] == second.headers["x-service"] == "api"
    assert first.headers["x-request-1"] == "yes"
    assert second.headers["x-request-2"] == "yes"
    assert "x-request-1" not in second.headers
