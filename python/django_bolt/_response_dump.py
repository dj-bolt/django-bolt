"""Dump each Serializer at a position of a declared response type.

msgspec encodes a Serializer natively, with all of its fields: also its
write-only fields, and none of its computed fields. A Serializer gives its
``dump()`` only where Bolt calls it. Thus Bolt compiles a function for each
declared response type, once, at registration. The function goes to each
position of the type that can hold a Serializer, and dumps the Serializer
there. A value with no declared type goes out as msgspec encodes it.

``msgspec.structs.fields`` gives the fields of a Struct as msgspec reads
them: with the generic parameters filled in and with their wire names.
"""

from __future__ import annotations

import collections
import collections.abc
import copy
import dataclasses
import functools
import types
import typing
from collections.abc import Callable
from typing import Annotated, Any, NamedTuple, NotRequired, Required, TypeVar, Union, get_args, get_origin

import msgspec

Dumper = Callable[[Any], Any]

# The origins that only add data to the type of their first argument.
# typing.ReadOnly starts in Python 3.13.
_WRAPPER_ORIGINS = frozenset(
    origin for origin in (Annotated, Required, NotRequired, getattr(typing, "ReadOnly", None)) if origin is not None
)
_UNION_ORIGINS = frozenset({Union, types.UnionType})
_ITEM_ORIGINS = frozenset(
    {
        list,
        set,
        frozenset,
        collections.deque,
        collections.abc.Collection,
        collections.abc.Sequence,
        collections.abc.MutableSequence,
        collections.abc.Set,
        collections.abc.MutableSet,
    }
)
_MAPPING_ORIGINS = frozenset(
    {
        dict,
        collections.OrderedDict,
        collections.defaultdict,
        collections.abc.Mapping,
        collections.abc.MutableMapping,
    }
)
# The values that msgspec encodes as a JSON array.
_ARRAY_VALUES = (list, tuple, set, frozenset)


class _Field(NamedTuple):
    name: str
    encode_name: str
    annotation: Any


class _Shape(NamedTuple):
    """What a value of a type holds, as msgspec encodes it.

    ``kind`` is one of: serializer, items, fixed, mapping, union, struct,
    dataclass, fields (a TypedDict). ``children`` are the types of the values
    that the value holds. ``fields`` are the fields of a struct, a dataclass
    or a TypedDict.
    """

    kind: str
    cls: Any = None
    children: tuple[Any, ...] = ()
    fields: tuple[_Field, ...] = ()


_LEAF = _Shape("leaf")


class _UnresolvedTypeError(Exception):
    """A field type names a class that does not exist yet."""


def compile_serializer_dumper(annotation: Any) -> Dumper | None:
    """Return a function that dumps each Serializer at a position of ``annotation``.

    Returns None when no position of the type can hold a Serializer. The
    route then keeps its encode path, with no extra cost.

    A module can define a field type after its routes. Then the function
    compiles the plan at its first call. If the type still does not resolve,
    the call raises TypeError, so the response fails and sends no field.
    """
    try:
        return _compile(annotation)
    except _UnresolvedTypeError:
        return _compile_at_first_call(annotation)


def _compile(annotation: Any) -> Dumper | None:
    shapes = _shapes(annotation)
    holders = _holders(shapes)
    if _key(_normalize(annotation)) not in holders:
        return None
    return _Compiler(shapes, holders).compile(annotation)


def _compile_at_first_call(annotation: Any) -> Dumper:
    compiled: list[Dumper] = []

    def dump(value: Any) -> Any:
        if not compiled:
            try:
                # Two threads can compile at the same time. They get the same plan.
                compiled.append(_compile(annotation) or _keep)
            except _UnresolvedTypeError as exc:
                raise TypeError(f"Bolt cannot resolve the response type {annotation!r}: {exc}") from exc
        return compiled[0](value)

    return dump


def _key(annotation: Any) -> Any:
    """Return a dict key for ``annotation``. An annotation with unhashable metadata gives its id."""
    try:
        hash(annotation)
    except TypeError:
        return id(annotation)
    return annotation


def _normalize(annotation: Any) -> Any:
    """Remove the wrappers that do not change what msgspec encodes."""
    while True:
        if isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) in _WRAPPER_ORIGINS:
            annotation = get_args(annotation)[0]
        elif isinstance(annotation, TypeVar):
            # A parameter with no value: msgspec reads it as Any.
            return Any
        else:
            return annotation


def _type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except NameError as exc:
        raise _UnresolvedTypeError(f"{cls.__qualname__}: {exc}") from exc


