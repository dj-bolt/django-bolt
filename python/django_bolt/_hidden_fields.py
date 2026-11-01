"""Remove the fields that ``dump()`` never sends from a dict body.

Bolt does not convert a dict body to its Serializer type. The dict goes out as
the handler gives it, but without the hidden keys. A hidden key is the name,
the wire name or the alias of a write-only or excluded field.

The plan follows the annotation of each field. Thus a Serializer type in a
list, a tuple, a set, a dict value, a union, or a field of a plain Struct, a
dataclass or a TypedDict also loses its hidden keys. A value that is not a
dict or a list stays as it is. The response encoder dumps each Serializer
instance.

Bolt makes the plan of a class once, at its first use, and keeps it.
"""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import sys
import types
import typing
from collections.abc import Callable
from typing import Annotated, Any, ForwardRef, Literal, Union, get_args, get_origin

import msgspec

Stripper = Callable[[Any], Any]

_MISSING = object()

# The maximum number of classes in each cache. When a cache is full, its
# oldest class goes: a dict keeps the order of insertion. Thus the caches keep
# a bounded number of classes alive when an app makes classes at runtime.
_CACHE_LIMIT = 512


def _remember(cache: dict[Any, Any], key: Any, value: Any) -> None:
    """Keep ``value`` in ``cache``. When the cache is full, its oldest entry goes."""
    if len(cache) >= _CACHE_LIMIT:
        try:
            del cache[next(iter(cache))]
        except (KeyError, RuntimeError, StopIteration):
            # Another thread changed the cache. It is a cache, so the entry stays out.
            return
    cache[key] = value


# The plan of each class: a function that removes the hidden keys, or None
# when the class has no hidden key at any position.
_STRIPPERS: dict[type, Stripper | None] = {}
# The projection of a dict row onto the fields of each Serializer.
_ROW_PROJECTORS: dict[type, Callable[[list[Any]], list[dict[Any, Any]]]] = {}

