"""Support for nested serializers with type-driven inference."""

from __future__ import annotations

from dataclasses import dataclass
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Union, cast, get_args, get_origin

if TYPE_CHECKING:
    from .base import Serializer


@dataclass(frozen=True, slots=True)
class ResolvedNestedConfig:
    """Fully resolved nested field configuration derived from the type hint."""

    serializer_class: type[Serializer]
    many: bool


def _is_serializer_type(field_type: Any) -> bool:
    """Return True if the type is a django-bolt Serializer subclass."""
    # Import locally to avoid a circular import with base.py
    # ruff: noqa: PLC0415
    from .base import Serializer as BaseSerializer

    try:
        return isinstance(field_type, type) and issubclass(field_type, BaseSerializer)
    except TypeError:
        return False


def _unwrap_nested_type(field_type: Any) -> Any:
    """Strip Annotated and Optional wrappers when inferring nested serializer types."""
    while True:
        origin = get_origin(field_type)
        if origin is Annotated:
            field_type = get_args(field_type)[0]
            continue
        if origin in (Union, UnionType):
            non_none = [arg for arg in get_args(field_type) if arg is not type(None)]
            if len(non_none) == 1:
                field_type = non_none[0]
                continue
        return field_type


def resolve_nested_config(field_type: Any) -> ResolvedNestedConfig | None:
    """Infer nested serializer behavior from a field type."""
    resolved_type = _unwrap_nested_type(field_type)

    if _is_serializer_type(resolved_type):
        return ResolvedNestedConfig(
            serializer_class=resolved_type,
            many=False,
        )

    origin = get_origin(resolved_type)
    if origin is list:
        args = get_args(resolved_type)
        if args:
            item_type = _unwrap_nested_type(args[0])
            if _is_serializer_type(item_type):
                return ResolvedNestedConfig(
                    serializer_class=item_type,
                    many=True,
                )

    return None


def validate_nested_field(
    value: Any,
    nested_config: ResolvedNestedConfig,
    field_name: str,
) -> Any:
    """Validate and convert an inferred nested field value."""
    if value is None:
        return None

    serializer_class = nested_config.serializer_class

    if nested_config.many:
        return _validate_many_nested(value, serializer_class, field_name)
    return _validate_single_nested(value, serializer_class, field_name)


def _validate_single_nested(
    value: Any,
    serializer_class: type[Serializer],
    field_name: str,
) -> Any:
    """Validate a single nested object inferred from the field type."""
    if isinstance(value, dict):
        try:
            return serializer_class(**value)
        except Exception as e:
            raise ValueError(f"{field_name}: Failed to validate nested {serializer_class.__name__}: {e}") from e

    if isinstance(value, serializer_class):
        return value

    raise ValueError(
        f"{field_name}: Expected {serializer_class.__name__} object or dict, "
        f"got {type(value).__name__}. "
        f"Use a separate serializer with plain 'int' type for ID-only fields."
    )


def _validate_many_nested(
    value: Any,
    serializer_class: type[Serializer],
    field_name: str,
) -> Any:
    """Validate a list of nested objects inferred from the field type."""
    if not isinstance(value, list):
        raise ValueError(f"{field_name}: Expected list for nested relationship, got {type(value).__name__}")

    result = []
    for idx, item in enumerate(value):
        if isinstance(item, dict):
            try:
                result.append(serializer_class(**cast(dict[str, Any], item)))
            except Exception as e:
                raise ValueError(
                    f"{field_name}[{idx}]: Failed to validate nested {serializer_class.__name__}: {e}"
                ) from e
        elif isinstance(item, serializer_class):
            result.append(item)
        else:
            raise ValueError(
                f"{field_name}[{idx}]: Expected {serializer_class.__name__} object or dict, "
                f"got {type(item).__name__}. "
                f"Use a separate serializer with 'list[int]' type for ID-only fields."
            )

    return result


def is_nested_field(metadata: Any) -> bool:
    """Check if a field type resolves to a nested serializer field."""
    return resolve_nested_config(metadata) is not None
