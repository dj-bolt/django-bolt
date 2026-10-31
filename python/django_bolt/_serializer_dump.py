"""Dump each Bolt Serializer that a JSON value holds.

msgspec encodes each Struct natively. Thus a Serializer in a container sent
its write-only fields and lost its computed fields. ``_json.encode`` asks the
Rust scan ``find_serializer`` whether a value holds a Serializer. Only then
does ``dump_serializers`` rebuild the value with the dump of each Serializer.

The scan reads the kind of each type from ``SCAN_KINDS``:

- ``None``: the scan does not look into a value of the type.
- ``True``: the type is a Serializer.
- a tuple of attribute names: the scan looks into these attributes.

The kind of a Struct, dataclass or attrs class trusts its field annotations:
the scan skips a field whose annotation cannot hold a Serializer.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import types
import typing
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

# The maximum number of types in SCAN_KINDS. A type past it gets its kind
# again each time. This keeps memory bounded when an app makes types at runtime.
_SCAN_KINDS_LIMIT = 4096

_MISSING = object()


def _is_serializer_class(tp: Any) -> bool:
    return bool(getattr(tp, "__is_bolt_serializer__", False))


def _field_annotations(tp: type) -> list[tuple[str, Any]] | None:
    """Return (attribute, annotation) for each field that msgspec encodes, or None.

    None means that msgspec does not encode the fields of the type, so no
    field can hold a Serializer that msgspec sends.
    """
    if issubclass(tp, msgspec.Struct):
        names: tuple[str, ...] = tp.__struct_fields__
    elif dataclasses.is_dataclass(tp):
        names = tuple(field.name for field in dataclasses.fields(tp))
    elif (attributes := getattr(tp, "__attrs_attrs__", None)) is not None:
        names = tuple(attribute.name for attribute in attributes)
    else:
        return None
    try:
        hints = typing.get_type_hints(tp, include_extras=True)
    except (NameError, TypeError):
        # An annotation that does not resolve can hold anything. Each field
        # then gets Any, and the scan looks into all of them.
        hints = {}
    return [(name, hints.get(name, Any)) for name in names]


def annotation_excludes_serializer(annotation: Any, _seen: set[type] | None = None) -> bool:
    """Return whether no value of ``annotation`` can hold a Bolt Serializer.

    The check trusts the annotation. A value that breaks its annotation is
    not checked. ``Any``, a bare ``dict`` or ``list``, ``object`` and an
    unknown class can hold a Serializer.
    """
    origin = get_origin(annotation)
    if origin is None:
        if annotation is type(None):
            return True
        if not isinstance(annotation, type) or _is_serializer_class(annotation):
            return False
        if issubclass(annotation, _LEAF_TYPES):
            return True
        fields = _field_annotations(annotation)
        if fields is None:
            return False
        seen = set() if _seen is None else _seen
        if annotation in seen:
            # A recursive type: the other fields decide.
            return True
        seen.add(annotation)
        return all(annotation_excludes_serializer(field_annotation, seen) for _, field_annotation in fields)
    if origin is Literal:
        return True
    args = get_args(annotation)
    if origin is Annotated:
        return annotation_excludes_serializer(args[0], _seen)
    if origin not in _CONTAINER_ORIGINS or not args:
        return False
    return all(arg is Ellipsis or annotation_excludes_serializer(arg, _seen) for arg in args)


def _kind_of(tp: type) -> Any:
    if _is_serializer_class(tp):
        return True
    fields = _field_annotations(tp)
    if fields is None:
        return None
    names = tuple(name for name, annotation in fields if not annotation_excludes_serializer(annotation))
    return names or None


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
    changes = {}
    for name in kind:
        item = getattr(value, name, _MISSING)
        if item is _MISSING:
            continue
        new_item = dump_serializers(item)
        if new_item is not item:
            changes[name] = new_item
    if not changes:
        return value
    # A copy keeps the encoding options of the type: rename, tag, omit_defaults.
    clone = copy.copy(value)
    set_attribute = msgspec.structs.force_setattr if isinstance(value, msgspec.Struct) else object.__setattr__
    for name, new_item in changes.items():
        set_attribute(clone, name, new_item)
    return clone
