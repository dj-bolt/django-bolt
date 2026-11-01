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
import logging
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import attrs
import msgspec
import pytest
from django.contrib.auth.models import User
from hypothesis import given
from hypothesis import strategies as st

import django_bolt
from django_bolt import BoltAPI, EventSourceResponse, ServerSentEvent, StreamingResponse, WebSocket, _json
from django_bolt._core import find_serializer
from django_bolt._serializer_dump import SCAN_KINDS, SCAN_OFFSETS, classify
from django_bolt.exceptions import HTTPException, SerializationError
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


class CamelEnvelope(Serializer, rename="camel"):
    """A Serializer with no write-only field of its own, so its dump() uses msgspec directly.

    Nothing checks the type of ``data``, so it can hold a Serializer too.
    """

    request_id: str
    data: int


class TaggedPublic(Serializer, tag=True):
    id: int


class OmitDefaults(Serializer, omit_defaults=True):
    """msgspec leaves out a default value, and dump() keeps it."""

    id: int
    note: str = "n"


class ArrayLike(Serializer, array_like=True):
    """msgspec encodes a JSON array, and dump() gives a dict."""

    id: int


class OwnDump(Serializer):
    """A dump() of its own gives the JSON, and its result can hold a Serializer."""

    id: int

    def dump(self, **kwargs: Any) -> Any:
        return [self.id, account()]


class Related(Serializer):
    """dump() checks that a field of this type holds no QuerySet or manager."""

    rows: Any


class _IdentityEq(type):
    """A metaclass with __eq__ and no __hash__, so its classes cannot be dict keys."""

    def __eq__(cls, other):
        return cls is other

    __hash__ = None


@dataclasses.dataclass
class Unhashable(metaclass=_IdentityEq):
    item: Any


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

    @api.get("/untyped/serializer-with-rename")
    def untyped_serializer_with_rename():
        return {"envelope": CamelEnvelope(request_id="r", data=account())}

    @api.get("/untyped/unhashable-type")
    def untyped_unhashable_type():
        return {"plain": Unhashable(item=1), "account": account()}

    @api.get("/untyped/unhashable-type-holds-serializer")
    def untyped_unhashable_type_holds_serializer():
        return {"held": Unhashable(item=account())}

    # A str or None annotation gets no response validator.
    @api.get("/novalidator/str")
    def novalidator_str() -> str:
        return {"user": account()}

    @api.get("/novalidator/none")
    def novalidator_none() -> None:
        return {"user": account()}

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


def test_a_serializer_in_a_serializer_with_rename_gives_its_dump(client):
    """The outer Serializer encodes as its dump(), so the scan reads its fields as a Struct."""
    _check(client.get("/untyped/serializer-with-rename"), {"envelope": {"requestId": "r", "data": DUMPED}})


def test_a_type_that_cannot_be_a_dict_key_still_encodes(client):
    _check(client.get("/untyped/unhashable-type"), {"plain": {"item": 1}, "account": DUMPED})


def test_a_type_that_cannot_be_a_dict_key_and_holds_a_serializer_fails_closed(client):
    """copy.copy hashes the class, so the dump cannot copy the object: a 500, with no secret."""
    response = client.get("/untyped/unhashable-type-holds-serializer")
    assert response.status_code == 500
    assert SECRET not in response.text


class _EqualByName(type):
    """A metaclass whose classes are equal, with the same hash, when they have the same name."""

    def __eq__(cls, other):
        return isinstance(other, _EqualByName) and cls.__name__ == other.__name__

    def __hash__(cls):
        return hash(cls.__name__)


def _named_box(holds_a_field: bool) -> type:
    if holds_a_field:

        @dataclasses.dataclass
        class NamedBox(metaclass=_EqualByName):
            inner: Any

    else:

        @dataclasses.dataclass
        class NamedBox(metaclass=_EqualByName):
            pass

    return NamedBox


def test_a_type_equal_to_a_type_with_no_field_still_gives_the_dump_of_its_serializer():
    """The kind of a type must come from the type itself, not from a type that is only equal to it."""
    empty, holder = _named_box(False), _named_box(True)
    assert empty == holder and hash(empty) == hash(holder)
    assert _json.encode({"empty": empty()}) == b'{"empty":{}}'
    assert json.loads(_json.encode({"box": holder(inner=account())})) == {"box": {"inner": DUMPED}}


