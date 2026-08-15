"""Validation decorators for Serializer classes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast, get_type_hints, overload

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .base import Serializer

T = TypeVar("T")
ValidatorMode = Literal["before", "after"]


class _ComputedFieldCallable(Protocol):
    __name__: str
    __computed_field__: ComputedFieldConfig

    def __call__(self, instance: Any, /) -> Any: ...


class _FieldValidatorCallable(Protocol):
    __validator_field__: str
    __validator_mode__: ValidatorMode

    def __call__(self, serializer_cls: type[Serializer], value: Any, /) -> Any: ...


class _ModelValidatorCallable(Protocol):
    __model_validator__: bool
    __validator_mode__: ValidatorMode

    def __call__(self, serializer: Serializer, /) -> Serializer: ...


ComputedFieldMethod = Callable[[Any], Any]
FieldValidatorFunc = Callable[[type["Serializer"], Any], Any]
ModelValidatorFunc = Callable[["Serializer"], "Serializer"]

# Decorated symbols keep their own type. A validator is written as an ordinary
# method body, which a type checker infers as an instance method (`cls: Self`);
# requiring it to be assignable to FieldValidatorFunc made Pylance report
# `type[Serializer]` incompatible with `Self@<Model>` on every validator. The
# aliases above stay for the runtime protocols and for documentation.
DecoratedT = TypeVar("DecoratedT")


def _unwrap_descriptor(func: Any) -> Any:
    """The plain function behind a classmethod/staticmethod, if any.

    Validators are collected out of the class `__dict__`, where a
    `@classmethod` is still a descriptor object rather than the function.
    """
    return func.__func__ if isinstance(func, (classmethod, staticmethod)) else func


def _marked_validator(raw: Any, marker: str, *, allow_classmethod: bool) -> Any | None:
    """The validator function behind a class `__dict__` entry, or None.

    Decorators mark the underlying function, so this is the one place that
    decides which descriptor forms are legal — regardless of decorator order.
    A field validator is called as ``func(cls, value)``, so ``@classmethod``
    is accepted; ``@staticmethod`` has the wrong arity. A model validator
    receives the constructed instance, so no descriptor can express one.
    Rejecting loudly here matters: silently collecting nothing leaves a
    serializer that looks validated and is not.
    """
    func = _unwrap_descriptor(raw)
    if not (callable(func) and hasattr(func, marker)):
        return None
    if isinstance(raw, staticmethod) or (isinstance(raw, classmethod) and not allow_classmethod):
        kind = type(raw).__name__
        raise TypeError(f"{func.__qualname__}: a validator cannot be a {kind}. Drop the @{kind} decorator.")
    return func


# Marker attributes for storing validators on classes
FIELD_VALIDATORS_ATTR = "__field_validators__"
MODEL_VALIDATORS_ATTR = "__model_validators__"
COMPUTED_FIELDS_ATTR = "__computed_fields__"


@dataclass(frozen=True, slots=True)
class ComputedFieldConfig:
    """Configuration for a computed field."""

    method_name: str
    """Name of the method that computes the value."""

    return_type: Any
    """Return type of the computed field."""

    description: str | None = None
    """Description for OpenAPI documentation."""

    alias: str | None = None
    """Alternative name for this field in JSON output."""

    deprecated: bool = False
    """Mark this field as deprecated."""

    include_in_schema: bool = True
    """Whether to include this field in OpenAPI schema."""


def computed_field(
    func: ComputedFieldMethod | None = None,
    *,
    alias: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
    include_in_schema: bool = True,
) -> Any:
    """
    Mark a method as a computed field for serialization output.

    Computed fields are calculated during serialization (dump) and are NOT
    stored as struct fields. They are similar to DRF's SerializerMethodField
    or Pydantic's @computed_field.

    Args:
        func: The method to use for computing the value (if used without parentheses)
        alias: Alternative name for this field in JSON output.
        description: Description for OpenAPI documentation.
        deprecated: Mark this field as deprecated.
        include_in_schema: Whether to include this field in OpenAPI schema.

    Returns:
        The decorated method with computed field metadata.

    Example:
        class UserSerializer(Serializer):
            first_name: str
            last_name: str

            @computed_field
            def full_name(self) -> str:
                return f"{self.first_name} {self.last_name}"

            @computed_field(alias="displayName")
            def display_name(self) -> str:
                return self.full_name.upper()

        # When dumped:
        # {"first_name": "John", "last_name": "Doe", "full_name": "John Doe", "display_name": "JOHN DOE"}

    Note:
        - Computed fields are OUTPUT ONLY - they don't exist during parsing/loading
        - They are calculated fresh on each dump() call
        - They can access other struct fields and computed fields
        - Method name becomes the field name (unless alias is specified)
    """

    def decorator(method: ComputedFieldMethod) -> ComputedFieldMethod:
        # Try to get return type from method annotations
        return_type = Any
        try:
            hints = get_type_hints(method)
            return_type = hints.get("return", Any)
        except Exception as e:
            logger.debug(
                "Failed to get return type hints for computed field method %s. Using Any as return type. Error: %s",
                method.__name__ if hasattr(method, "__name__") else str(method),
                e,
            )

        # Store computed field metadata on the method
        typed_method = cast(_ComputedFieldCallable, method)
        typed_method.__computed_field__ = ComputedFieldConfig(
            method_name=typed_method.__name__,
            return_type=return_type,
            description=description,
            alias=alias,
            deprecated=deprecated,
            include_in_schema=include_in_schema,
        )
        return method

    if func is None:
        # Called with parentheses: @computed_field() or @computed_field(alias="...")
        return decorator
    else:
        # Called without parentheses: @computed_field
        return decorator(func)


def field_validator(
    field_name: str,
    mode: ValidatorMode = "after",
) -> Callable[[DecoratedT], DecoratedT]:
    """
    Decorator to validate a specific field in a Serializer.

    Args:
        field_name: Name of the field to validate
        mode: When to run the validator ('before' or 'after' other validators)

    Example:
        class UserCreate(Serializer):
            email: str

            @field_validator('email')
            @classmethod
            def validate_email(cls, value):
                if '@' not in value:
                    raise ValueError('Invalid email')
                return value.lower()
    """

    def decorator(func: DecoratedT) -> DecoratedT:
        # Mark the underlying function, not the descriptor: `@classmethod` is
        # the form type checkers accept, and attributes set on the classmethod
        # object are invisible once the descriptor is resolved.
        typed_func = cast(_FieldValidatorCallable, _unwrap_descriptor(func))
        typed_func.__validator_field__ = field_name
        typed_func.__validator_mode__ = mode
        return func

    return decorator


@overload
def model_validator(func: ModelValidatorFunc, mode: ValidatorMode = "after") -> ModelValidatorFunc: ...


@overload
def model_validator(
    func: None = None,
    mode: ValidatorMode = "after",
) -> Callable[[ModelValidatorFunc], ModelValidatorFunc]: ...


def model_validator(
    func: ModelValidatorFunc | None = None,
    mode: ValidatorMode = "after",
) -> ModelValidatorFunc | Callable[[ModelValidatorFunc], ModelValidatorFunc]:
    """
    Decorator to validate an entire Serializer after all fields are set.

    Args:
        func: The validator function (if used without parentheses)
        mode: When to run the validator ('before' or 'after' field validators)

    Example:
        class UserCreate(Serializer):
            password: str
            password_confirm: str

            @model_validator
            def validate_passwords(self):
                if self.password != self.password_confirm:
                    raise ValueError('Passwords must match')
    """

    def decorator(
        validator_func: ModelValidatorFunc,
    ) -> ModelValidatorFunc:
        # Mark the underlying function; the collector rejects descriptors.
        typed_validator = cast(_ModelValidatorCallable, _unwrap_descriptor(validator_func))
        typed_validator.__model_validator__ = True
        typed_validator.__validator_mode__ = mode
        return validator_func

    if func is None:
        # Called with parentheses: @model_validator()
        return decorator
    # Called without parentheses: @model_validator
    return decorator(func)


def collect_field_validators(cls: type[Serializer]) -> dict[str, list[FieldValidatorFunc]]:
    """
    Collect all field validators from a class and its bases.

    Returns a dict mapping field names to lists of validator functions.
    """
    validators: dict[str, list[FieldValidatorFunc]] = {}

    # Walk through MRO to collect validators
    for base in cls.__mro__:
        if not hasattr(base, "__dict__"):
            continue

        for raw in base.__dict__.values():
            value = _marked_validator(raw, "__validator_field__", allow_classmethod=True)
            if value is not None:
                validator = cast(_FieldValidatorCallable, value)
                field_name = validator.__validator_field__
                if field_name not in validators:
                    validators[field_name] = []
                validators[field_name].append(cast(FieldValidatorFunc, value))

    return validators


def collect_model_validators(cls: type[Serializer]) -> list[ModelValidatorFunc]:
    """
    Collect all model validators from a class and its bases.

    Returns a list of validator functions in MRO order.
    """
    validators: list[ModelValidatorFunc] = []

    # Walk through MRO to collect validators
    for base in cls.__mro__:
        if not hasattr(base, "__dict__"):
            continue

        for raw in base.__dict__.values():
            value = _marked_validator(raw, "__model_validator__", allow_classmethod=False)
            if value is not None:
                validators.append(cast(ModelValidatorFunc, value))

    return validators


def collect_computed_fields(cls: type[Serializer]) -> dict[str, ComputedFieldConfig]:
    """
    Collect all computed fields from a class and its bases.

    Returns a dict mapping field names to their ComputedFieldConfig.
    """
    computed: dict[str, ComputedFieldConfig] = {}

    # Walk through MRO to collect computed fields (reverse order so subclass overrides parent)
    for base in reversed(cls.__mro__):
        if not hasattr(base, "__dict__"):
            continue

        for _name, value in base.__dict__.items():
            if callable(value) and hasattr(value, "__computed_field__"):
                config = cast(_ComputedFieldCallable, value).__computed_field__
                # Use alias if provided, otherwise use method name
                field_name = config.alias or config.method_name
                computed[field_name] = config

    return computed
