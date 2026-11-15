"""Options of ``@action``: the allowed HTTP methods, the status code and the response model."""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.testing import TestClient

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "QUERY"]


class Public(msgspec.Struct):
    name: str


async def _handler(self, request):
    return {}


@pytest.mark.parametrize("method", [m.lower() for m in ALL_METHODS])
def test_action_accepts_each_http_method(method):
    @action(methods=[method], detail=False)
    async def run(self, request):
        return {}

    assert run.methods == [method.upper()]


def test_action_rejects_an_unknown_method_and_lists_the_valid_ones():
    with pytest.raises(
        ValueError, match=r"Invalid HTTP method 'FETCH'\. Valid methods: DELETE, GET, HEAD, OPTIONS, PATCH"
    ):
        action(methods=["FETCH"], detail=False)(_handler)


def test_action_status_code_and_response_model_shape_the_response():
    api = BoltAPI()

    @api.viewset("/things")
    class ThingViewSet(ViewSet):
        @action(methods=["POST"], detail=False, status_code=202, response_model=Public)
        async def make(self, request):
            return {"name": "n", "secret": "s"}

        @action(methods=["GET"], detail=False)
        async def plain(self, request):
            return {"name": "n", "secret": "s"}

    with TestClient(api) as client:
        made = client.post("/things/make")
        assert (made.status_code, made.json()) == (202, {"name": "n"})
        plain = client.get("/things/plain")
        assert (plain.status_code, plain.json()) == (200, {"name": "n", "secret": "s"})