# The origins of the annotations whose values have one item type.
_SEQUENCE_ORIGINS = frozenset(
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
_UNION_ORIGINS = frozenset({Union, types.UnionType})
# The origins that only add data to the type of their first argument.
_WRAPPER_ORIGINS = frozenset(
    origin
    for origin in (Annotated, typing.Required, typing.NotRequired, getattr(typing, "ReadOnly", None))
    if origin is not None
)


def without_hidden_fields(serializer_cls: Any, value: Any) -> Any:
    """Return ``value`` without the hidden keys of ``serializer_cls`` at each Serializer position.

    The function does not change ``value``. It gives a new dict or list for
    each level that it goes into.
    """
    strip = _STRIPPERS.get(serializer_cls, _MISSING)
    if strip is _MISSING:
        strip = _plan(serializer_cls)
    return value if strip is None else strip(value)


def project_dict_rows(serializer_cls: Any, rows: list[Any]) -> list[dict[Any, Any]]:
    """Project each dict row onto the fields of ``serializer_cls`` that ``dump()`` sends.

    A row keeps the Python names of the fields, as a ``values()`` row has them.
    A field that the row does not have is None. A nested value loses its
    hidden keys, as in ``without_hidden_fields``.
    """
    project = _ROW_PROJECTORS.get(serializer_cls)
    if project is None:
        project = _build_row_projector(serializer_cls)
        _remember(_ROW_PROJECTORS, serializer_cls, project)
    return project(rows)


def _plan(cls: type) -> Stripper | None:
    building: dict[Any, Stripper | None] = {}
    stripper = _class_stripper(cls, building)
    _publish(building)
    return stripper


def _publish(building: dict[Any, Stripper | None]) -> None:
    """Keep the plan of each class that a complete build made."""
    for key, stripper in building.items():
        if isinstance(key, type):
            _remember(_STRIPPERS, key, stripper)


def _build_row_projector(cls: Any) -> Callable[[list[Any]], list[dict[Any, Any]]]:
    building: dict[Any, Stripper | None] = {}
    _, fields = _class_fields(cls) or (frozenset(), [])
    names = tuple(name for name, _, _ in fields)
    parts = tuple(
        (name, part)
        for name, _, annotation in fields
        if (part := _annotation_stripper(annotation, cls, building)) is not None
    )
    _publish(building)

    if not parts:

        def project(rows: list[Any]) -> list[dict[Any, Any]]:
            return [{name: row.get(name) for name in names} for row in rows]

        return project

    def project_nested(rows: list[Any]) -> list[dict[Any, Any]]:
        result = []
        for row in rows:
            item = {name: row.get(name) for name in names}
            for name, part in parts:
                item[name] = part(item[name])
            result.append(item)
        return result

    return project_nested


def _guarded(key: Any, building: dict[Any, Stripper | None], build: Callable[[], Stripper | None]) -> Stripper | None:
    """Build the plan of ``key`` once, also when ``key`` refers to itself.

    During the build, a reference to ``key`` gets a function that calls the
    plan when the build is complete. Thus a self reference does not recurse
    without end.
    """
    stripper = building.get(key, _MISSING)
    if stripper is not _MISSING:
        return stripper
    cell: list[Stripper] = []

    def forward(value: Any) -> Any:
        return cell[0](value)

    building[key] = forward
    stripper = build()
    cell.append(_keep if stripper is None else stripper)
    building[key] = stripper
    return stripper


def _class_stripper(cls: type, building: dict[Any, Stripper | None]) -> Stripper | None:
    stripper = _STRIPPERS.get(cls, _MISSING)
    if stripper is not _MISSING:
        return stripper

    def build() -> Stripper | None:
        plan = _class_fields(cls)
        if plan is None:
            return None
        hidden_keys, fields = plan
        nested: dict[Any, Stripper] = {}
        for _, keys, annotation in fields:
            part = _annotation_stripper(annotation, cls, building)
            if part is not None:
                for key in keys:
                    nested[key] = part
        return _fields_stripper(hidden_keys, nested)

    return _guarded(cls, building, build)


def _class_fields(cls: Any) -> tuple[frozenset[Any], list[tuple[str, tuple[str, ...], Any]]] | None:
    """Return the hidden keys and the visible fields of ``cls``, or None.

    Each field is ``(name, keys, annotation)``. ``keys`` are the keys that a
    dict can use for the field. None means that ``cls`` has no fields to plan.
    """
    if getattr(cls, "__is_bolt_serializer__", False):
        cls._ensure_dump_ready()
        hidden = cls.__hidden_fields__
        rename_map = cls.__rename_map__
        configs = cls.__field_configs__
        hints = _field_hints(cls)
        hidden_keys: set[str] = set()
        fields = []
        for name in cls.__struct_fields__:
            config = configs.get(name)
            # A field can have three keys: its name, its wire name and its alias.
            keys = dict.fromkeys((name, rename_map.get(name, name), config.alias if config is not None else None))
            keys.pop(None, None)
            if name in hidden:
                hidden_keys.update(keys)
            else:
                fields.append((name, tuple(keys), hints.get(name)))
        return frozenset(hidden_keys), fields
    if issubclass(cls, msgspec.Struct):
        hints = _field_hints(cls)
        return frozenset(), [
            (name, (name,) if encode_name == name else (name, encode_name), hints.get(name))
            for name, encode_name in zip(cls.__struct_fields__, cls.__struct_encode_fields__, strict=True)
        ]
    if dataclasses.is_dataclass(cls):
        hints = _field_hints(cls)
        return frozenset(), [(item.name, (item.name,), hints.get(item.name)) for item in dataclasses.fields(cls)]
    if typing.is_typeddict(cls):
        return frozenset(), [(name, (name,), hint) for name, hint in _field_hints(cls).items()]
    return None


def _field_hints(cls: Any) -> dict[str, Any]:
    """Return the annotation of each field of ``cls``, with the forward references resolved.

    A Serializer keeps the hints that it resolved when Python made the class.
    These hints can name a local class, so they stay. A hint can still hold a
    forward reference, for example to the class itself. Such a hint resolves
    now, with the globals of the module and the name of the class.
    """
    cached = getattr(cls, "__cached_type_hints__", None)
    if cached is not None and not any(_holds_forward_ref(hint) for hint in cached.values()):
        return cached
    for localns in (None, {cls.__name__: cls}):
        try:
            hints = typing.get_type_hints(cls, localns=localns)
        except Exception:
            # An annotation is code, so get_type_hints can raise any error. Try the next namespace.
            continue
        if cached is None:
            return hints
        return {**hints, **{name: hint for name, hint in cached.items() if not _holds_forward_ref(hint)}}
    # One annotation does not resolve. Resolve each annotation alone, so that the other fields still get a plan.
    raw = cached if cached is not None else _raw_annotations(cls)
    return {name: _resolve_annotation(hint, cls) for name, hint in raw.items()}


def _raw_annotations(cls: type) -> dict[str, Any]:
    """Return the annotations that ``cls`` and its bases keep in their namespaces."""
    annotations: dict[str, Any] = {}
    for base in reversed(cls.__mro__):
        annotations.update(base.__dict__.get("__annotations__", {}))
    return annotations


def _resolve_annotation(annotation: Any, owner: type) -> Any:
    """Return ``annotation`` with its forward references resolved, or as it is when they do not resolve."""
    if not _holds_forward_ref(annotation):
        return annotation
    module = sys.modules.get(owner.__module__)

    def holder() -> None:
        """Hold the annotation, because get_type_hints reads the annotations of an object."""

    holder.__annotations__ = {"value": annotation}
    try:
        return typing.get_type_hints(
            holder, globalns=vars(module) if module is not None else {}, localns={owner.__name__: owner}
        )["value"]
    except Exception:
        # An annotation is code, so get_type_hints can raise any error. The walk leaves the field as it is.
        return annotation


def _holds_forward_ref(annotation: Any) -> bool:
    if isinstance(annotation, (str, ForwardRef)):
        return True
    origin = get_origin(annotation)
    if origin is Literal:
        return False
    args = get_args(annotation)
    if origin is Annotated:
        args = args[:1]
    return any(_holds_forward_ref(arg) for arg in args)


def _annotation_stripper(annotation: Any, owner: type, building: dict[Any, Stripper | None]) -> Stripper | None:
    """Return the plan for a value of the type ``annotation``, or None when no key is hidden in it.

    An annotation that the plan does not know gets None, so its value stays as it is.
    A forward reference that did not resolve is such an annotation.
    """
    origin = get_origin(annotation)
    if origin in _WRAPPER_ORIGINS:
        return _annotation_stripper(get_args(annotation)[0], owner, building)
    if origin in _UNION_ORIGINS:
        return _chain([_annotation_stripper(arg, owner, building) for arg in get_args(annotation)])
    if origin is tuple:
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return _sequence_stripper(_annotation_stripper(args[0], owner, building))
        return _positions_stripper([_annotation_stripper(arg, owner, building) for arg in args])
    if origin in _SEQUENCE_ORIGINS:
        args = get_args(annotation)
        return _sequence_stripper(_annotation_stripper(args[0], owner, building)) if args else None
    if origin in _MAPPING_ORIGINS:
        args = get_args(annotation)
        return _mapping_stripper(_annotation_stripper(args[1], owner, building)) if len(args) == 2 else None
    if isinstance(annotation, typing.TypeAliasType):
        alias = annotation
        return _guarded(alias, building, lambda: _annotation_stripper(alias.__value__, owner, building))
    if isinstance(annotation, typing.NewType):
        return _annotation_stripper(annotation.__supertype__, owner, building)
    if isinstance(annotation, type):
        return _class_stripper(annotation, building)
    return None


def _keep(value: Any) -> Any:
    return value


def _fields_stripper(hidden_keys: frozenset[Any], nested: dict[Any, Stripper]) -> Stripper | None:
    if nested:

        def strip_fields(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            result = {}
            for key, item in value.items():
                if key in hidden_keys:
                    continue
                part = nested.get(key)
                result[key] = item if part is None else part(item)
            return result

        return strip_fields

    if hidden_keys:

        def strip_keys(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            return {key: item for key, item in value.items() if key not in hidden_keys}

        return strip_keys

    return None


def _sequence_stripper(item: Stripper | None) -> Stripper | None:
    if item is None:
        return None

    def strip_items(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [item(element) for element in value]
        return value

    return strip_items


def _positions_stripper(parts: list[Stripper | None]) -> Stripper | None:
    """Return the plan of a tuple with a type for each position."""
    indexed = tuple((index, part) for index, part in enumerate(parts) if part is not None)
    if not indexed:
        return None

    def strip_positions(value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            return value
        result = list(value)
        size = len(result)
        for index, part in indexed:
            if index < size:
                result[index] = part(result[index])
        return result

    return strip_positions


def _mapping_stripper(item: Stripper | None) -> Stripper | None:
    if item is None:
        return None

    def strip_values(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: item(element) for key, element in value.items()}
        return value

    return strip_values


def _chain(parts: list[Stripper | None]) -> Stripper | None:
    """Return the plan of a union: each member removes its hidden keys in turn."""
    members = tuple(part for part in parts if part is not None)
    if not members:
        return None
    if len(members) == 1:
        return members[0]

    def strip_members(value: Any) -> Any:
        for member in members:
            value = member(value)
        return value

    return strip_members
