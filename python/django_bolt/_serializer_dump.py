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
  their slots, so the scan and the dump do too. A Serializer that hides
  nothing has this kind too: msgspec encodes it as before.
- an ``AttributeNames`` tuple: the keys of ``__dataclass_fields__``. msgspec
  reads each one from the ``__dict__`` first, then from a data descriptor of
  the class. A class with its own attribute lookup gets ``getattr``.
- ``OBJECT_FIELDS``: an attrs class. msgspec encodes it as a plain object:
  each public key of its ``__dict__`` and each public slot of the classes in
  its base chain.

Nothing checks the type of a field, so an ``int`` field can hold a
Serializer too. The Rust scan reads a slot at its offset, which costs about
as much as a leaf check. msgspec skips a field whose getter raises, so the
scan and the dump skip it too.
"""

from __future__ import annotations

import copy
import itertools
import types
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

# The maximum number of types in each cache. When a cache is full, its oldest
# type goes: a dict keeps the order of insertion. Thus the caches keep a
# bounded number of types alive when an app makes types at runtime.
_CACHE_LIMIT = 512

_MISSING = object()
_MEMBER_DESCRIPTOR = types.MemberDescriptorType


class AttributeNames(tuple):
    """The keys of ``__dataclass_fields__``, which msgspec reads as dataclass fields."""

    __slots__ = ()


class ObjectFields(tuple):
    """The kind of an attrs class. msgspec encodes such an object as a plain object."""

    __slots__ = ()
    # The Rust scan finds this attribute on the kind.
    object_fields = True


OBJECT_FIELDS = ObjectFields()


# The kind of an Enum with a member value that is not a leaf. msgspec encodes
# a member as its value, so the scan reads the value.
_ENUM_VALUE = AttributeNames(("_value_",))


def _is_leaf_value(value: Any) -> bool:
    """Return whether ``value`` cannot hold a Serializer.

    msgspec encodes the fields of a dataclass or an attrs class, also of a
    subclass of str or int, so such a value is not a leaf.
    """
    if value is None:
        return True
    if not isinstance(value, _LEAF_TYPES):
        return False
    tp = value.__class__
    return not (hasattr(tp, "__dataclass_fields__") or hasattr(tp, "__attrs_attrs__"))


def _enum_is_leaf(tp: type[Enum]) -> bool:
    """Return whether each member value of ``tp`` is None or a leaf.

    The members of an Enum do not change after the class is made.
    """
    return all(_is_leaf_value(member._value_) for member in tp.__members__.values())


def _is_serializer_class(tp: Any) -> bool:
    return bool(getattr(tp, "__is_bolt_serializer__", False))


def _dumps_as_encoded(tp: Any) -> bool:
    """Return whether the Serializer ``tp`` hides nothing, so that msgspec can encode it natively.

    Such a class has no write-only, computed or excluded field, no nested
    Serializer field, no ORM state check and no dump() of its own. msgspec
    encodes it as before: also with ``omit_defaults`` or ``array_like``. The
    scan reads its fields as the fields of a Struct, so it costs no dump().
    """
    tp._ensure_dump_ready()
    dump_owner = next(base for base in tp.__mro__ if "dump" in base.__dict__)
    return bool(
        tp.__dump_fast_path__ and not tp.__orm_state_check_fields__ and "__is_bolt_serializer__" in dump_owner.__dict__
    )


# The attribute lookup of ``type`` itself. A metaclass cannot change what these give.
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_DICT = type.__dict__["__dict__"]


def _type_lookup(tp: type, name: str) -> Any:
    """Return the attribute ``name`` of ``tp`` from the dicts of its MRO, or ``_MISSING``."""
    for base in _TYPE_MRO.__get__(tp):
        namespace = _TYPE_DICT.__get__(base)
        if name in namespace:
            return namespace[name]
    return _MISSING


def _field_names(tp: type) -> tuple[str, ...] | None:
    """Return the kind of a type with fields that msgspec encodes, or None.

    A Struct gives the names of its fields. A dataclass gives an
    ``AttributeNames`` tuple of the keys of ``__dataclass_fields__``. This
    includes the ClassVar and InitVar entries, which msgspec encodes when the
    object holds them. An attrs class gives ``OBJECT_FIELDS``. msgspec finds
    ``__attrs_attrs__`` only in the dict of the class itself.
    None means that msgspec does not encode the fields of the type, so no
    field can hold a Serializer that msgspec sends.
    """
    if issubclass(tp, msgspec.Struct):
        return tp.__struct_fields__
    fields = _type_lookup(tp, "__dataclass_fields__")
    if isinstance(fields, dict):
        return AttributeNames(name for name in fields if name.__class__ is str)
    if "__attrs_attrs__" in _TYPE_DICT.__get__(tp):
        return OBJECT_FIELDS
    return None


def _kind_of(tp: type) -> Any:
    if _is_serializer_class(tp):
        return tp.__struct_fields__ if _dumps_as_encoded(tp) else True
    if issubclass(tp, Enum):
        return None if _enum_is_leaf(tp) else _ENUM_VALUE
    return _field_names(tp)


def _remember(cache: dict[Any, Any], key: Any, value: Any) -> None:
    """Keep ``value`` in ``cache``. When the cache is full, its oldest entry goes."""
    if len(cache) >= _CACHE_LIMIT:
        try:
            del cache[next(iter(cache))]
        except (KeyError, RuntimeError, StopIteration):
            # Another thread changed the cache. It is a cache, so the entry stays out.
            return
    cache[key] = value


def classify(tp: type) -> Any:
    """Return the kind of ``tp`` for the scan, and keep it in SCAN_KINDS."""
    kind = _kind_of(tp)
    metaclass = type(tp)
    # A dict finds a key by equality. A metaclass with its own __eq__ or __hash__
    # can make two classes equal, and one would get the kind of the other. Such a
    # class, or one that cannot be a key, gets its kind again each time.
    if metaclass.__eq__ is type.__eq__ and metaclass.__hash__ is type.__hash__:
        _remember(SCAN_KINDS, tp, kind)
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


# The read rule of each dataclass type: (whether it uses the attribute lookup of
# object, the data descriptor of each name or None). An entry can be old: then
# the dump reads a field where msgspec does not, and dump_serializers fails closed.
_DATACLASS_RULES: dict[type, tuple[bool, tuple[Any, ...]]] = {}
# The member descriptors of the public object slots of each attrs type.
_OBJECT_SLOTS: dict[type, tuple[Any, ...]] = {}

# Where a field value comes from, and thus where the dump writes its new value.
_IN_DICT = 0
_IN_DESCRIPTOR = 1
_BY_GETATTR = 2


def _uses_generic_getattr(cls: type) -> bool:
    """Return whether ``cls`` uses the attribute lookup of ``object``, with no ``__getattr__``."""
    return cls.__getattribute__ is object.__getattribute__ and _type_lookup(cls, "__getattr__") is _MISSING


def _is_data_descriptor(attribute: Any) -> bool:
    """Return whether ``attribute`` has both a getter and a setter, as msgspec requires."""
    attribute_type = type(attribute)
    return hasattr(attribute_type, "__get__") and (
        hasattr(attribute_type, "__set__") or hasattr(attribute_type, "__delete__")
    )


def _dataclass_rule(cls: type, names: tuple[str, ...]) -> tuple[bool, tuple[Any, ...]]:
    rule = _DATACLASS_RULES.get(cls)
    if rule is None:
        generic = _uses_generic_getattr(cls)
        descriptors = []
        for name in names:
            attribute = _type_lookup(cls, name) if generic else _MISSING
            descriptors.append(attribute if attribute is not _MISSING and _is_data_descriptor(attribute) else None)
        rule = (generic, tuple(descriptors))
        _remember(_DATACLASS_RULES, cls, rule)
    return rule


def _instance_dict(value: Any) -> dict[str, Any] | None:
    """Return the ``__dict__`` of ``value``, or None if it has none."""
    try:
        return object.__getattribute__(value, "__dict__")
    except AttributeError:
        return None


def _dataclass_fields(value: Any, names: tuple[str, ...]) -> list[tuple[int, Any, Any]]:
    """Return each field of a dataclass object that msgspec encodes, as (where, key, value).

    msgspec reads the ``__dict__`` first, then a data descriptor of the class.
    It skips a field that neither gives, and a field whose getter raises. A
    class with its own attribute lookup gets ``getattr``.
    """
    cls = value.__class__
    generic, descriptors = _dataclass_rule(cls, names)
    fields = []
    if not generic:
        for name in names:
            try:
                fields.append((_BY_GETATTR, name, getattr(value, name)))
            except Exception:  # noqa: BLE001 - msgspec skips a field whose lookup raises.
                continue
        return fields
    instance_dict = _instance_dict(value)
    for name, descriptor in zip(names, descriptors, strict=True):
        if instance_dict is not None and name in instance_dict:
            fields.append((_IN_DICT, name, instance_dict[name]))
        elif descriptor is not None:
            try:
                fields.append((_IN_DESCRIPTOR, descriptor, type(descriptor).__get__(descriptor, value, cls)))
            except Exception:  # noqa: BLE001 - msgspec skips a field whose getter raises.
                continue
    return fields


def _object_slots(cls: type) -> tuple[Any, ...]:
    """Return the member descriptors of the public object slots of ``cls`` and its bases."""
    slots = _OBJECT_SLOTS.get(cls)
    if slots is None:
        found = []
        owner: type | None = cls
        while owner is not None:
            for attribute in _TYPE_DICT.__get__(owner).values():
                if (
                    type(attribute) is _MEMBER_DESCRIPTOR
                    and attribute.__objclass__ is owner
                    and not attribute.__name__.startswith("_")
                ):
                    found.append(attribute)
            owner = owner.__base__
        slots = tuple(found)
        _remember(_OBJECT_SLOTS, cls, slots)
    return slots


def _object_fields(value: Any) -> list[tuple[int, Any, Any]]:
    """Return each field of an attrs object that msgspec encodes, as (where, key, value).

    msgspec encodes the keys of the ``__dict__`` that are a str and do not
    start with an underscore, then each such object slot that is set.
    """
    fields = []
    instance_dict = _instance_dict(value)
    if instance_dict is not None:
        for key, item in dict.items(instance_dict):
            if key.__class__ is str and not key.startswith("_"):
                fields.append((_IN_DICT, key, item))
    cls = value.__class__
    for slot in _object_slots(cls):
        try:
            fields.append((_IN_DESCRIPTOR, slot, slot.__get__(value, cls)))
        except AttributeError:
            # The slot is not set.
            continue
    return fields


def _write_field(clone: Any, where: int, key: Any, new_item: Any) -> None:
    """Set a field of ``clone`` where msgspec reads it: see _dataclass_fields and _object_fields."""
    if where == _IN_DICT:
        object.__getattribute__(clone, "__dict__")[key] = new_item
    elif where == _IN_DESCRIPTOR:
        # A descriptor with no setter raises, and the response fails.
        type(key).__set__(key, clone, new_item)
    else:
        object.__setattr__(clone, key, new_item)


def _dump(value: Any, depth: int) -> Any:
    """Return ``value`` with the dump of each Serializer in place of the Serializer. See dump_serializers."""
    if isinstance(value, dict):
        return _dump_dict(value, depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        if value.__class__ is list and value:
            batch = _dump_serializer_list(value, depth)
            if batch is not None:
                return batch
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
    # Identity compares: a subclass of a scalar type can be a dataclass, whose fields msgspec encodes.
    cls = value.__class__
    if cls is str or cls is int or cls is float or cls is bool or cls is bytes or value is None:
        return value
    kind = _kind_of_instance(value)
    if kind is _MISSING:
        kind = classify(cls)
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
    if is_struct:
        # msgspec reads a Struct field from the slot that the class had when it was made,
        # as astuple does, also when a property now hides the field.
        fields = [(None, name, item) for name, item in zip(kind, msgspec.structs.astuple(value), strict=True)]
    elif kind is OBJECT_FIELDS:
        fields = _object_fields(value)
    else:
        fields = _dataclass_fields(value, kind)
    changes = {}
    for where, key, item in fields:
        item_cls = item.__class__
        if item_cls is str or item_cls is int or item_cls is float or item_cls is bool or item is None:
            continue
        new_item = _dump_item(item, depth)
        if new_item is not item:
            changes[where, key] = new_item
    if not changes:
        return value
    # A copy keeps the encoding options of the type: rename, tag, omit_defaults.
    if is_struct:
        # The copy of msgspec itself: a __copy__ of the type could return the value.
        clone = msgspec.Struct.__copy__(value)
        for (_, name), new_item in changes.items():
            msgspec.structs.force_setattr(clone, name, new_item)
        return clone
    clone = copy.copy(value)
    if clone is value:
        raise TypeError(
            f"{cls.__qualname__}.__copy__ returns the object itself. "
            "Bolt cannot dump the Serializer that it holds without a change to the object."
        )
    for where, key, item in fields:
        new_item = changes.get((where, key), _MISSING)
        if new_item is not _MISSING:
            # A field that does not give back the dumped value still holds the
            # Serializer for msgspec. dump_serializers then finds it and raises.
            _write_field(clone, where, key, new_item)
        elif where == _IN_DICT or kind is OBJECT_FIELDS:
            # A copy can leave out a key or a slot that msgspec encodes, for
            # example the slot of a cached property. Thus it gets each one.
            _write_field(clone, where, key, item)
    return clone


def _dump_serializer_list(value: list[Any], depth: int) -> list[Any] | None:
    """Return the dumps of a list of Serializers of one class that needs dump(), or None.

    One native scan of the dumps replaces the walk of each dump. The walk
    goes into the dumps only if the scan finds a Serializer in them.
    """
    first = value[0]
    if _kind_of_instance(first) is not True:
        return None
    cls = first.__class__
    for item in value:
        if item.__class__ is not cls:
            return None
    dumps = [item.dump() for item in value]
    if not find_serializer(dumps, SCAN_KINDS, classify, SCAN_OFFSETS):
        return dumps
    return _dump(dumps, depth)


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
