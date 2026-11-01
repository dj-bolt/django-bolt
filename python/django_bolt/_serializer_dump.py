"""Dump each Bolt Serializer that a JSON value holds.

msgspec encodes each Struct natively. Thus a Serializer in a container sent
its write-only fields and lost its computed fields. ``_json.encode`` asks the
Rust scan ``find_serializer`` whether a value holds a Serializer. Only then
does ``dump_serializers`` rebuild the value with the dump of each Serializer.

The scan reads the kind of each type from ``SCAN_KINDS``:

- ``None``: the scan does not look into a value of the type.
- ``True``: the type is a Serializer whose dump() differs from the
  encoding of msgspec.
- a tuple of field names: the fields of a Struct. msgspec reads them from
  their slots, so the scan and the dump do too. A Serializer whose dump()
  gives what msgspec encodes has this kind too.
- an ``AttributeNames`` tuple: the fields of a dataclass or an attrs class.
  msgspec reads them with ``getattr`` when the class has its own attribute
  lookup, so the scan and the dump use ``getattr``.

The kind of a Struct, dataclass or attrs class lists each of its fields.
Nothing checks the type of a field, so an ``int`` field can hold a
Serializer too. The Rust scan reads a slot at its offset, which costs about
as much as a leaf check.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import msgspec

from ._core import find_serializer

# The types of values that cannot hold a Serializer. An Enum is a leaf only
# when each member value is a leaf: see _enum_is_leaf.
_LEAF_TYPES = (str, int, float, bytes, bytearray, memoryview, datetime, date, time, timedelta, UUID, Decimal)

SCAN_KINDS: dict[type, Any] = {}
# The slot offsets of the fields of each Struct type. The Rust scan fills it.
SCAN_OFFSETS: dict[type, Any] = {}

# The maximum number of types in SCAN_KINDS. A type past it gets its kind
# again each time. This keeps memory bounded when an app makes types at runtime.
_SCAN_KINDS_LIMIT = 4096

_MISSING = object()


class AttributeNames(tuple):
    """The fields of a dataclass or an attrs class, which msgspec reads with ``getattr``."""

    __slots__ = ()


# The kind of an Enum with a member value that is not a leaf. msgspec encodes
# a member as its value, so the scan reads the value.
_ENUM_VALUE = AttributeNames(("_value_",))


def _enum_is_leaf(tp: type[Enum]) -> bool:
    """Return whether each member value of ``tp`` is None or a leaf.

    The members of an Enum do not change after the class is made.
    """
    return all(member._value_ is None or isinstance(member._value_, _LEAF_TYPES) for member in tp.__members__.values())


def _is_serializer_class(tp: Any) -> bool:
    return bool(getattr(tp, "__is_bolt_serializer__", False))


def _dumps_as_encoded(tp: Any) -> bool:
    """Return whether msgspec encodes an instance of the Serializer ``tp`` as its dump() gives it.

    Such a class has no write-only, computed or excluded field, no nested
    Serializer field, no ORM state check and no dump() of its own. Its dump()
    then gives the fields as msgspec encodes them, unless ``omit_defaults``
    or ``array_like`` changes the encoding. The scan reads the fields of such
    a class as the fields of a Struct, so the class costs no dump().
    """
    tp._ensure_dump_ready()
    config = tp.__struct_config__
    dump_owner = next(base for base in tp.__mro__ if "dump" in base.__dict__)
    return bool(
        tp.__dump_fast_path__
        and not tp.__orm_state_check_fields__
        and not config.omit_defaults
        and not config.array_like
        and "__is_bolt_serializer__" in dump_owner.__dict__
    )


def _field_names(tp: type) -> tuple[str, ...] | None:
    """Return the attribute of each field that msgspec encodes, or None.

    A Struct gives a tuple, and a dataclass or an attrs class gives an
    ``AttributeNames`` tuple.
    None means that msgspec does not encode the fields of the type, so no
    field can hold a Serializer that msgspec sends.
    """
    if issubclass(tp, msgspec.Struct):
        return tp.__struct_fields__
    if dataclasses.is_dataclass(tp):
        return AttributeNames(field.name for field in dataclasses.fields(tp))
    if (attributes := getattr(tp, "__attrs_attrs__", None)) is not None:
        return AttributeNames(attribute.name for attribute in attributes)
    return None


def _kind_of(tp: type) -> Any:
    if _is_serializer_class(tp):
        return tp.__struct_fields__ if _dumps_as_encoded(tp) else True
    if issubclass(tp, Enum):
        return None if _enum_is_leaf(tp) else _ENUM_VALUE
    return _field_names(tp) or None


def classify(tp: type) -> Any:
    """Return the kind of ``tp`` for the scan, and keep it in SCAN_KINDS."""
    kind = _kind_of(tp)
    metaclass = type(tp)
    # A dict finds a key by equality. A metaclass with its own __eq__ or __hash__
    # can make two classes equal, and one would get the kind of the other. Such a
    # class, or one that cannot be a key, gets its kind again each time.
    if metaclass.__eq__ is type.__eq__ and metaclass.__hash__ is type.__hash__ and len(SCAN_KINDS) < _SCAN_KINDS_LIMIT:
        SCAN_KINDS[tp] = kind
    return kind


def _kind_of_instance(value: Any) -> Any:
    """Return the kind of the class of ``value``, or ``_MISSING`` if SCAN_KINDS does not hold it."""
    try:
        return SCAN_KINDS.get(value.__class__, _MISSING)
    except TypeError:
        # A class that cannot be a key: see classify.
        return _MISSING


# The walk asks the Rust scan before it goes into an item only at the first
# levels. Below them it goes into each item. Thus the scans cost at most this
# many passes over the value, also when a client nests a Serializer deep in
# data that the handler returns.
_SCAN_DEPTH = 8


def _dump_item(item: Any, depth: int) -> Any:
    """Return ``item`` with its Serializers dumped, or ``item`` itself if it holds none.

    The walk skips each str, int, float, bool and None item before the call.
    A Serializer or a leaf costs one kind lookup. Above ``_SCAN_DEPTH``, each
    other item asks the Rust scan first, so the walk goes only into the items
    that hold a Serializer.
    """
    kind = _kind_of_instance(item)
    if kind is True:
        dumped = item.dump()
        return _dump_dict(dumped, depth) if dumped.__class__ is dict else _dump(dumped, depth)
    if kind is None:
        return item
    if depth < _SCAN_DEPTH and not find_serializer(item, SCAN_KINDS, classify, SCAN_OFFSETS):
        return item
    return _dump(item, depth + 1)


def _dump_dict(value: dict[Any, Any], depth: int) -> dict[Any, Any]:
    """Return ``value``, or a copy with the dump of each Serializer that it holds."""
    dumped: dict[Any, Any] | None = None
    # dict.items reads the storage, as msgspec and the scan do. A subclass can override items().
    for key, item in dict.items(value):
        # Identity compares, not a set: a set would hash the class, and some classes cannot be hashed.
        cls = item.__class__
        if cls is str or cls is int or cls is float or cls is bool or item is None:
            continue
        new_item = _dump_item(item, depth)
        if new_item is not item:
            if dumped is None:
                # The storage again: dict.copy and dict() of a subclass call its keys() or __iter__.
                dumped = dict(dict.items(value))
            dumped[key] = new_item
    return value if dumped is None else dumped


def _items(value: list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any]) -> Any:
    """Return an iterator over the storage of ``value``, as msgspec reads it. A subclass can override __iter__."""
    if isinstance(value, list):
        return list.__iter__(value)
    if isinstance(value, tuple):
        return tuple.__iter__(value)
    return iter(value)


def _dump(value: Any, depth: int) -> Any:
    """Return ``value`` with the dump of each Serializer in place of the Serializer. See dump_serializers."""
    if isinstance(value, dict):
        return _dump_dict(value, depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        dumped_list: list[Any] | None = None
        for index, item in enumerate(_items(value)):
            cls = item.__class__
            if cls is str or cls is int or cls is float or cls is bool or item is None:
                new_item = item
            else:
                new_item = _dump_item(item, depth)
            if dumped_list is not None:
                dumped_list.append(new_item)
            elif new_item is not item:
                dumped_list = list(itertools.islice(_items(value), index))
                dumped_list.append(new_item)
        return value if dumped_list is None else dumped_list
    if value is None or isinstance(value, (str, int, float, bytes, bytearray)):
        return value
    kind = _kind_of_instance(value)
    if kind is _MISSING:
        kind = classify(value.__class__)
    if kind is None:
        return value
    if kind is True:
        return _dump(value.dump(), depth)
    if kind is _ENUM_VALUE:
        # The dumped value gives the JSON that msgspec gives for the member.
        item = value._value_
        new_item = _dump_item(item, depth)
        return value if new_item is item else new_item
    is_struct = kind.__class__ is tuple
    # msgspec reads a Struct field from its slot, so a __getattribute__ of the type does not apply.
    get_attribute = object.__getattribute__ if is_struct else getattr
    changes = {}
    for name in kind:
        try:
            item = get_attribute(value, name)
        except AttributeError:
            continue
        cls = item.__class__
        if cls is str or cls is int or cls is float or cls is bool or item is None:
            continue
        new_item = _dump_item(item, depth)
        if new_item is not item:
            changes[name] = new_item
    if not changes:
        return value
    # A copy keeps the encoding options of the type: rename, tag, omit_defaults.
    if is_struct:
        # The copy of msgspec itself: a __copy__ of the type could return the value.
        clone = msgspec.Struct.__copy__(value)
        set_attribute = msgspec.structs.force_setattr
    else:
        clone = copy.copy(value)
        set_attribute = object.__setattr__
        if clone is value:
            raise TypeError(
                f"{type(value).__qualname__}.__copy__ returns the object itself. "
                "Bolt cannot dump the Serializer that it holds without a change to the object."
            )
    for name, new_item in changes.items():
        set_attribute(clone, name, new_item)
        # msgspec reads this field with getattr, so the clone must give back the dumped value.
        if not is_struct and getattr(clone, name) is not new_item:
            raise TypeError(
                f"{type(value).__qualname__}.{name} does not give back the value that Bolt sets. "
                "Bolt cannot dump the Serializer that it holds."
            )
    return clone


def dump_serializers(value: Any) -> Any:
    """Return ``value`` with the dump of each Serializer in place of the Serializer.

    The function goes only into the items that hold a Serializer, as the
    Rust scan tells, and copies only the containers on the path to a
    Serializer. A tuple, set or frozenset on that path becomes a list, which
    msgspec encodes as the same JSON array.

    The result is scanned again. If it still holds a Serializer, the function
    raises ``TypeError``, so msgspec never encodes the write-only fields.
    """
    try:
        dumped = _dump(value, 0)
    except RecursionError:
        # A short error: the traceback of the walk would hold a frame for each level.
        raise RecursionError("A Serializer is nested too deeply in the value for Bolt to dump it.") from None
    if find_serializer(dumped, SCAN_KINDS, classify, SCAN_OFFSETS):
        raise TypeError(f"The value still holds a Serializer after its dump: {type(value).__qualname__}.")
    return dumped
