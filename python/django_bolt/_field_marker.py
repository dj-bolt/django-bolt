"""The configuration of a serializer field and its marker.

This module has no import from the serializers package. So the request
pipeline can import it before that package.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Sentinel for unset default values
_UNSET = object()


@dataclass(frozen=True, slots=True)
class FieldConfig:
    """
    Configuration for a serializer field.

    This is used internally to store field metadata. Users should use the
    field() function to create field configurations.
    """

    read_only: bool = False
    """
    If True, input cannot set the field. Input is a JSON request body, a
    Query(), Header(), Cookie() or Form() model, model_validate() and
    model_validate_json(). The field gets its default, so
    it must have one. Python code and from_model() set the field.
    """

    write_only: bool = False
    """If True, field is only accepted in input (load), not included in output (dump)."""

    source: str | None = None
    """
    Source attribute name on the model. Allows mapping API field names to different
    model attributes. Supports dot notation for nested access (e.g., "author.name").
    """

    alias: str | None = None
    """
    Name of this field in JSON, for input and for output. The field name is then
    not a key of the JSON object. The alias wins over the rename option of the class.
    """

    default: Any = _UNSET
    """Default value for this field."""

    default_factory: Callable[[], Any] | None = None
    """Factory function to create default value."""

    description: str | None = None
    """The ``description`` of the property in the OpenAPI schema."""

    title: str | None = None
    """The ``title`` of the property in the OpenAPI schema."""

    examples: list[Any] | None = None
    """The ``examples`` list of the property in the OpenAPI schema."""

    deprecated: bool = False
    """If True, the property has ``deprecated: true`` in the OpenAPI schema."""

    exclude: bool = False
    """Always exclude this field from serialization."""

    include_in_schema: bool = True
    """
    If False, the OpenAPI schema has no property for this field, also not in
    ``required``. The field still validates input and is still in the output.
    """

    def has_default(self) -> bool:
        """Check if this field has a default value."""
        return self.default is not _UNSET or self.default_factory is not None

    def get_default(self) -> Any:
        """Get the default value for this field."""
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is not _UNSET:
            return self.default
        raise ValueError("Field has no default value")


@dataclass(slots=True)
class _FieldMarker:
    """
    Internal marker class that holds field configuration.

    This is used during class creation to extract field metadata.
    The marker is replaced with the actual default value (if any)
    during __init_subclass__.
    """

    config: FieldConfig

    def __repr__(self) -> str:
        parts = []
        if self.config.read_only:
            parts.append("read_only=True")
        if self.config.write_only:
            parts.append("write_only=True")
        if self.config.source:
            parts.append(f"source={self.config.source!r}")
        if self.config.alias:
            parts.append(f"alias={self.config.alias!r}")
        if self.config.has_default():
            parts.append(f"default={self.config.get_default()!r}")
        return f"field({', '.join(parts)})"
