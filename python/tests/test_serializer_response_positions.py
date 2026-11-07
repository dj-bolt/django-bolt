"""A declared response type gives the dump() of each Serializer at each of its positions.

Bolt dumped a Serializer only when the response type was the Serializer, an
optional Serializer or a list of Serializers. msgspec encodes each other
Serializer natively. Thus a Serializer in a Struct field, a dict value, a
tuple, a TypedDict or a union sent its write-only fields and lost its
computed fields.

The declared type is the boundary, as a DTO is in Litestar. A value with no
declared type goes out as msgspec encodes it.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Annotated, Generic, NotRequired, TypedDict, TypeVar

import msgspec
import pytest
import typing_extensions

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.testing import TestClient

SECRET = "s3cret-value"
T = TypeVar("T")


class Account(Serializer):
    id: int
    password: str = field(write_only=True)

    @computed_field
    def label(self) -> str:
        return f"user-{self.id}"


class Admin(Account):
    token: str = field(write_only=True)


class Envelope(msgspec.Struct):
    account: Account
    note: str = "n"


class Wrapped(msgspec.Struct, rename="camel"):
    main_account: Account
    others: list[Envelope] = []


class Page(msgspec.Struct, Generic[T]):  # noqa: UP046 - PEP 695 params do not resolve under PEP 563
    items: list[T]
    total: int


class Node(msgspec.Struct):
    account: Account | None = None
    children: list[Node] = []


class Plain(msgspec.Struct):
    """A Struct with no Serializer position."""

    id: int


@dataclasses.dataclass
class Box:
    account: Account


class Holder(TypedDict):
    account: Account
    extra: NotRequired[Annotated[Account, "doc"]]


class ExtensionsHolder(typing_extensions.TypedDict):
    account: Account


type Accounts[X] = list[X]


@dataclasses.dataclass
class GenericBox(Generic[T]):  # noqa: UP046 - PEP 695 params do not resolve under PEP 563
    item: T


class Pair(msgspec.Struct, array_like=True):
    n: int
    account: Account


class FrozenAccount(Serializer, frozen=True):
    id: int
    password: str = field(write_only=True)


DEFAULT_ACCOUNT = FrozenAccount(id=0, password=SECRET)


class WithDefault(msgspec.Struct, omit_defaults=True):
    account: FrozenAccount = DEFAULT_ACCOUNT


def account(i: int = 1) -> Account:
    return Account(id=i, password=SECRET)


# The routes below register before the module defines LateAccount, as in an
# app module that defines a nested type after its routes.
class EarlyEnvelope(msgspec.Struct):
    account: LateAccount


class NeverEnvelope(msgspec.Struct):
    account: NeverDefined  # noqa: F821 - the name never resolves


late_api = BoltAPI()


@late_api.get("/late")
def late_route() -> EarlyEnvelope:
    return EarlyEnvelope(account=LateAccount(id=1, password=SECRET))


@late_api.get("/never", validate_response=False)
def never_route() -> NeverEnvelope:
    return NeverEnvelope(account=account())


class LateAccount(Account):
    pass


DUMPED = {"id": 1, "label": "user-1"}


def get(api: BoltAPI, path: str):
    with TestClient(api) as client:
        response = client.get(path)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text, response.text
    return response.json()


@pytest.mark.parametrize("validate", [True, False])
def test_struct_field(validate):
    api = BoltAPI()

    @api.get("/sync", validate_response=validate)
    def sync_route() -> Envelope:
        return Envelope(account=account())

    @api.get("/async", validate_response=validate)
    async def async_route() -> Envelope:
        return Envelope(account=account())

    assert get(api, "/sync") == {"account": DUMPED, "note": "n"}
    assert get(api, "/async") == {"account": DUMPED, "note": "n"}


def test_list_of_structs_and_rename():
    api = BoltAPI()

    @api.get("/list")
    def list_route() -> list[Envelope]:
        return [Envelope(account=account()), Envelope(account=account(2))]

    @api.get("/renamed")
    def renamed() -> Wrapped:
        return Wrapped(main_account=account(), others=[Envelope(account=account(2))])

    assert get(api, "/list") == [
        {"account": DUMPED, "note": "n"},
        {"account": {"id": 2, "label": "user-2"}, "note": "n"},
    ]
    assert get(api, "/renamed") == {
        "mainAccount": DUMPED,
        "others": [{"account": {"id": 2, "label": "user-2"}, "note": "n"}],
    }


@pytest.mark.parametrize("validate", [True, False])
def test_containers(validate):
    api = BoltAPI()

    @api.get("/dict", validate_response=validate)
    def dict_route() -> dict[str, Account]:
        return {"a": account()}

    @api.get("/tuple", validate_response=validate)
    def tuple_route() -> tuple[Account, ...]:
        return (account(), account())

    @api.get("/fixed-tuple", validate_response=validate)
    def fixed_tuple() -> tuple[int, Account]:
        return (7, account())

    @api.get("/nested", validate_response=validate)
    def nested() -> dict[str, list[Account]]:
        return {"a": [account()]}

    assert get(api, "/dict") == {"a": DUMPED}
    assert get(api, "/tuple") == [DUMPED, DUMPED]
    assert get(api, "/fixed-tuple") == [7, DUMPED]
    assert get(api, "/nested") == {"a": [DUMPED]}


def test_typed_dict_dataclass_and_union():
    api = BoltAPI()

    @api.get("/typed-dict")
    def typed_dict() -> Holder:
        return {"account": account(), "extra": account()}

    @api.get("/dataclass-field")
    def dataclass_field() -> dict[str, Box]:
        return {"box": Box(account=account())}

    @api.get("/union")
    def union() -> Envelope | Plain:
        return Envelope(account=account())

    @api.get("/optional")
    def optional() -> Envelope | None:
        return Envelope(account=account())

    assert get(api, "/typed-dict") == {"account": DUMPED, "extra": DUMPED}
    assert get(api, "/dataclass-field") == {"box": {"account": DUMPED}}
    assert get(api, "/union") == {"account": DUMPED, "note": "n"}
    assert get(api, "/optional") == {"account": DUMPED, "note": "n"}


def test_generic_and_recursive_structs():
    api = BoltAPI()

    @api.get("/page")
    def page() -> Page[Account]:
        return Page(items=[account()], total=1)

    @api.get("/tree")
    def tree() -> Node:
        return Node(account=account(), children=[Node(children=[Node(account=account(2))])])

    assert get(api, "/page") == {"items": [DUMPED], "total": 1}
    assert get(api, "/tree") == {
        "account": DUMPED,
        "children": [{"account": None, "children": [{"account": {"id": 2, "label": "user-2"}, "children": []}]}],
    }


def test_subclass_instance_gives_its_own_dump():
    api = BoltAPI()

    @api.get("/admin")
    def admin() -> Envelope:
        return Envelope(account=Admin(id=1, password=SECRET, token=SECRET))

    assert get(api, "/admin") == {"account": DUMPED, "note": "n"}


def test_status_entry_and_status_tuple():
    api = BoltAPI()

    @api.get("/entry", response_model={200: Envelope, 404: dict})
    def entry():
        return Envelope(account=account())

    @api.get("/tuple-result", response_model={201: dict[str, Account], 404: dict})
    def tuple_result():
        return 201, {"a": account()}

    assert get(api, "/entry") == {"account": DUMPED, "note": "n"}
    with TestClient(api) as client:
        response = client.get("/tuple-result")
    assert response.status_code == 201, response.text
    assert response.json() == {"a": DUMPED}


@pytest.mark.parametrize("validate", [True, False])
def test_typed_stream_items(validate):
    api = BoltAPI()

    @api.get("/stream", validate_response=validate)
    def stream() -> Iterator[Envelope]:
        yield Envelope(account=account())
        yield Envelope(account=account(2))

    with TestClient(api) as client:
        response = client.get("/stream")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text, response.text
    assert [json.loads(line) for line in response.content.splitlines()] == [
        {"account": DUMPED, "note": "n"},
        {"account": {"id": 2, "label": "user-2"}, "note": "n"},
    ]


def test_field_type_defined_after_the_route():
    assert get(late_api, "/late") == {"account": DUMPED}


def test_field_type_that_never_resolves_fails_closed():
    with TestClient(late_api) as client:
        response = client.get("/never")
    assert response.status_code == 500, response.text
    assert SECRET not in response.text, response.text


def test_type_with_no_serializer_keeps_its_output():
    api = BoltAPI()

    @api.get("/plain")
    def plain() -> list[Plain]:
        return [Plain(id=1)]

    @api.get("/mapping")
    def mapping() -> dict[str, int]:
        return {"a": 1}

    assert get(api, "/plain") == [{"id": 1}]
    assert get(api, "/mapping") == {"a": 1}


def test_generic_alias_dataclass_and_extensions_typed_dict():
    api = BoltAPI()

    @api.get("/alias", validate_response=False)
    def alias() -> dict[str, Accounts[Account]]:
        return {"a": [account()]}

    @api.get("/generic-dataclass", validate_response=False)
    def generic_dataclass() -> dict[str, GenericBox[Account]]:
        return {"box": GenericBox(item=account())}

    @api.get("/extensions-typed-dict", validate_response=False)
    def extensions_typed_dict() -> ExtensionsHolder:
        return {"account": account()}

    assert get(api, "/alias") == {"a": [DUMPED]}
    assert get(api, "/generic-dataclass") == {"box": {"item": DUMPED}}
    assert get(api, "/extensions-typed-dict") == {"account": DUMPED}


def test_union_of_alternatives_that_each_hold_a_serializer():
    api = BoltAPI()

    @api.get("/first", validate_response=False)
    def first() -> tuple[Account, int] | tuple[int, Account]:
        return (account(), 7)

    @api.get("/second", validate_response=False)
    def second() -> tuple[Account, int] | tuple[int, Account]:
        return (7, account())

    @api.get("/both", validate_response=False)
    def both() -> tuple[Account, int] | tuple[Account, Account]:
        return (account(), account(2))

    assert get(api, "/first") == [DUMPED, 7]
    assert get(api, "/second") == [7, DUMPED]
    assert get(api, "/both") == [DUMPED, {"id": 2, "label": "user-2"}]


def test_array_like_struct_as_a_list():
    api = BoltAPI()

    @api.get("/pair", validate_response=False)
    def pair() -> Pair:
        return [7, account()]  # type: ignore[return-value]

    assert get(api, "/pair") == [7, DUMPED]


def test_omit_defaults_still_omits_the_default_serializer():
    api = BoltAPI()

    @api.get("/default")
    def default() -> WithDefault:
        return WithDefault()

    @api.get("/other")
    def other() -> WithDefault:
        return WithDefault(account=FrozenAccount(id=3, password=SECRET))

    assert get(api, "/default") == {}
    assert get(api, "/other") == {"account": {"id": 3}}
