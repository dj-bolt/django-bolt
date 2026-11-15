"""Helper functions for creating Serializer classes from Django models."""

from __future__ import annotations

from typing import Any, TypeVar, cast

from django.db import models

from .base import Serializer
from .fields import get_msgspec_type_for_django_field

T = TypeVar("T", bound=Serializer)


def create_serializer(
    model: type[models.Model],
    *,
    fields: list[str] | None = None,
    exclude: list[str] | None = None,
    write_only: set[str] | None = None,
    read_only: set[str] | None = None,
    serializer_name: str | None = None,
) -> type[Serializer]:
    """
    Dynamically create a Serializer class from a Django model.

    This generates an explicit Serializer class with field annotations,
    which provides full type safety for IDE and type checkers.

    Args:
        model: Django model class
        fields: List of field names to include. If None, includes all.
        exclude: List of field names to exclude
        write_only: Set of field names that are input-only
        read_only: Set of field names that are output-only
        serializer_name: Name for the generated class (default: {Model}Serializer)

    Returns:
        A dynamically created Serializer subclass

    Example:
        UserSerializer = create_serializer(
            User,
            fields=['id', 'username', 'email', 'created_at'],
            read_only={'id', 'created_at'},
        )
    """
    write_only = write_only or set()
    read_only = read_only or set()

    # Get all model fields
    model_meta = cast(Any, model)._meta
    model_fields = {
        field.name: field
        for field in model_meta.get_fields()
        if isinstance(field, models.Field) and field.name is not None
    }

    # Keep the order of the `fields` argument, or the order of the model fields.
    # The field order is the order of the JSON keys and of the schema.
    field_names = list(fields) if fields is not None else list(model_fields)
    excluded = set(exclude) if exclude else set()

    # Build field annotations
    annotations: dict[str, Any] = {}
    for field_name in field_names:
        if field_name in excluded or field_name not in model_fields:
            continue

        field = model_fields[field_name]

        # Skip many-to-many for now (can be added later with nested serializers)
        if isinstance(field, models.ManyToManyField):
            continue

        # Get the msgspec type
        field_type = get_msgspec_type_for_django_field(field)
        annotations[field_name] = field_type

    # Create the class name
    class_name = serializer_name or f"{model.__name__}Serializer"

    # Create the class
    attrs: dict[str, Any] = {
        "__annotations__": annotations,
        "__doc__": f"Auto-generated serializer for {model.__name__}",
        "__module__": model.__module__,
    }

    # Add Config class with model reference
    config_attrs = {
        "model": model,
        "write_only": write_only,
        "read_only": read_only,
    }
    attrs["Config"] = type("Config", (), config_attrs)

    # Create and return the Serializer class
    serializer_class = type(class_name, (Serializer,), attrs)

    return serializer_class


def create_serializer_set(
    model: type[models.Model],
    *,
    create_fields: list[str] | None = None,
    update_fields: list[str] | None = None,
    public_fields: list[str] | None = None,
    write_only: set[str] | None = None,
    read_only: set[str] | None = None,
) -> tuple[type[Serializer], type[Serializer], type[Serializer]]:
    """
    Create a standard set of three Serializer classes for CRUD operations.

    Generates:
    - Create: Accepts input for creating new instances
    - Update: Accepts input for updating instances. Each field of `update_fields` is required
    - Public: Returns public representation

    Args:
        model: Django model class
        create_fields: Fields to include in Create serializer
        update_fields: Fields to include in Update serializer
        public_fields: Fields to include in Public serializer
        write_only: Additional fields that are input-only
        read_only: Additional fields that are output-only

    Returns:
        Tuple of (CreateSerializer, UpdateSerializer, PublicSerializer)

    Example:
        UserCreate, UserUpdate, UserPublic = create_serializer_set(
            User,
            create_fields=['username', 'email', 'password'],
            update_fields=['username', 'email'],
            public_fields=['id', 'username', 'email', 'created_at'],
        )

        # Now use in API:
        @api.post("/users", response_model=UserPublic)
        async def create_user(data: UserCreate):
            user = await User.objects.acreate(**data.to_dict())
            return UserPublic.from_model(user)
    """
    write_only = write_only or set()
    read_only = read_only or set()

    # Create serializers (use different variable names to avoid shadowing the function)
    create_class = create_serializer(
        model,
        fields=create_fields,
        write_only=write_only,
        serializer_name=f"{model.__name__}Create",
    )

    update_class = create_serializer(
        model,
        fields=update_fields,
        write_only=write_only,
        serializer_name=f"{model.__name__}Update",
    )

    public_class = create_serializer(
        model,
        fields=public_fields,
        read_only=read_only,
        serializer_name=f"{model.__name__}Public",
    )

    return create_class, update_class, public_class
