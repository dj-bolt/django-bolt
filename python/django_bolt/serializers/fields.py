"""Django field to msgspec type mapping utilities and field configuration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from django.db import models
from msgspec import Meta

from .._field_marker import _UNSET, FieldConfig, _FieldMarker

T = TypeVar("T")


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
        read_only: If True, input cannot set the field. Bolt ignores the input value,
                   and the field gets its default. Give the field a default.
                   With forbid_unknown_fields=True, the input value is an error.
                   Python code and from_model() set the field. The OpenAPI request
                   body schema has no such field. The response schema marks it readOnly.
                   Use for auto-generated fields like `id`, `created_at`.
        write_only: If True, field is only accepted in input, not included in output.
                    Use for sensitive data like `password`.
        source: Source attribute name on the model. Supports dot notation.
                Example: source="author.name" maps field to instance.author.name
        alias: Name of this field in JSON, for input and for output. The input does
               not accept the field name then. The alias wins over the rename
               option of the class. Python code still uses the field name.
        default: Default value for this field.
        default_factory: Factory function to create default value (for mutable defaults).
        description: The description of the property in the OpenAPI schema.
        title: The title of the property in the OpenAPI schema.
        examples: A list of example values for the property in the OpenAPI schema.
        deprecated: If True, the OpenAPI schema marks the property as deprecated.
        exclude: Always exclude this field from serialization.
        include_in_schema: If False, the OpenAPI schema has no property for this field.
                           The field still validates input and is still in the output.

    Returns:
        A field configuration that can be used as a default value.

    Example:
        class UserSerializer(Serializer):
            id: int = field(read_only=True, default=0)
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

    # CharField and its subclasses (EmailField, URLField, SlugField)
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
        # A float loses digits. JSON sends a Decimal as a string, as Django REST Framework does.
        base_type = Decimal

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

    # ForeignKey and OneToOneField (a subclass of ForeignKey): the primary key
    # of the related row. from_model() reads it from `<name>_id`, and to_model()
    # and update_instance() write it there.
    elif isinstance(field, models.ForeignKey):
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
    if field.null:
        base_type = base_type | None

    # Apply constraints if any
    if constraints:
        return Annotated[base_type, Meta(**constraints)]

    return base_type