def _shape(annotation: Any) -> _Shape:
    """Return the shape of a normalized annotation."""
    origin = get_origin(annotation)
    shape = _generic_shape(origin, get_args(annotation))
    if shape is not None:
        return shape
    cls = origin or annotation
    return _class_shape(annotation, cls) if isinstance(cls, type) else _LEAF


def _generic_shape(origin: Any, args: tuple[Any, ...]) -> _Shape | None:
    """Return the shape of a union or a container type, or None for another type."""
    if origin in _UNION_ORIGINS:
        return _Shape("union", children=args)
    if origin in _ITEM_ORIGINS or (origin is tuple and args[1:] == (Ellipsis,)):
        return _Shape("items", children=args[:1])
    if origin is tuple:
        return _Shape("fixed", children=args)
    if origin in _MAPPING_ORIGINS:
        return _Shape("mapping", children=args[1:2])
    return None


def _fields_shape(kind: str, cls: type, fields: tuple[_Field, ...]) -> _Shape:
    return _Shape(kind, cls, tuple(field.annotation for field in fields), fields)


def _class_shape(annotation: Any, cls: type) -> _Shape:
    """Return the shape of a class, or of a generic alias of a class."""
    if getattr(cls, "__is_bolt_serializer__", False):
        return _Shape("serializer")
    if issubclass(cls, msgspec.Struct):
        try:
            infos = msgspec.structs.fields(annotation)
        except NameError as exc:
            raise _UnresolvedTypeError(f"{cls.__qualname__}: {exc}") from exc
        return _fields_shape("struct", cls, tuple(_Field(info.name, info.encode_name, info.type) for info in infos))
    if dataclasses.is_dataclass(cls):
        hints = _type_hints(cls)
        return _fields_shape(
            "dataclass", cls, tuple(_Field(item.name, item.name, hints[item.name]) for item in dataclasses.fields(cls))
        )
    if typing.is_typeddict(cls):
        return _fields_shape("fields", cls, tuple(_Field(name, name, hint) for name, hint in _type_hints(cls).items()))
    if issubclass(cls, tuple) and hasattr(cls, "_fields"):
        # A NamedTuple: msgspec encodes it as an array.
        hints = _type_hints(cls)
        return _Shape("fixed", children=tuple(hints.get(name, Any) for name in cls._fields))
    return _LEAF


def _shapes(annotation: Any) -> dict[Any, _Shape]:
    """Return the shape of each type that ``annotation`` reaches, by key. A recursive type is reached once."""
    shapes: dict[Any, _Shape] = {}
    stack = [annotation]
    while stack:
        current = _normalize(stack.pop())
        key = _key(current)
        if key in shapes:
            continue
        shape = _shape(current)
        shapes[key] = shape
        stack.extend(shape.children)
    return shapes


def _holders(shapes: dict[Any, _Shape]) -> set[Any]:
    """Return the keys of the types that can hold a Serializer: the Serializers, and each type above one."""
    parents: dict[Any, list[Any]] = {}
    for key, shape in shapes.items():
        for child in shape.children:
            parents.setdefault(_key(_normalize(child)), []).append(key)
    stack = [key for key, shape in shapes.items() if shape.kind == "serializer"]
    holders = set(stack)
    while stack:
        for parent in parents.get(stack.pop(), ()):
            if parent not in holders:
                holders.add(parent)
                stack.append(parent)
    return holders


class _Compiler:
    """Build the dumper of each type that can hold a Serializer."""

    def __init__(self, shapes: dict[Any, _Shape], holders: set[Any]) -> None:
        self.shapes = shapes
        self.holders = holders
        # The dumper of each type, by key. A recursive type reads its own cell.
        self.cells: dict[Any, list[Dumper | None]] = {}

    def compile(self, annotation: Any) -> Dumper | None:
        key = _key(_normalize(annotation))
        if key not in self.holders:
            return None
        cell = self.cells.get(key)
        if cell is None:
            cell = self.cells[key] = [None]
            cell[0] = self._build(self.shapes[key])
        if cell[0] is not None:
            return cell[0]
        # A recursive reference: the dumper of the type is not ready yet.
        return lambda value: cell[0](value)

    def _build(self, shape: _Shape) -> Dumper:
        if shape.kind == "serializer":
            return _dump_serializer
        if shape.kind == "items":
            return functools.partial(_dump_array, self.compile(shape.children[0]))
        if shape.kind == "fixed":
            return functools.partial(_dump_fixed_array, tuple(self.compile(item) for item in shape.children))
        if shape.kind == "mapping":
            return functools.partial(_dump_mapping, self.compile(shape.children[0]))
        if shape.kind == "union":
            options = tuple(dumper for item in shape.children if (dumper := self.compile(item)) is not None)
            return options[0] if len(options) == 1 else functools.partial(_dump_union, options)
        plan = tuple(
            (index, field.name, field.encode_name, dumper)
            for index, field in enumerate(shape.fields)
            if (dumper := self.compile(field.annotation)) is not None
        )
        if shape.kind == "struct":
            return functools.partial(_dump_struct, shape.cls, plan)
        if shape.kind == "dataclass":
            return functools.partial(_dump_dataclass, shape.cls, plan)
        return functools.partial(_dump_dict_fields, plan)


