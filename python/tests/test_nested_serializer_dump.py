"""A Serializer in a response gives its dump(), wherever it is in the value.

msgspec encodes each Struct natively, so a Serializer that the response
holds in a container got all of its fields: also its ``write_only`` fields,
and none of its computed fields. Each route here returns such a value on a
different path of the dispatch.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

from django_bolt import BoltAPI, EventSourceResponse, ServerSentEvent, WebSocket, _json
from django_bolt._core import find_serializer
from django_bolt._serializer_dump import SCAN_KINDS, classify
from django_bolt.responses import JSON, Response
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.testing import TestClient, WebSocketTestClient

SECRET = "s3cret-value"


class Account(Serializer):
    id: int
    password: str = field(write_only=True)

    @computed_field
    def label(self) -> str:
        return f"user-{self.id}"


class Public(Serializer):
    """A Serializer with no write-only or computed field."""

    id: int


class Envelope(msgspec.Struct):
    account: Account
    note: str = "n"


class LooseEnvelope(msgspec.Struct):
    data: Any


@dataclasses.dataclass
class Box:
    account: Any


class Renamed(msgspec.Struct, rename="camel", frozen=True):
    """A dumped field must keep the encoding options of the Struct."""

    inner_value: Any


@dataclasses.dataclass(frozen=True, slots=True)
class FrozenBox:
    item: Any


class Page[T](msgspec.Struct):
    """A generic envelope: a type variable can hold a Serializer."""

    items: list[T]


class Wrapper(Serializer):
    """A Serializer whose dump() keeps the values of a plain dict field."""

    payload: dict


class Counted(msgspec.Struct):
    """The annotation of the field cannot hold a Serializer, so the scan skips it."""

    n: int


DUMPED = {"id": 1, "label": "user-1"}


def account() -> Account:
    return Account(id=1, password=SECRET)


def _check(response, expected) -> None:
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert response.json() == expected


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    # Routes with no return annotation.
    @api.get("/untyped/sync")
    def untyped_sync():
        return {"account": account()}

    @api.get("/untyped/sync-params/{n}")
    def untyped_sync_params(n: int):
        return {"account": account(), "n": n}

    @api.get("/untyped/trivial")
    async def untyped_trivial():
        return {"account": account()}

    @api.get("/untyped/trivial-params/{n}")
    async def untyped_trivial_params(n: int):
        return {"account": account(), "n": n}

    @api.get("/untyped/awaiting")
    async def untyped_awaiting():
        await msgspec_noop()
        return {"account": account()}

    @api.get("/untyped/request-only")
    async def untyped_request_only(request):
        return {"account": account()}

    @api.get("/untyped/request-only-sync")
    def untyped_request_only_sync(request):
        return {"account": account()}

    @api.get("/untyped/mixed-list")
    def untyped_mixed_list():
        return [{"x": 1}, account()]

    @api.get("/untyped/mixed-list-async")
    async def untyped_mixed_list_async():
        await msgspec_noop()
        return [{"x": 1}, account()]

    @api.get("/untyped/list-of-dicts")
    def untyped_list_of_dicts():
        return [{"account": account()}]

    @api.get("/untyped/tuple-in-dict")
    def untyped_tuple_in_dict():
        return {"pair": (account(), account())}

    @api.get("/untyped/struct")
    def untyped_struct():
        return Envelope(account=account())

    @api.get("/untyped/struct-async")
    async def untyped_struct_async():
        await msgspec_noop()
        return Envelope(account=account())

    @api.get("/untyped/dataclass")
    def untyped_dataclass():
        return {"box": Box(account=account())}

    @api.get("/untyped/public")
    def untyped_public():
        return {"public": Public(id=1)}

    @api.get("/untyped/generic-page")
    def untyped_generic_page():
        return Page(items=[account()])

    @api.get("/untyped/serializer-in-serializer-dict")
    def untyped_serializer_in_serializer_dict():
        return {"wrapper": Wrapper(payload={"account": account()})}

    # Routes with a return annotation.
    @api.get("/typed/dict")
    def typed_dict() -> dict:
        return {"account": account()}

    @api.get("/typed/dict-async")
    async def typed_dict_async() -> dict:
        await msgspec_noop()
        return {"account": account()}

    @api.get("/typed/dict-of-serializers")
    def typed_dict_of_serializers() -> dict[str, Account]:
        return {"account": account()}

    @api.get("/typed/list-of-dicts")
    def typed_list_of_dicts() -> list[dict]:
        return [{"account": account()}]

    @api.get("/typed/any")
    def typed_any() -> Any:
        return {"account": account()}

    @api.get("/typed/struct")
    def typed_struct() -> Envelope:
        return Envelope(account=account())

    @api.get("/typed/struct-no-validation", validate_response=False)
    def typed_struct_no_validation() -> Envelope:
        return Envelope(account=account())

    @api.get("/typed/loose-struct")
    def typed_loose_struct() -> LooseEnvelope:
        return LooseEnvelope(data={"account": account()})

    @api.get("/typed/list-of-structs")
    async def typed_list_of_structs() -> list[Envelope]:
        await msgspec_noop()
        return [Envelope(account=account())]

    # Response classes.
    @api.get("/json/serializer")
    def json_serializer():
        return JSON(account())

    @api.get("/json/dict")
    async def json_dict():
        return JSON({"account": account()})

    @api.get("/response/dict")
    def response_dict():
        return Response({"account": account()})

    # Routes with a response model for each status.
    @api.get("/multi/tuple", response_model={200: dict, 404: dict})
    def multi_tuple():
        return 200, {"account": account()}

    @api.get("/multi/tuple-async", response_model={200: dict, 404: dict})
    async def multi_tuple_async():
        await msgspec_noop()
        return 200, {"account": account()}

    @api.get("/multi/bare", response_model={200: dict, 404: dict})
    def multi_bare():
        return {"account": account()}

    @api.get("/multi/list", response_model={200: list, 404: dict})
    def multi_list():
        return 200, [{"x": 1}, account()]

    # Streams.
    @api.get("/stream/ndjson")
    def stream_ndjson() -> Iterator[dict]:
        yield {"account": account()}

    @api.get("/stream/sse")
    async def stream_sse():
        async def events():
            yield {"account": account()}
            yield ServerSentEvent(data={"account": account()}, event="update")

        return EventSourceResponse(events())

    with TestClient(api) as test_client:
        yield test_client


async def msgspec_noop() -> None:
    """Make an async handler await, so that it takes the awaiting dispatch."""


@pytest.mark.parametrize(
    "path",
    [
        "/untyped/sync",
        "/untyped/trivial",
        "/untyped/awaiting",
        "/untyped/request-only",
        "/untyped/request-only-sync",
        "/typed/dict",
        "/typed/dict-async",
        "/typed/dict-of-serializers",
        "/typed/any",
        "/json/dict",
        "/response/dict",
        "/multi/tuple",
        "/multi/tuple-async",
        "/multi/bare",
    ],
)
def test_a_serializer_in_a_dict_gives_its_dump(client, path):
    _check(client.get(path), {"account": DUMPED})


@pytest.mark.parametrize("path", ["/untyped/sync-params/3", "/untyped/trivial-params/3"])
def test_a_serializer_in_a_dict_gives_its_dump_on_a_route_with_parameters(client, path):
    _check(client.get(path), {"account": DUMPED, "n": 3})


@pytest.mark.parametrize("path", ["/untyped/mixed-list", "/untyped/mixed-list-async", "/multi/list"])
def test_a_serializer_after_a_dict_in_a_list_gives_its_dump(client, path):
    _check(client.get(path), [{"x": 1}, DUMPED])


@pytest.mark.parametrize("path", ["/untyped/list-of-dicts", "/typed/list-of-dicts"])
def test_a_serializer_in_a_dict_in_a_list_gives_its_dump(client, path):
    _check(client.get(path), [{"account": DUMPED}])


def test_a_serializer_in_a_tuple_gives_its_dump(client):
    _check(client.get("/untyped/tuple-in-dict"), {"pair": [DUMPED, DUMPED]})


@pytest.mark.parametrize(
    "path",
    ["/untyped/struct", "/untyped/struct-async", "/typed/struct", "/typed/struct-no-validation"],
)
def test_a_serializer_in_a_struct_field_gives_its_dump(client, path):
    _check(client.get(path), {"account": DUMPED, "note": "n"})


def test_a_serializer_in_an_any_field_of_a_struct_gives_its_dump(client):
    _check(client.get("/typed/loose-struct"), {"data": {"account": DUMPED}})


def test_a_serializer_in_a_list_of_structs_gives_its_dump(client):
    _check(client.get("/typed/list-of-structs"), [{"account": DUMPED, "note": "n"}])


def test_a_serializer_in_a_dataclass_gives_its_dump(client):
    _check(client.get("/untyped/dataclass"), {"box": {"account": DUMPED}})


def test_a_serializer_in_json_gives_its_dump(client):
    _check(client.get("/json/serializer"), DUMPED)


def test_a_serializer_in_a_generic_struct_gives_its_dump(client):
    _check(client.get("/untyped/generic-page"), {"items": [DUMPED]})


def test_a_serializer_in_the_dump_of_another_serializer_gives_its_dump(client):
    _check(client.get("/untyped/serializer-in-serializer-dict"), {"wrapper": {"payload": {"account": DUMPED}}})


def test_a_serializer_with_no_write_only_field_keeps_its_fields(client):
    _check(client.get("/untyped/public"), {"public": {"id": 1}})


def test_a_serializer_in_a_json_stream_chunk_gives_its_dump(client):
    response = client.get("/stream/ndjson")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert [json.loads(line) for line in response.content.splitlines()] == [{"account": DUMPED}]


def test_a_serializer_in_a_server_sent_event_gives_its_dump(client):
    response = client.get("/stream/sse")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    data = [json.loads(line[len("data: ") :]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert data == [{"account": DUMPED}, {"account": DUMPED}]


@pytest.mark.asyncio
async def test_a_serializer_sent_on_a_websocket_gives_its_dump():
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json(account())
        await websocket.send_json({"account": account()})

    async with WebSocketTestClient(api, "/ws") as ws_client:
        assert await ws_client.receive_json() == DUMPED
        assert await ws_client.receive_json() == {"account": DUMPED}


def test_an_encode_with_a_custom_hook_gives_the_dump_of_each_serializer():
    assert json.loads(_json.encode({"account": account()}, serializer=str)) == {"account": DUMPED}


def test_a_deeply_nested_response_fails_as_before():
    """A value deeper than the recursion limit gives a 500 and the worker keeps serving."""
    api = BoltAPI()
    deep: list[Any] = []
    for _ in range(100_000):
        deep = [deep]

    @api.get("/deep")
    def deep_route():
        return {"deep": deep, "account": account()}

    @api.get("/ok")
    def ok():
        return {"account": account()}

    with TestClient(api) as test_client:
        response = test_client.get("/deep")
        assert response.status_code == 500
        assert SECRET not in response.text
        _check(test_client.get("/ok"), {"account": DUMPED})


def _reference(value: Any) -> Any:
    """Convert ``value`` the plain way: each Serializer gives its dump()."""
    if isinstance(value, Serializer):
        return _reference(value.dump())
    if isinstance(value, dict):
        return {key: _reference(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reference(item) for item in value]
    if isinstance(value, Renamed):
        return {"innerValue": _reference(value.inner_value)}
    if isinstance(value, FrozenBox):
        return {"item": _reference(value.item)}
    if isinstance(value, Counted):
        return {"n": value.n}
    return value


def _holds_serializer(value: Any) -> bool:
    if isinstance(value, Serializer):
        return True
    if isinstance(value, dict):
        return any(_holds_serializer(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_holds_serializer(item) for item in value)
    if isinstance(value, Renamed):
        return _holds_serializer(value.inner_value)
    if isinstance(value, FrozenBox):
        return _holds_serializer(value.item)
    return False


_leaves = st.one_of(
    st.integers(),
    st.text(max_size=4),
    st.none(),
    st.booleans(),
    st.builds(lambda n: Account(id=n, password=SECRET), st.integers(0, 9)),
    st.builds(Public, id=st.integers(0, 9)),
    st.builds(Counted, n=st.integers(0, 9)),
)
_values = st.recursive(
    _leaves,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.lists(children, max_size=4).map(tuple),
        st.dictionaries(st.text(max_size=3), children, max_size=4),
        st.builds(Renamed, inner_value=children),
        st.builds(FrozenBox, item=children),
    ),
    max_leaves=24,
)


@given(value=_values)
def test_the_scan_finds_a_serializer_exactly_when_the_value_holds_one(value):
    assert find_serializer(value, SCAN_KINDS, classify) == _holds_serializer(value)


@given(value=_values)
def test_the_encoder_gives_the_dump_of_each_serializer_and_keeps_the_value(value):
    before = _json._ENCODER.encode(value)
    encoded = _json.encode(value)
    assert SECRET.encode() not in encoded
    assert json.loads(encoded) == json.loads(_json._ENCODER.encode(_reference(value)))
    # The dump builds new containers. It does not change the value of the handler.
    assert _json._ENCODER.encode(value) == before
