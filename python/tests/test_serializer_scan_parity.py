"""The scan and the dump read each object as msgspec 0.22 encodes it.

msgspec encodes an attrs object as a plain object: each public key of its
``__dict__`` and each public slot of the classes in its base chain. It
encodes a dataclass from each key of ``__dataclass_fields__``. It reads the
``__dict__`` first, then a data descriptor, and it skips a field whose getter
raises. A Serializer that msgspec encodes must give its dump(). A field that
msgspec skips must not change the response.
"""

from __future__ import annotations

import dataclasses
import functools
import gc
import json
import os
import subprocess
import sys
import textwrap
import weakref
from dataclasses import InitVar
from pathlib import Path
from typing import Any, ClassVar

import attr
import attrs
import msgspec
import pytest

import django_bolt
from django_bolt import BoltAPI, _json
from django_bolt._serializer_dump import SCAN_KINDS, SCAN_OFFSETS
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.testing import TestClient

SECRET = "s3cret-value"


class Account(Serializer):
    id: int
    password: str = field(write_only=True)

    @computed_field
    def label(self) -> str:
        return f"user-{self.id}"


DUMPED = {"id": 1, "label": "user-1"}


def account() -> Account:
    return Account(id=1, password=SECRET)


# attrs: msgspec encodes the public keys of __dict__ and the public slots, not __attrs_attrs__.


@attrs.define
class Card:
    """attrs makes the cached property a slot."""

    title: str

    @functools.cached_property
    def owner(self) -> Account:
        return account()


@attrs.define(slots=False)
class Doc:
    title: str


@attr.s(auto_attribs=True)
class Legacy:
    user_id: int

    def __attrs_post_init__(self) -> None:
        self.owner = account()


class PlainSlotBase:
    __slots__ = ("extra",)


@attrs.define
class WithBaseSlot(PlainSlotBase):
    user_id: int


@attrs.define
class Masking:
    """msgspec reads the slot itself, not the attribute lookup of the class."""

    owner: Any

    def __getattribute__(self, name: str) -> Any:
        if name == "owner":
            return "masked"
        return object.__getattribute__(self, name)


# Dataclasses: msgspec reads each key of __dataclass_fields__, with the dict first.


@dataclasses.dataclass
class WithInitVar:
    user_id: int
    owner: InitVar[Any] = None

    def __post_init__(self, owner: Any) -> None:
        self.owner = owner


@dataclasses.dataclass
class WithClassVar:
    user_id: int
    current: ClassVar[Any] = None


@dataclasses.dataclass
class SharedClassVar:
    """msgspec skips a ClassVar that only the class holds."""

    user_id: int
    registry: ClassVar[Any] = {"admin": account()}


@dataclasses.dataclass(slots=True)
class SlotOwner:
    owner: Any = None


class SlotOwnerWithDict(SlotOwner):
    """A subclass with a __dict__: msgspec reads the dict before the slot of the base."""


class Audited:
    """A data descriptor that gives a plain value on class access."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, cls: type | None = None) -> Any:
        return None if obj is None else "<audited>"

    def __set__(self, obj: Any, value: Any) -> None:
        obj.__dict__[self.name] = value


@dataclasses.dataclass
class Event:
    actor: Any = Audited()


@dataclasses.dataclass
class Row:
    name: str
    price: Any = None


class LazyRow(Row):
    """msgspec skips a field whose getter raises."""

    @property
    def price(self) -> Any:
        raise LookupError("price is not loaded")

    @price.setter
    def price(self, value: Any) -> None:
        pass


@dataclasses.dataclass(slots=True)
class LateProperty:
    owner: Any = None


# Serializers that hide nothing: msgspec encodes them natively, as before.


class Point(Serializer, array_like=True):
    x: int
    y: int


class Sparse(Serializer, omit_defaults=True):
    id: int
    note: str = ""
    tags: list[str] = msgspec.field(default_factory=list)


def _check(response, expected) -> None:
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert response.json() == expected


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/attrs/cached-property")
    def cached_property_route():
        card = Card(title="t")
        assert card.owner.id == 1
        return {"card": card}

    @api.get("/attrs/dict-attribute")
    def dict_attribute_route():
        doc = Doc(title="t")
        doc.owner = account()
        doc._private = account()
        return [doc]

    @api.get("/attrs/legacy")
    def legacy_route():
        return {"item": Legacy(user_id=1)}

    @api.get("/attrs/base-slot")
    def base_slot_route():
        item = WithBaseSlot(user_id=1)
        item.extra = account()
        return {"item": item}

    @api.get("/attrs/own-getattribute")
    def own_getattribute_route():
        return [Masking(owner=account())]

    @api.get("/dataclass/initvar")
    def initvar_route():
        return {"item": WithInitVar(user_id=1, owner=account())}

    @api.get("/dataclass/classvar-on-instance")
    def classvar_on_instance_route():
        item = WithClassVar(user_id=1)
        item.current = account()
        return [item]

    @api.get("/dataclass/classvar-on-class")
    def classvar_on_class_route():
        return [SharedClassVar(user_id=1)]

    @api.get("/dataclass/dict-over-slot")
    def dict_over_slot_route():
        item = SlotOwnerWithDict(owner=None)
        item.__dict__["owner"] = account()
        return [item]

    @api.get("/dataclass/descriptor")
    def descriptor_route():
        return [Event(actor=account())]

    @api.get("/dataclass/getter-raises")
    def getter_raises_route():
        return [LazyRow("a")]

    @api.get("/dataclass/getter-raises-next-to-a-serializer")
    def getter_raises_next_to_a_serializer_route():
        return [LazyRow("a"), account()]

    # The object exists before its class gets the property: the property has no setter.
    late = LateProperty(owner=1)

    @api.get("/dataclass/late-property")
    def late_property_route():
        return [late]

    @api.get("/nested/no-hidden-field")
    def no_hidden_field_route():
        return {"point": Point(x=1, y=2), "sparse": Sparse(id=1)}

    with TestClient(api) as test_client:
        yield test_client


def test_a_cached_property_of_a_slotted_attrs_class_gives_the_dump_of_its_serializer(client):
    _check(client.get("/attrs/cached-property"), {"card": {"title": "t", "owner": DUMPED}})


def test_an_attribute_set_on_an_attrs_object_gives_the_dump_of_its_serializer(client):
    """msgspec skips the keys that start with an underscore."""
    _check(client.get("/attrs/dict-attribute"), [{"title": "t", "owner": DUMPED}])


def test_an_attribute_set_after_the_init_of_an_attr_s_class_gives_the_dump_of_its_serializer(client):
    _check(client.get("/attrs/legacy"), {"item": {"user_id": 1, "owner": DUMPED}})


def test_a_slot_of_a_plain_base_of_an_attrs_class_gives_the_dump_of_its_serializer(client):
    _check(client.get("/attrs/base-slot"), {"item": {"user_id": 1, "extra": DUMPED}})


def test_an_attrs_slot_is_read_in_place_as_msgspec_reads_it(client):
    _check(client.get("/attrs/own-getattribute"), [{"owner": DUMPED}])


def test_an_initvar_that_a_dataclass_keeps_gives_the_dump_of_its_serializer(client):
    _check(client.get("/dataclass/initvar"), {"item": {"user_id": 1, "owner": DUMPED}})


def test_a_classvar_set_on_a_dataclass_object_gives_the_dump_of_its_serializer(client):
    _check(client.get("/dataclass/classvar-on-instance"), [{"user_id": 1, "current": DUMPED}])


def test_a_classvar_that_only_the_class_holds_does_not_change_the_response(client):
    _check(client.get("/dataclass/classvar-on-class"), [{"user_id": 1}])


def test_a_dict_entry_over_a_slot_of_the_base_gives_the_dump_of_its_serializer(client):
    _check(client.get("/dataclass/dict-over-slot"), [{"owner": DUMPED}])


def test_a_data_descriptor_field_is_read_from_the_dict_first(client):
    _check(client.get("/dataclass/descriptor"), [{"actor": DUMPED}])


def test_a_field_whose_getter_raises_is_skipped_as_msgspec_skips_it(client):
    _check(client.get("/dataclass/getter-raises"), [{"name": "a"}])


def test_a_field_whose_getter_raises_is_skipped_next_to_a_serializer(client):
    _check(client.get("/dataclass/getter-raises-next-to-a-serializer"), [{"name": "a"}, DUMPED])


def test_a_property_that_replaces_a_slot_after_a_scan_is_read_as_msgspec_reads_it(client):
    """The property has no setter, so the dump cannot replace its Serializer. The response fails closed."""
    _check(client.get("/dataclass/late-property"), [{"owner": 1}])
    slot = LateProperty.__dict__["owner"]
    LateProperty.owner = property(lambda _self: account())
    try:
        response = client.get("/dataclass/late-property")
        assert response.status_code == 500
        assert SECRET not in response.text
    finally:
        LateProperty.owner = slot


def test_a_nested_serializer_that_hides_nothing_keeps_the_encoding_of_msgspec(client):
    """array_like and omit_defaults apply as msgspec encodes them, as before the dump of nested Serializers."""
    _check(client.get("/nested/no-hidden-field"), {"point": [1, 2], "sparse": {"id": 1}})


def test_the_scan_caches_do_not_keep_classes_that_an_app_makes_at_runtime():
    """Each cache keeps a bounded number of classes. It drops the oldest one first."""
    refs = []
    for index in range(1500):
        cls = dataclasses.make_dataclass(f"Runtime{index}", [("value", Any)], slots=True)
        assert json.loads(_json.encode({"item": cls(value=account())})) == {"item": {"value": DUMPED}}
        refs.append(weakref.ref(cls))
        del cls
    gc.collect()
    alive = sum(ref() is not None for ref in refs)
    assert len(SCAN_KINDS) <= 512
    assert len(SCAN_OFFSETS) <= 512
    assert alive <= 1024


_PACKAGE_ROOT = str(Path(django_bolt.__file__).resolve().parents[1])


def test_the_stack_of_a_worker_thread_grows_only_where_cpython_needs_it():
    """CPython 3.12 and 3.13 need about 3 MiB of C stack for a JSON body at the recursion limit.

    CPython 3.14 checks the stack itself, so Bolt keeps the default size there.
    """
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {_PACKAGE_ROOT!r})
        import ctypes, os
        import django
        from django.conf import settings
        settings.configure(INSTALLED_APPS=["django_bolt"], SECRET_KEY="x" * 40)
        django.setup()
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        libc = ctypes.CDLL(None)
        libc.pthread_self.restype = ctypes.c_ulong
        libc.pthread_getattr_np.argtypes = [ctypes.c_ulong, ctypes.c_void_p]

        def stack_size():
            attributes = ctypes.create_string_buffer(128)
            libc.pthread_getattr_np(libc.pthread_self(), attributes)
            size = ctypes.c_size_t()
            address = ctypes.c_void_p()
            libc.pthread_attr_getstack(attributes, ctypes.byref(address), ctypes.byref(size))
            libc.pthread_attr_destroy(attributes)
            return size.value

        api = BoltAPI()

        @api.get("/stack")
        def stack():
            return {{"stack": stack_size()}}

        print(os.environ.get("RUST_MIN_STACK"))
        with TestClient(api) as client:
            print(client.get("/stack").json()["stack"] // 2**20)
        """
    )
    env = {key: value for key, value in os.environ.items() if key != "RUST_MIN_STACK"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr[-2000:]
    variable, stack_mib = result.stdout.split()
    assert variable == "None"
    assert int(stack_mib) == (8 if sys.version_info < (3, 14) else 2)
