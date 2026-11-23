"""Tool input-schema generation from Python signatures / FieldDefinitions.

Builds a ``msgspec.Struct`` from a tool's parameters and emits a top-level
``type: "object"`` JSON Schema via ``msgspec.json.schema()`` (the MCP ``inputSchema``).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import msgspec

from ._execute import ToolResult

INJECTED_PARAMS = frozenset({"request", "req"})
_AUTOEXPOSE_SOURCES = frozenset({"path", "query", "body", "header", "cookie"})


def _signature_fields(fn: Callable, exclude: frozenset[str]) -> list[tuple]:
    # Keyword-only params let a required field follow a defaulted one (e.g.
    # `def f(a=1, *, b)`); defstruct rejects that ordering, so split as
    # struct_from_fields does — required first, optional after, order preserved.
    hints = get_type_hints(fn, include_extras=True)
    required: list[tuple] = []
    optional: list[tuple] = []
    for p in inspect.signature(fn).parameters.values():
        if p.name in exclude or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        annotation = hints.get(p.name, Any)
        if p.default is inspect.Parameter.empty:
            required.append((p.name, annotation))
        else:
            optional.append((p.name, annotation, p.default))
    return required + optional


def struct_from_signature(fn: Callable, *, exclude: frozenset[str] = INJECTED_PARAMS) -> type[msgspec.Struct]:
    """Build a msgspec.Struct mirroring ``fn``'s call parameters (minus injected ones)."""
    name = getattr(fn, "__name__", "tool")
    return msgspec.defstruct(f"{name}_Args", _signature_fields(fn, exclude))


def struct_from_fields(name: str, fields: list[Any]) -> type[msgspec.Struct]:
    """Build a msgspec.Struct from django-bolt FieldDefinitions (auto-expose path).

    Keeps path/query/body/header/cookie params; required fields first (defstruct
    requires non-default fields to precede defaulted ones).
    """
    required: list[tuple] = []
    optional: list[tuple] = []
    for f in fields:
        if f.source not in _AUTOEXPOSE_SOURCES:
            continue
        if f.is_required:
            required.append((f.field_alias, f.annotation))
        else:
            default = None if f.default is inspect.Parameter.empty else f.default
            optional.append((f.field_alias, f.annotation, default))
    return msgspec.defstruct(f"{name}_Args", required + optional)


def _point_at_root(value: Any, root_ref: str) -> Any:
    """Return ``value`` with each ``$ref`` to ``root_ref`` replaced by ``#``, the root of the document."""
    if isinstance(value, dict):
        return {
            key: "#" if key == "$ref" and item == root_ref else _point_at_root(item, root_ref)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_point_at_root(item, root_ref) for item in value]
    return value


def _inline_root_ref(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve a top-level ``$ref`` while retaining definitions it depends on.

    The root definition moves to the top of the document. Thus a reference to it from
    inside, as in a recursive Struct, becomes ``#``. Else it points at a removed definition.
    """
    if "$ref" not in raw:
        return raw
    defs = raw.get("$defs") or {}
    root_ref = raw["$ref"]
    ref = root_ref.rsplit("/", 1)[-1]
    schema = _point_at_root(dict(defs.get(ref, {})), root_ref)
    others = {key: _point_at_root(value, root_ref) for key, value in defs.items() if key != ref}
    if others:
        schema["$defs"] = others
    return schema


def output_schema_from_return(fn: Callable) -> dict[str, Any] | None:
    """Derive an MCP ``outputSchema`` from ``fn``'s return annotation (opt-in).

    Only object-shaped returns get a schema: MCP ``structuredContent`` is an
    object, so scalar/str annotations yield ``None`` rather than a misleading
    schema. Raises for annotations msgspec cannot represent.
    """
    hints = get_type_hints(fn, include_extras=True)
    annotation = hints.get("return")
    if annotation is None or annotation is type(None):
        return None
    # The schema of ToolResult describes its own fields, not the structured content.
    if annotation is ToolResult or (
        get_origin(annotation) in (Union, UnionType) and ToolResult in get_args(annotation)
    ):
        raise TypeError(
            f"{getattr(fn, '__name__', fn)!r} returns ToolResult, so its output schema cannot be derived. "
            "Pass output_schema as a JSON Schema dict for the structured content, or leave it out."
        )
    return object_schema(annotation)


def object_schema(annotation: Any) -> dict[str, Any] | None:
    """Return the JSON Schema of ``annotation`` when it describes an object, else None."""
    schema = _inline_root_ref(msgspec.json.schema(annotation))
    if schema.get("type") != "object":
        return None
    schema.pop("title", None)
    return schema


def input_schema_for(struct_type: type[msgspec.Struct]) -> dict[str, Any]:
    """Return a top-level ``type:"object"`` JSON Schema for an args struct.

    ``msgspec.json.schema`` returns ``{"$ref": "#/$defs/Name", "$defs": {...}}``;
    MCP requires a top-level object schema, so resolve the root ``$ref`` and inline
    it, carrying along any remaining ``$defs`` for nested structs to reference.
    """
    schema = _inline_root_ref(msgspec.json.schema(struct_type))
    schema.pop("title", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema
