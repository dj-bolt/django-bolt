"""A Serializer in a response gives its dump(), wherever it is in the value.

msgspec encodes each Struct natively, so a Serializer that the response
holds in a container got all of its fields: also its ``write_only`` fields,
and none of its computed fields. Each route here returns such a value on a
different path of the dispatch.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, NewType, TypeVar

import attrs
import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

from django_bolt import BoltAPI, EventSourceResponse, ServerSentEvent, WebSocket, _json
from django_bolt._core import find_serializer
from django_bolt._serializer_dump import SCAN_KINDS, SCAN_OFFSETS, annotation_excludes_serializer, classify
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


@dataclasses.dataclass
class IntBox:
    n: int


class Renamed(msgspec.Struct, rename="camel", frozen=True):
    """A dumped field must keep the encoding options of the Struct."""

    inner_value: Any


@dataclasses.dataclass(frozen=True, slots=True)
class FrozenBox:
    item: Any


class Page[T](msgspec.Struct):
    """A generic envelope: a type variable can hold a Serializer."""

    items: list[T]
    owner: Any = None


class Wrapper(Serializer):
    """A Serializer whose dump() keeps the values of a plain dict field."""

    payload: dict


class UserOut(msgspec.Struct):
    """A plain Struct with the public fields of Account."""

    id: int


class UserBox(msgspec.Struct):
    """Nothing checks the type of a field, so ``user`` can hold an Account."""

    user: UserOut


class Counted(msgspec.Struct):
    """Nothing checks the type of ``n``, so ``n`` can hold a Serializer too."""

    n: int


@dataclasses.dataclass(slots=True)
class Swapped:
    """A slotted dataclass whose ``getattr`` gives another object than its slot.

    msgspec reads the fields of such a dataclass with ``getattr``.
    """

    inner: Any

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name == "inner" and value == "swap":
            return account()
        return value


class Masked(msgspec.Struct):
    """msgspec reads a Struct field from its slot, also when ``getattr`` gives another object."""

    inner: Any

    def __getattribute__(self, name):
        if name == "inner":
            return "masked"
        return msgspec.Struct.__getattribute__(self, name)


class SelfCopy(msgspec.Struct):
    """A Struct whose ``__copy__`` gives the object itself."""

    inner: Any

    def __copy__(self):
        return self


@dataclasses.dataclass(slots=True)
class Stubborn:
    """A dataclass whose ``getattr`` gives a new Serializer whatever its slot holds."""

    inner: Any

    def __getattribute__(self, name):
        if name == "inner":
            return account()
        return object.__getattribute__(self, name)


@dataclasses.dataclass(slots=True)
class Late:
    """A dataclass that gets its own attribute lookup after its first scan."""

    inner: Any


def _late_getattribute(self, name):
    value = object.__getattribute__(self, name)
    if name == "inner" and value == "swap":
        return account()
    return value


@dataclasses.dataclass
class SelfCopyBox:
    """A dataclass whose ``__copy__`` gives the object itself."""

    inner: Any

    def __copy__(self):
        return self


class Role(enum.Enum):
    """msgspec encodes an Enum member as its value, so a member value can be a Serializer."""

    ADMIN = Account(id=1, password=SECRET)


@attrs.define
class AttrsBox:
    inner: Any


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

    # The annotation of these routes is not checked against the value.
    @api.get("/trust/dict-of-int", validate_response=False)
    def trust_dict_of_int() -> dict[str, int]:
        return {"account": account()}

    @api.get("/trust/list-of-structs", validate_response=False)
    def trust_list_of_structs() -> list[UserOut]:
        return [account()]

    @api.get("/trust/struct-field")
    def trust_struct_field() -> UserBox:
        return UserBox(user=account())

    @api.get("/trust/struct-field-no-validation", validate_response=False)
    def trust_struct_field_no_validation() -> UserBox:
        return UserBox(user=account())

    @api.get("/trust/struct-field-untyped")
    def trust_struct_field_untyped():
        return UserBox(user=account())

    @api.get("/trust/generic-of-int")
    def trust_generic_of_int() -> Page[int]:
        return Page(items=[1], owner=account())

    @api.get("/trust/int-field")
    def trust_int_field():
        return [Counted(n=1), Counted(n=account())]

    @api.get("/trust/int-field-validated")
    def trust_int_field_validated() -> list[Counted]:
        return [Counted(n=account())]

    @api.get("/trust/int-field-dataclass")
    def trust_int_field_dataclass():
        return {"box": IntBox(n=account())}

    @api.get("/trust/leaf-structs-then-serializer")
    def trust_leaf_structs_then_serializer():
        return [Counted(n=1), Counted(n=2), account()]

    @api.get("/untyped/enum-value")
    def untyped_enum_value():
        return {"role": Role.ADMIN}

    @api.get("/untyped/attrs")
    def untyped_attrs():
        return [AttrsBox(inner=1), AttrsBox(inner=account())]

    # Types with their own attribute lookup.
    @api.get("/read/swapped-dataclass")
    def read_swapped_dataclass():
        # The scan visits the last item first, so a plain instance comes last.
        return [Swapped(inner="swap"), Swapped(inner=1)]

    @api.get("/read/masked-struct")
    def read_masked_struct():
        return [Masked(inner=1), Masked(inner=account())]

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


def test_a_serializer_in_a_route_that_does_not_validate_its_type_gives_its_dump(client):
    _check(client.get("/trust/dict-of-int"), {"account": DUMPED})
    _check(client.get("/trust/list-of-structs"), [DUMPED])


@pytest.mark.parametrize(
    "path", ["/trust/struct-field", "/trust/struct-field-no-validation", "/trust/struct-field-untyped"]
)
def test_a_serializer_in_a_struct_field_of_another_type_gives_its_dump(client, path):
    _check(client.get(path), {"user": DUMPED})


def test_a_serializer_in_a_generic_struct_of_a_leaf_type_gives_its_dump(client):
    _check(client.get("/trust/generic-of-int"), {"items": [1], "owner": DUMPED})


def test_a_serializer_in_an_int_field_gives_its_dump(client):
    _check(client.get("/trust/int-field"), [{"n": 1}, {"n": DUMPED}])
    _check(client.get("/trust/int-field-validated"), [{"n": DUMPED}])
    _check(client.get("/trust/int-field-dataclass"), {"box": {"n": DUMPED}})


def test_a_serializer_after_structs_with_only_leaf_fields_gives_its_dump(client):
    _check(client.get("/trust/leaf-structs-then-serializer"), [{"n": 1}, {"n": 2}, DUMPED])


def test_a_serializer_as_the_value_of_an_enum_member_gives_its_dump(client):
    _check(client.get("/untyped/enum-value"), {"role": DUMPED})
    assert json.loads(_json.encode({"role": Role.ADMIN}, serializer=str)) == {"role": DUMPED}


def test_a_serializer_in_an_attrs_field_gives_its_dump(client):
    _check(client.get("/untyped/attrs"), [{"inner": 1}, {"inner": DUMPED}])


def test_a_dataclass_field_is_read_with_getattr_as_msgspec_reads_it(client):
    _check(client.get("/read/swapped-dataclass"), [{"inner": DUMPED}, {"inner": 1}])


def test_a_struct_field_is_read_from_its_slot_as_msgspec_reads_it(client):
    _check(client.get("/read/masked-struct"), [{"inner": 1}, {"inner": DUMPED}])


def test_a_dataclass_whose_getattr_ignores_the_dumped_value_fails_closed():
    """msgspec reads the field with getattr, so the clone must give back the dumped value."""
    api = BoltAPI()

    @api.get("/stubborn")
    def stubborn_route():
        return [Stubborn(inner=1)]

    with TestClient(api) as test_client:
        response = test_client.get("/stubborn")
        assert response.status_code == 500
        assert SECRET not in response.text


def test_a_dataclass_that_gets_its_own_lookup_after_a_scan_is_read_with_getattr():
    """The cached slot offsets of a class apply only while the class keeps the generic lookup."""
    api = BoltAPI()

    @api.get("/late")
    def late_route():
        return [Late(inner="swap")]

    @api.get("/late-one")
    def late_one_route():
        return JSON(Late(inner="swap"))

    with TestClient(api) as test_client:
        _check(test_client.get("/late"), [{"inner": "swap"}])
        Late.__getattribute__ = _late_getattribute
        try:
            # In a list, the scan reads the cached offsets of the child. Alone, it reads those of the value.
            _check(test_client.get("/late"), [{"inner": DUMPED}])
            _check(test_client.get("/late-one"), {"inner": DUMPED})
        finally:
            del Late.__getattribute__


def test_a_copy_that_gives_the_object_itself_does_not_change_the_value_of_the_handler():
    """A Struct gets the copy of msgspec. A dataclass gives a 500, and its Serializer stays."""
    api = BoltAPI()
    struct_value = SelfCopy(inner=account())
    dataclass_value = SelfCopyBox(inner=account())

    @api.get("/struct")
    def struct_route():
        return [struct_value]

    @api.get("/dataclass")
    def dataclass_route():
        return [dataclass_value]

    with TestClient(api) as test_client:
        _check(test_client.get("/struct"), [{"inner": DUMPED}])
        response = test_client.get("/dataclass")
        assert response.status_code == 500
        assert SECRET not in response.text
    assert isinstance(struct_value.inner, Account)
    assert isinstance(dataclass_value.inner, Account)


def test_a_serializer_in_json_gives_its_dump(client):
    _check(client.get("/json/serializer"), DUMPED)


def test_a_serializer_in_a_generic_struct_gives_its_dump(client):
    _check(client.get("/untyped/generic-page"), {"items": [DUMPED], "owner": None})


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


class Owner:
    """A type that msgspec cannot encode. msgspec calls the encoder hook for it."""

    def __init__(self, pk: int) -> None:
        self.pk = pk


# msgspec encodes the return value of a hook natively, after the scan.
_HOOK_RESULTS = {
    "serializer": (lambda owner: Account(id=owner.pk, password=SECRET), DUMPED),
    "list": (lambda owner: [Account(id=owner.pk, password=SECRET)], [DUMPED]),
    "struct": (lambda owner: Envelope(account=Account(id=owner.pk, password=SECRET)), {"account": DUMPED, "note": "n"}),
}


@pytest.mark.parametrize("result", _HOOK_RESULTS)
def test_a_serializer_that_a_default_type_encoder_returns_gives_its_dump(monkeypatch, result):
    hook, expected = _HOOK_RESULTS[result]
    monkeypatch.setitem(_json.DEFAULT_TYPE_ENCODERS, Owner, hook)
    api = BoltAPI()

    @api.get("/owner")
    def owner_route():
        return {"owner": Owner(1)}

    with TestClient(api) as test_client:
        _check(test_client.get("/owner"), {"owner": expected})


@pytest.mark.parametrize("result", _HOOK_RESULTS)
def test_a_serializer_that_a_custom_hook_returns_gives_its_dump(result):
    hook, expected = _HOOK_RESULTS[result]
    encoded = _json.encode({"owner": Owner(1)}, serializer=hook)
    assert SECRET.encode() not in encoded
    assert json.loads(encoded) == {"owner": expected}


# A Serializer with rename= or tag= dumps with msgspec.to_builtins when it has
# no write-only, computed or nested field of its own. A field of these
# Serializers is not a nested field, but it can hold a Serializer.


class CamelEnvelope(Serializer, rename="camel"):
    request_id: str
    data: Any


class TaggedEnvelope(Serializer, tag=True):
    data: Any


class CamelTeam(Serializer, rename="camel"):
    team_name: str
    members_by_role: dict[str, Account]
    pair: tuple[Account, ...] = ()


class OmittingEnvelope(Serializer, rename="camel", omit_defaults=True):
    inner_data: Any = None
    note_text: str = "n"


class CamelPost(Serializer, rename="camel"):
    """``author`` names a class below, so the type hints fail at class creation."""

    post_id: int
    author: LaterAccount


class LaterAccount(Serializer):
    id: int
    password: str = field(write_only=True)


class CamelComment(Serializer, rename="camel"):
    """The self reference makes the type hints fail at class creation."""

    comment_id: int
    author: Account
    replies: list[CamelComment] = []


_DUMP_CASES = {
    "any": (lambda: CamelEnvelope(request_id="r", data=account()), {"requestId": "r", "data": DUMPED}),
    "tag": (lambda: TaggedEnvelope(data=[account()]), {"type": "TaggedEnvelope", "data": [DUMPED]}),
    "dict-and-tuple": (
        lambda: CamelTeam(team_name="t", members_by_role={"owner": account()}, pair=(account(),)),
        {"teamName": "t", "membersByRole": {"owner": DUMPED}, "pair": [DUMPED]},
    ),
    "omit-defaults": (lambda: OmittingEnvelope(inner_data=account()), {"innerData": DUMPED}),
    "forward-ref": (
        lambda: CamelPost(post_id=1, author=LaterAccount(id=1, password=SECRET)),
        {"postId": 1, "author": {"id": 1}},
    ),
    "self-ref": (
        lambda: CamelComment(comment_id=1, author=account(), replies=[CamelComment(comment_id=2, author=account())]),
        {"commentId": 1, "author": DUMPED, "replies": [{"commentId": 2, "author": DUMPED, "replies": []}]},
    ),
}


@pytest.fixture(scope="module")
def dump_client():
    api = BoltAPI()

    @api.get("/dump/{name}")
    def dump_route(name: str):
        return _DUMP_CASES[name][0]()

    @api.get("/dump-typed/envelope")
    async def dump_typed_envelope() -> CamelEnvelope:
        return CamelEnvelope(request_id="r", data=account())

    @api.get("/dump-typed/teams")
    def dump_typed_teams() -> list[CamelTeam]:
        return [CamelTeam(team_name="t", members_by_role={"owner": account()})]

    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize("case", _DUMP_CASES)
def test_a_serializer_in_a_field_of_a_renamed_or_tagged_serializer_gives_its_dump(dump_client, case):
    _check(dump_client.get(f"/dump/{case}"), _DUMP_CASES[case][1])


def test_a_typed_route_dumps_the_serializer_in_a_field_of_its_serializer(dump_client):
    _check(dump_client.get("/dump-typed/envelope"), {"requestId": "r", "data": DUMPED})
    _check(dump_client.get("/dump-typed/teams"), [{"teamName": "t", "membersByRole": {"owner": DUMPED}, "pair": []}])


@pytest.mark.parametrize("case", _DUMP_CASES)
def test_dump_gives_the_dump_of_each_serializer_in_its_fields(case):
    factory, expected = _DUMP_CASES[case]
    value = factory()
    assert value.dump() == expected
    assert type(value).dump_many([value, factory()]) == [expected, expected]
    # The dump does not change the value: it still holds the Serializer.
    assert SECRET.encode() in _json._ENCODER.encode(value)


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


class Color(enum.Enum):
    RED = "red"


@pytest.mark.parametrize(
    "annotation",
    [int, str, type(None), int | None, list[int], dict[str, int], tuple[int, ...], Literal["a"]]
    + [Annotated[int, "doc"], Color, datetime, uuid.UUID, Decimal],
    ids=repr,
)
def test_a_leaf_annotation_cannot_hold_a_serializer(annotation):
    assert annotation_excludes_serializer(annotation)


@pytest.mark.parametrize(
    "annotation",
    [Any, dict, list, object, UserOut, Account, list[UserOut], dict[str, Any], int | UserOut, Page[int], Role]
    + [TypeVar("T"), NewType("UserId", int)],
    ids=repr,
)
def test_a_class_or_an_open_annotation_can_hold_a_serializer(annotation):
    """Nothing checks the fields of a Struct, so a Struct type is not a leaf, also with leaf arguments."""
    assert not annotation_excludes_serializer(annotation)


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
        return {"n": _reference(value.n)}
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
    if isinstance(value, Counted):
        return _holds_serializer(value.n)
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
        st.lists(children, max_size=12),
        st.lists(children, max_size=4).map(tuple),
        st.dictionaries(st.text(max_size=3), children, max_size=12),
        st.builds(Renamed, inner_value=children),
        st.builds(FrozenBox, item=children),
        st.builds(Counted, n=children),
    ),
    max_leaves=24,
)


@given(value=_values)
def test_the_scan_finds_a_serializer_exactly_when_the_value_holds_one(value):
    expected = _holds_serializer(value)
    assert find_serializer(value, SCAN_KINDS, classify) == expected
    # The encoder reads the fields of a Struct at their slot offsets.
    assert find_serializer(value, SCAN_KINDS, classify, SCAN_OFFSETS) == expected


@given(value=_values)
def test_the_encoder_gives_the_dump_of_each_serializer_and_keeps_the_value(value):
    before = _json._ENCODER.encode(value)
    encoded = _json.encode(value)
    assert SECRET.encode() not in encoded
    assert json.loads(encoded) == json.loads(_json._ENCODER.encode(_reference(value)))
    # The dump builds new containers. It does not change the value of the handler.
    assert _json._ENCODER.encode(value) == before
