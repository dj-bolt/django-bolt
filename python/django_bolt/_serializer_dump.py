"""Dump each Bolt Serializer that a JSON value holds.

msgspec encodes each Struct natively. Thus a Serializer in a container sent
its write-only fields and lost its computed fields. ``_json.encode`` asks the
Rust scan ``find_serializer`` whether a value holds a Serializer. Only then
does ``dump_serializers`` rebuild the value with the dump of each Serializer.

The scan reads the kind of each type from ``SCAN_KINDS``:

- ``None``: the scan does not look into a value of the type.
- ``True``: the type is a Serializer.
- a tuple of field names: the fields of a Struct. msgspec reads them from
  their slots, so the scan and the dump do too.
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
import types
from collections import abc
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from uuid import UUID

import msgspec

# The types of values that cannot hold a Serializer.
_LEAF_TYPES = (str, int, float, bytes, bytearray, memoryview, datetime, date, time, timedelta, UUID, Decimal, Enum)

# The generic origins whose arguments give the types of all their items.
_CONTAINER_ORIGINS = frozenset(
    {
        list,
        tuple,
        set,
        frozenset,
        dict,
        abc.Sequence,
        abc.MutableSequence,
        abc.Set,
        abc.MutableSet,
        abc.Mapping,
        abc.MutableMapping,
        Union,
        types.UnionType,
    }
)

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


def _is_serializer_class(tp: Any) -> bool:
    return bool(getattr(tp, "__is_bolt_serializer__", False))


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


def annotation_excludes_serializer(annotation: Any) -> bool:
    """Return whether ``annotation`` holds only leaf values, which cannot be a Serializer.

    A leaf is None, a str, a number, bytes, a date or time, a UUID, a
    Decimal, an Enum or a Literal value. A container or a union of leaves is
    a leaf type too. A Struct, dataclass or other class is not: nothing
    checks the types of its fields, so a field can hold a Serializer.
    """
    origin = get_origin(annotation)
    if origin is None:
        if annotation is type(None):
            return True
        return isinstance(annotation, type) and issubclass(annotation, _LEAF_TYPES)
    if origin is Literal:
        return True
    args = get_args(annotation)
    if origin is Annotated:
        return annotation_excludes_serializer(args[0])
    if origin not in _CONTAINER_ORIGINS or not args:
        return False
    return all(arg is Ellipsis or annotation_excludes_serializer(arg) for arg in args)


def _kind_of(tp: type) -> Any:
    if _is_serializer_class(tp):
        return True
    return _field_names(tp) or None


def classify(tp: type) -> Any:
    """Return the kind of ``tp`` for the scan, and keep it in SCAN_KINDS."""
    kind = _kind_of(tp)
    if len(SCAN_KINDS) < _SCAN_KINDS_LIMIT:
        SCAN_KINDS[tp] = kind
    return kind


def dump_serializers(value: Any) -> Any:
    """Return ``value`` with the dump of each Serializer in place of the Serializer.

    The function copies only the containers on the path to a Serializer. A
    tuple, set or frozenset on that path becomes a list, which msgspec
    encodes as the same JSON array.
    """
    if isinstance(value, dict):
        dumped_dict: dict[Any, Any] | None = None
        for key, item in value.items():
            new_item = dump_serializers(item)
            if new_item is not item:
                if dumped_dict is None:
                    dumped_dict = dict(value)
                dumped_dict[key] = new_item
        return value if dumped_dict is None else dumped_dict
    if isinstance(value, (list, tuple, set, frozenset)):
        dumped_list: list[Any] | None = None
        for index, item in enumerate(value):
            new_item = dump_serializers(item)
            if dumped_list is not None:
                dumped_list.append(new_item)
            elif new_item is not item:
                dumped_list = list(itertools.islice(value, index))
                dumped_list.append(new_item)
        return value if dumped_list is None else dumped_list
    if value is None or isinstance(value, (str, int, float, bytes, bytearray)):
        return value
    kind = SCAN_KINDS.get(value.__class__, _MISSING)
    if kind is _MISSING:
        kind = classify(value.__class__)
    if kind is None:
        return value
    if kind is True:
        return dump_serializers(value.dump())
    is_struct = kind.__class__ is tuple
    # msgspec reads a Struct field from its slot, so a __getattribute__ of the type does not apply.
    get_attribute = object.__getattribute__ if is_struct else getattr
    changes = {}
    for name in kind:
        try:
            item = get_attribute(value, name)
        except AttributeError:
            continue
        new_item = dump_serializers(item)
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
    return clone