@functools.lru_cache(maxsize=256)
def _dumper_of_subclass(cls: type) -> Dumper:
    """Return the dumper of a subclass instance at the position of its base class.

    msgspec encodes the fields of the subclass, so the dumper follows them.
    """
    return compile_serializer_dumper(cls) or _keep


def _keep(value: Any) -> Any:
    return value


def _dump_serializer(value: Any) -> Any:
    if getattr(value.__class__, "__is_bolt_serializer__", False):
        return value.dump()
    return value


def _dump_array(item: Dumper, value: Any) -> Any:
    if isinstance(value, _ARRAY_VALUES):
        return [item(element) for element in value]
    return value


def _dump_fixed_array(items: tuple[Dumper | None, ...], value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == len(items):
        return [element if item is None else item(element) for item, element in zip(items, value, strict=True)]
    return value


def _dump_mapping(item: Dumper, value: Any) -> Any:
    if isinstance(value, dict):
        return {key: item(element) for key, element in value.items()}
    return value


def _dump_union(options: tuple[Dumper, ...], value: Any) -> Any:
    """Apply the first option that changes ``value``. Each option changes only a value of its own type."""
    for option in options:
        result = option(value)
        if result is not value:
            return result
    return value


def _dump_dict_fields(plan: tuple[tuple[int, str, str, Dumper], ...], value: Any) -> Any:
    """Dump the fields of a dict at the position of a Struct, a dataclass or a TypedDict.

    The keys of the dict are the wire names, or else the field names.
    """
    if not isinstance(value, dict):
        return value
    dumped = None
    for _, name, encode_name, dump in plan:
        key = encode_name if encode_name in value else name
        if key in value:
            element = value[key]
            new_element = dump(element)
            if new_element is not element:
                if dumped is None:
                    dumped = dict(value)
                dumped[key] = new_element
    return value if dumped is None else dumped


def _dump_struct(cls: type, plan: tuple[tuple[int, str, str, Dumper], ...], value: Any) -> Any:
    if not isinstance(value, cls):
        return _dump_dict_fields(plan, value)
    if value.__class__ is not cls:
        return _dumper_of_subclass(value.__class__)(value)
    # astuple reads the slots, as msgspec does.
    elements = msgspec.structs.astuple(value)
    clone = None
    for index, name, _, dump in plan:
        element = elements[index]
        new_element = dump(element)
        if new_element is not element:
            if clone is None:
                # The copy keeps the encoding of the type: rename, tag, omit_defaults, array_like.
                clone = msgspec.Struct.__copy__(value)
            msgspec.structs.force_setattr(clone, name, new_element)
    return value if clone is None else clone


def _dataclass_changes(plan: tuple[tuple[int, str, str, Dumper], ...], value: Any) -> list[tuple[str, Any]]:
    """Return (name, dumped value) for each field of a dataclass object that the dump changes."""
    changes = []
    for _, name, _, dump in plan:
        try:
            element = getattr(value, name)
        except AttributeError:
            # msgspec skips a field that the object does not hold.
            continue
        new_element = dump(element)
        if new_element is not element:
            changes.append((name, new_element))
    return changes


def _dump_dataclass(cls: type, plan: tuple[tuple[int, str, str, Dumper], ...], value: Any) -> Any:
    if not isinstance(value, cls):
        return _dump_dict_fields(plan, value)
    if value.__class__ is not cls:
        return _dumper_of_subclass(value.__class__)(value)
    changes = _dataclass_changes(plan, value)
    if not changes:
        return value
    clone = copy.copy(value)
    if clone is value:
        raise TypeError(f"{cls.__qualname__}.__copy__ returns the object itself, so Bolt cannot dump its Serializers.")
    for name, new_element in changes:
        # object.__setattr__ also sets a field of a frozen dataclass.
        object.__setattr__(clone, name, new_element)
    return clone
