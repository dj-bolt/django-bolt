"""Tool input-schema generation from Python signatures / FieldDefinitions.

Builds a ``msgspec.Struct`` from a tool's parameters and emits a top-level
``type: "object"`` JSON Schema via ``msgspec.json.schema()`` (the MCP ``inputSchema``).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

import msgspec

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


def input_schema_for(struct_type: type[msgspec.Struct]) -> dict[str, Any]:
    """Return a top-level ``type:"object"`` JSON Schema for an args struct.

    ``msgspec.json.schema`` returns ``{"$ref": "#/$defs/Name", "$defs": {...}}``;
    MCP requires a top-level object schema, so resolve the root ``$ref`` and inline
    it, carrying along any remaining ``$defs`` for nested structs to reference.
    """
    raw = msgspec.json.schema(struct_type)
    defs = raw.get("$defs") or {}
    schema = raw
    if "$ref" in raw:
        ref = raw["$ref"].rsplit("/", 1)[-1]
        schema = dict(defs.get(ref, {}))
        others = {k: v for k, v in defs.items() if k != ref}
        if others:
            schema["$defs"] = others
    schema.pop("title", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema
