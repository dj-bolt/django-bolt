"""Django field to msgspec type mapping utilities and field configuration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from django.db import models
from msgspec import Meta

T = TypeVar("T")

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
    """If True, field is only included in output (dump), not accepted in input (load)."""

    write_only: bool = False
    """If True, field is only accepted in input (load), not included in output (dump)."""

    source: str | None = None
    """
    Source attribute name on the model. Allows mapping API field names to different
    model attributes. Supports dot notation for nested access (e.g., "author.name").
    """

    alias: str | None = None
    """Alternative name for this field in JSON input/output."""

    default: Any = _UNSET
    """Default value for this field."""

    default_factory: Callable[[], Any] | None = None
    """Factory function to create default value."""

    description: str | None = None
    """Description for OpenAPI documentation."""

    title: str | None = None
    """Title for OpenAPI documentation."""

    examples: list[Any] | None = None
    """Example values for OpenAPI documentation."""

    deprecated: bool = False
    """Mark this field as deprecated in OpenAPI documentation."""

    exclude: bool = False
    """Always exclude this field from serialization."""

    include_in_schema: bool = True
    """Whether to include this field in OpenAPI schema."""

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


def field(
    *,
    read_only: bool = False,
    write_only: bool = False,
    source: str | None = None,
    alias: str | None = None,
    default: Any = _UNSET,
    default_factory: Callable[[], Any] | None = None,
    description: str | None = None,
    title: str | None = None,
    examples: list[Any] | None = None,
    deprecated: bool = False,
    exclude: bool = False,
    include_in_schema: bool = True,
) -> Any:
    """
    Configure a serializer field with additional metadata.

    This function returns a value that can be used as a field default in a
    Serializer class. The returned value contains both the default value
    (if any) and the field configuration metadata.

    For validation constraints (ge, gt, le, lt, min_length, max_length, pattern),
    use msgspec.Meta with Annotated types instead:

        from typing import Annotated
        from msgspec import Meta

        name: Annotated[str, Meta(min_length=1, max_length=100)]
        price: Annotated[float, Meta(ge=0.0)]

    Args:
        read_only: If True, field is only included in output, not accepted in input.
                   Use for auto-generated fields like `id`, `created_at`.
        write_only: If True, field is only accepted in input, not included in output.
                    Use for sensitive data like `password`.
        source: Source attribute name on the model. Supports dot notation.
                Example: source="author.name" maps field to instance.author.name
        alias: Alternative name for this field in JSON input/output.
        default: Default value for this field.
        default_factory: Factory function to create default value (for mutable defaults).
        description: Description for OpenAPI documentation.
        title: Title for OpenAPI documentation.
        examples: Example values for OpenAPI documentation.
        deprecated: Mark this field as deprecated.
        exclude: Always exclude this field from serialization.
        include_in_schema: Whether to include this field in OpenAPI schema.

    Returns:
        A field configuration that can be used as a default value.

    Example:
        class UserSerializer(Serializer):
            id: int = field(read_only=True)
            email: str = field(source="email_address")
            password: str = field(write_only=True)
            tags: list[str] = field(default_factory=list)

        # For constraints, use Annotated + Meta:
        class ProductSerializer(Serializer):
            name: Annotated[str, Meta(min_length=1, max_length=100)]
            price: Annotated[float, Meta(ge=0.0)]
    """
    config = FieldConfig(
        read_only=read_only,
        write_only=write_only,
        source=source,
        alias=alias,
        default=default,
        default_factory=default_factory,
        description=description,
        title=title,
        examples=examples,
        deprecated=deprecated,
        exclude=exclude,
        include_in_schema=include_in_schema,
    )

    return _FieldMarker(config=config)


def _build_literal_type(choice_values: list[Any]) -> Any:
    """Build a runtime Literal annotation from dynamic Django choices."""
    return Literal.__getitem__(tuple(choice_values))


def get_msgspec_type_for_django_field(field: models.Field) -> Any:
    """
    Convert a Django model field to a msgspec-compatible type annotation.

    Args:
        field: Django model field instance

    Returns:
        A type annotation suitable for use in a msgspec.Struct

    Example:
        >>> get_msgspec_type_for_django_field(models.CharField(max_length=150))
        Annotated[str, Meta(max_length=150)]
    """
    # Build constraint metadata
    constraints: dict[str, Any] = {}

    # CharField and similar text fields
    if isinstance(field, models.CharField):
        # Check if field has choices - use Literal type for type safety
        if field.choices:
            # Extract choice values (first element of each tuple)
            choice_values = [choice[0] for choice in field.choices]
            base_type = _build_literal_type(choice_values)
        else:
            constraints["max_length"] = field.max_length
            base_type = str

    elif isinstance(field, models.TextField):
        base_type = str

    elif isinstance(field, (models.EmailField, models.URLField, models.SlugField)):
        constraints["max_length"] = field.max_length
        base_type = str

    # Numeric fields
    elif isinstance(field, models.IntegerField):
        # Check if field has choices - use Literal type for type safety
        if field.choices:
            # Extract choice values (first element of each tuple)
            choice_values = [choice[0] for choice in field.choices]
            base_type = _build_literal_type(choice_values)
        else:
            base_type = int
            # Handle validators for ranges
            for validator in field.validators:
                if hasattr(validator, "limit_value"):
                    if hasattr(validator, "message") and "greater" in validator.message:
                        constraints["ge"] = validator.limit_value
                    elif hasattr(validator, "message") and "less" in validator.message:
                        constraints["le"] = validator.limit_value

    elif isinstance(field, models.FloatField):
        base_type = float

    elif isinstance(field, models.DecimalField):
        # Decimal is a complex type, but we can use float for JSON API
        base_type = float

    elif isinstance(field, models.BooleanField):
        base_type = bool

    # Date/Time fields
    elif isinstance(field, models.DateTimeField):
        base_type = datetime

    elif isinstance(field, models.DateField):
        base_type = date

    elif isinstance(field, models.TimeField):
        base_type = time

    elif isinstance(field, models.DurationField):
        # Duration serializes as string in ISO format
        base_type = str

    # UUID field
    elif isinstance(field, models.UUIDField):
        base_type = UUID

    # ForeignKey and relationships (simplified)
    elif isinstance(field, models.ForeignKey):
        # For now, just use int (the primary key)
        # Can be enhanced with nested serializers later
        base_type = int

    elif isinstance(field, models.OneToOneField):
        base_type = int

    # ManyToManyField
    elif isinstance(field, models.ManyToManyField):
        # Use list of ints (primary keys)
        # Can be enhanced with nested serializers later
        base_type = list[int]

    else:
        # Fallback for unknown field types
        base_type = Any

    # Handle nullable fields
    if field.null and not isinstance(field, models.BooleanField):
        base_type = base_type | None

    # Apply constraints if any
    if constraints:
        return Annotated[base_type, Meta(**constraints)]

    return base_type