@pytest.mark.parametrize("path", ["/novalidator/str", "/novalidator/none"])
def test_a_route_with_no_response_validator_dumps_each_serializer(client, path):
    _check(client.get(path), {"user": DUMPED})


@pytest.mark.parametrize(
    "value",
    [
        Public(id=1),
        CamelEnvelope(request_id="r", data=[1, "x"]),
        TaggedPublic(id=1),
        OmitDefaults(id=1),
        ArrayLike(id=1),
    ],
    ids=lambda value: type(value).__name__,
)
def test_a_serializer_with_no_serializer_inside_encodes_as_its_dump(value):
    assert _json.encode(value) == _json._ENCODER.encode(value.dump())


def test_a_serializer_with_its_own_dump_gives_that_dump_with_each_serializer_dumped():
    assert json.loads(_json.encode({"value": OwnDump(id=1)})) == {"value": [1, DUMPED]}


def test_a_serializer_that_holds_a_queryset_fails_as_its_dump_fails():
    with pytest.raises(SerializationError, match="from_model"):
        _json.encode({"value": Related(rows=User.objects.none())})


_PACKAGE_ROOT = str(Path(django_bolt.__file__).resolve().parents[1])


def _run_in_subprocess(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a new interpreter, so that a crash or a large allocation stays out of this process."""
    prelude = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {_PACKAGE_ROOT!r})
        import django
        from django.conf import settings
        settings.configure(INSTALLED_APPS=["django_bolt"], SECRET_KEY="x" * 40)
        django.setup()
        """
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code)], capture_output=True, text=True, timeout=300
    )


def test_a_type_equal_to_another_type_does_not_take_its_field_offsets():
    """Two distinct types can compare equal. The scan must not read one type with the offsets of the other."""
    result = _run_in_subprocess(
        """
        from typing import Any
        import msgspec
        from django_bolt import _json
        from django_bolt.serializers import Serializer, field

        class Account(Serializer):
            id: int
            password: str = field(write_only=True)

        class EqualByName(msgspec.StructMeta):
            def __eq__(cls, other):
                return isinstance(other, type) and cls.__name__ == other.__name__

            def __hash__(cls):
                return hash(cls.__name__)

        def make(count):
            namespace = {"__annotations__": {f"f{i}": Any for i in range(count)}}
            namespace.update({f"f{i}": None for i in range(count)})
            return EqualByName("Row", (msgspec.Struct,), namespace)

        small, big = make(1), make(64)
        _json.encode([big()])
        rows = [small() for _ in range(50000)]
        rows.append(small(f0=Account(id=1, password="s3cret-value")))
        body = _json.encode(rows)
        print("leak" if b"s3cret-value" in body else "clean", body.endswith(b'{"f0":{"id":1}}]'))
        """
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["clean", "True"]


def test_a_deep_value_next_to_a_serializer_encodes():
    """The dump goes only into the items that hold a Serializer, so a deep plain item needs no Python recursion."""
    deep: list[Any] = []
    for _ in range(2000):
        deep = [deep]
    encoded = json.loads(_json.encode({"account": account(), "data": deep}))
    assert encoded["account"] == DUMPED
    assert SECRET not in json.dumps(encoded["account"])


def test_a_cyclic_value_fails_without_a_large_scan():
    """msgspec fails at once on a cycle. The scan must not first fill memory in proportion to the fan-out."""
    result = _run_in_subprocess(
        """
        import resource
        from django_bolt import _json
        from django_bolt.serializers import Serializer

        class Account(Serializer):
            id: int

        parent = {"children": []}
        parent["children"].extend({"id": index, "parent": parent} for index in range(3000))
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            _json.encode(parent)
        except RecursionError:
            print("RecursionError")
        print((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) // 1024)
        """
    )
    assert result.returncode == 0, result.stderr[-2000:]
    error, growth_mb = result.stdout.split()
    assert error == "RecursionError"
    assert int(growth_mb) < 64


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


class ReviewedPost(Serializer):
    """The dump of a view keeps the Serializers in ``reviewers``: it is not a nested field."""

    id: int
    internal_note: str = field(write_only=True)
    reviewers: dict[str, Account]
    extra: Any = None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/view-json", {"id": 1, "reviewers": {"lead": DUMPED}}),
        ("/view-many-json", [{"id": 1, "reviewers": {"lead": DUMPED}}]),
    ],
)
def test_the_json_of_a_serializer_view_gives_the_dump_of_each_serializer_in_it(path, expected):
    api = BoltAPI()
    card = ReviewedPost.only("id", "reviewers")

    def reviewed_post() -> ReviewedPost:
        return ReviewedPost(id=1, internal_note=SECRET, reviewers={"lead": account()})

    @api.get("/view-json")
    def view_json():
        return Response(card.dump_json(reviewed_post()), media_type="application/json")

    @api.get("/view-many-json")
    def view_many_json():
        return Response(ReviewedPost.exclude("extra").dump_many_json([reviewed_post()]), media_type="application/json")

    with TestClient(api) as test_client:
        _check(test_client.get(path), expected)


class Login(Serializer, repr_omit_defaults=True):
    """A Serializer with each kind of field that dump() does not send."""

    id: int
    password: str = field(write_only=True)
    token: str = ""
    internal: str = field(exclude=True, default="i")
    name: str = "anon"
    links: list[Any] = []

    class Config:
        write_only = {"token"}


def test_the_repr_of_a_serializer_leaves_out_the_fields_that_dump_does_not_send():
    assert repr(account()) == "Account(id=1)"
    assert str(account()) == "Account(id=1)"
    assert repr({"user": [account()]}) == "{'user': [Account(id=1)]}"
    assert repr(Login(id=1, password=SECRET, token=SECRET, internal=SECRET)) == "Login(id=1)"
    assert repr(Login(id=2, password=SECRET, name="bob")) == "Login(id=2, name='bob')"
    assert list(Login(id=2, password=SECRET, name="bob").__rich_repr__()) == [("id", 2), ("name", "bob")]
    # A cycle gives "...", as in the repr of msgspec.
    login = Login(id=3, password=SECRET)
    login.links.append(login)
    assert repr(login) == "Login(id=3, links=[...])"
    # A Serializer that hides no field keeps the repr of msgspec.
    assert repr(Public(id=1)) == "Public(id=1)"


@pytest.fixture(scope="module")
def str_client():
    api = BoltAPI()

    @api.get("/gen")
    def gen():
        # No Iterator[...] annotation: Bolt sends str() of each chunk.
        yield {"user": account()}

    @api.get("/agen")
    async def agen():
        yield account()

    @api.get("/streaming-response")
    def streaming_response():
        return StreamingResponse(iter([account()]), media_type="text/plain")

    @api.get("/text-json")
    def text_json():
        # Bolt does not read text/json as a JSON media type, so it sends str(content).
        return Response({"user": account()}, media_type="text/json")

    @api.get("/http-exception")
    def http_exception():
        raise HTTPException(409, detail={"error": "email taken", "user": account()})

    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/gen", "{'user': Account(id=1)}"),
        ("/agen", "Account(id=1)"),
        ("/streaming-response", "Account(id=1)"),
        ("/text-json", "{'user': Account(id=1)}"),
    ],
    ids=["gen", "agen", "streaming-response", "text-json"],
)
def test_a_value_that_bolt_sends_as_its_str_never_shows_a_write_only_value(str_client, path, body):
    response = str_client.get(path)
    assert response.status_code == 200
    assert SECRET not in response.text
    assert response.text == body


def test_the_detail_of_an_http_exception_never_shows_a_write_only_value(str_client):
    """HTTPException keeps str(detail) for a detail that is not a str, as before."""
    response = str_client.get("/http-exception")
    assert response.status_code == 409
    assert SECRET not in response.text
    assert response.json() == {"detail": "{'error': 'email taken', 'user': Account(id=1)}"}


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


@dataclasses.dataclass
class TreePad:
    """The scan reads a dataclass field with an attribute lookup, so each pad costs the most."""

    width: int = 0
    height: int = 0


class TreeNode(msgspec.Struct, omit_defaults=True):
    """A body type that a client can nest: each node can hold a Serializer."""

    child: TreeNode | None = None
    pads: list[TreePad] = []
    owner: Account | None = None


def _tree_body(depth: int, pads: int) -> bytes:
    """Return a tree ``depth`` nodes deep with ``pads`` pads at each node and the Serializer at the bottom."""
    body = '{"owner":{"id":1,"password":"' + SECRET + '"}}'
    pad_list = '"pads":[' + ",".join(["{}"] * pads) + "]"
    for _ in range(depth):
        body = '{"child":' + body + "," + pad_list + "}"
    return body.encode()


@pytest.fixture(scope="module")
def tree_client():
    api = BoltAPI()

    @api.post("/tree")
    def tree(data: TreeNode):
        return data

    with TestClient(api, raise_server_exceptions=False) as test_client:
        yield test_client


def _post_tree(test_client, depth: int, pads: int):
    return test_client.post("/tree", content=_tree_body(depth, pads), headers={"content-type": "application/json"})


def test_a_serializer_deep_in_a_tree_that_the_handler_returns_gives_its_dump(tree_client):
    response = _post_tree(tree_client, 300, 10)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    node = response.json()
    for _ in range(300):
        node = node["child"]
    assert node == {"owner": DUMPED}


def test_a_serializer_deep_in_a_tree_costs_time_linear_in_the_size_of_the_tree(tree_client):
    """Each level of the dump scanned the whole subtree below it again, so a 1 MB body held the GIL for 20 s."""
    start = time.process_time()
    response = _post_tree(tree_client, 3000, 100)
    elapsed = time.process_time() - start
    # The tree is deeper than the dump can go, so the response fails closed.
    assert response.status_code == 500
    assert SECRET not in response.text
    assert elapsed < 10, f"the dump took {elapsed:.1f} s of CPU"


def test_a_serializer_too_deep_to_dump_logs_a_short_error(tree_client, caplog):
    """The error of the dump names the cause. Its traceback has no frame for each level of the value."""
    caplog.set_level(logging.ERROR)
    response = _post_tree(tree_client, 600, 0)
    assert response.status_code == 500
    logged = "\n".join(logging.Formatter().format(record) for record in caplog.records)
    assert "nested too deeply" in logged
    assert len(logged) < 20_000, f"{len(logged)} bytes of log for one request"


class HidingDict(dict):
    """A dict whose methods hide its entries. msgspec reads the storage of a dict."""

    def items(self):
        return iter(())

    def keys(self):
        return iter(())

    def __iter__(self):
        return iter(())


class HidingList(list):
    """A list whose iterator hides its items. msgspec reads the storage of a list."""

    def __iter__(self):
        return iter(())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"wrap": HidingDict(plain=1, account=account())}, {"wrap": {"plain": 1, "account": DUMPED}}),
        ({"rows": HidingList([account()])}, {"rows": [DUMPED]}),
        ({"rows": HidingList([1, account()])}, {"rows": [1, DUMPED]}),
    ],
    ids=["dict-items", "list-iter", "list-iter-after-a-leaf"],
)
def test_a_container_that_overrides_its_iteration_gives_the_dump_of_its_serializer(value, expected):
    assert json.loads(_json.encode(value)) == expected


class Event(Serializer, tag=True):
    id: int


class SignInEvent(Event):
    password: str = field(write_only=True)


class NoteEvent(Event):
    text: str


class PublicUser(Serializer):
    id: int


class PrivateUser(PublicUser):
    password: str = field(write_only=True)


@pytest.mark.parametrize(
    ("base", "instances"),
    [
        (Event, [NoteEvent(id=1, text="hi"), SignInEvent(id=2, password=SECRET)]),
        (PublicUser, [PrivateUser(id=1, password=SECRET)]),
    ],
    ids=["tagged-union", "subclass"],
)
def test_dump_many_of_subclass_instances_gives_the_dump_of_each_instance(base, instances):
    """The fast path of the base class dumped each field of a subclass, also its write-only fields."""
    dumped = base.dump_many(instances)
    assert SECRET not in json.dumps(dumped)
    assert dumped == [instance.dump() for instance in instances]


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
    """Return whether ``value`` holds a Serializer whose dump() differs from the encoding of msgspec."""
    if isinstance(value, Account):
        return True
    if isinstance(value, Public):
        # Public has no write-only or computed field, so the scan reads it as a Struct.
        return False
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
