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
_VALIDATOR_MODES = ("before", "after")


def _check_validator_mode(decorator: str, mode: Any) -> None:
    """Reject a mode that Bolt does not run. The error stops the creation of the class."""
    if mode not in _VALIDATOR_MODES:
        raise ValueError(f"@{decorator}: mode={mode!r} is not supported. Use mode='before' or mode='after'.")


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
    is accepted; ``@staticmethod`` has the wrong arity. A model validator with
    ``mode="after"`` receives the constructed instance, so no descriptor can
    express one. A model validator with ``mode="before"`` is called as
    ``func(cls, data)``, so ``@classmethod`` is accepted.
    Rejecting loudly here matters: silently collecting nothing leaves a
    serializer that looks validated and is not.
    """
    func = _unwrap_descriptor(raw)
    if not (callable(func) and hasattr(func, marker)):
        return None
    if func.__validator_mode__ == "before":
        allow_classmethod = True
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
    """The ``description`` of the property in the OpenAPI schema. The default is the docstring of the method."""

    alias: str | None = None
    """Alternative name for this field in JSON output."""

    deprecated: bool = False
    """If True, the property has ``deprecated: true`` in the OpenAPI schema."""

    include_in_schema: bool = True
    """If False, the OpenAPI schema has no property for this field. ``dump()`` still returns it."""

    title: str | None = None
    """The ``title`` of the property in the OpenAPI schema."""

    examples: list[Any] | None = None
    """The ``examples`` list of the property in the OpenAPI schema."""


def computed_field(
    func: ComputedFieldMethod | None = None,
    *,
    alias: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
    include_in_schema: bool = True,
    title: str | None = None,
    examples: list[Any] | None = None,
) -> Any:
    """
    Mark a method as a computed field for serialization output.

    Computed fields are calculated during serialization (dump) and are NOT
    stored as struct fields. They are similar to DRF's SerializerMethodField
    or Pydantic's @computed_field.

    The OpenAPI response schema of the serializer has a property for each
    computed field. The property is required and has ``readOnly: true``. Its
    type is the return type of the method. A request body schema has no
    computed field.

    Args:
        func: The method to use for computing the value (if used without parentheses)
        alias: Alternative name for this field in JSON output.
        description: The description of the property in the OpenAPI schema.
                     The default is the docstring of the method.
        deprecated: If True, the OpenAPI schema marks the property as deprecated.
        include_in_schema: If False, the OpenAPI schema has no property for this field.
                           dump() still returns the field.
        title: The title of the property in the OpenAPI schema.
        examples: A list of example values for the property in the OpenAPI schema.

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
                return self.full_name().upper()

        # When dumped:
        # {"first_name": "John", "last_name": "Doe", "full_name": "John Doe", "displayName": "JOHN DOE"}

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
            title=title,
            examples=examples,
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
        mode: ``"after"`` (the default): the validator gets the value that the
            type of the field accepted. If it returns None, the value stays.
            ``"before"``: the validator gets the raw input value, before the
            type of the field converts it. Bolt uses the value that it returns,
            also None, and the type of the field then validates that value.
            A before validator runs only for a field that the input has.
            Before validators run before after validators, whatever the order
            of the definitions. ``"wrap"`` and ``"plain"`` are not supported
            and raise ValueError.

    Example:
        class UserCreate(Serializer):
            email: str
            tags: list[str] = []

            @field_validator('email')
            @classmethod
            def validate_email(cls, value):
                if '@' not in value:
                    raise ValueError('Invalid email')
                return value.lower()

            @field_validator('tags', mode='before')
            @classmethod
            def split_tags(cls, value):
                return value.split(',') if isinstance(value, str) else value
    """
    _check_validator_mode("field_validator", mode)

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
    Decorator to validate an entire Serializer.

    Args:
        func: The validator function (if used without parentheses)
        mode: ``"after"`` (the default): the validator is a method. It gets the
            built instance, after the field validators.
            ``"before"``: the validator is a classmethod. It gets the raw input
            data (usually a dict) before any field validation, and returns the
            data to validate. ``"wrap"`` and ``"plain"`` are not supported and
            raise ValueError.

    Example:
        class UserCreate(Serializer):
            password: str
            password_confirm: str

            @model_validator
            def validate_passwords(self):
                if self.password != self.password_confirm:
                    raise ValueError('Passwords must match')

            @model_validator(mode='before')
            @classmethod
            def accept_old_key(cls, data):
                if isinstance(data, dict) and 'pass' in data:
                    data = {**data, 'password': data['pass']}
                return data
    """
    _check_validator_mode("model_validator", mode)

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


def _collect_marked(cls: type[Serializer], marker: str, *, allow_classmethod: bool) -> list[Any]:
    """
    The marked validators of a class and its bases: parent before subclass, in definition order.

    A name has one entry. A subclass that defines the name again replaces the
    definition of the parent, at the position of the parent definition (the
    rule of pydantic). A new definition that is not such a validator removes it.
    """
    found: dict[str, Any] = {}
    for base in reversed(cls.__mro__):
        if not hasattr(base, "__dict__"):
            continue
        for name, raw in base.__dict__.items():
            value = _marked_validator(raw, marker, allow_classmethod=allow_classmethod)
            if value is not None:
                found[name] = value
            else:
                found.pop(name, None)
    return list(found.values())


def _in_run_order(validators: list[Any], mode: ValidatorMode) -> list[Any]:
    """
    The validators of one mode, in the order that they run (the order of pydantic).

    After validators run in definition order, those of a parent class first.
    Before validators run in the reverse order: the last one defined runs first.
    """
    selected = [validator for validator in validators if validator.__validator_mode__ == mode]
    return selected[::-1] if mode == "before" else selected


def collect_field_validators(
    cls: type[Serializer], mode: ValidatorMode | None = None
) -> dict[str, list[FieldValidatorFunc]]:
    """
    Collect the field validators of a class and its bases.

    Returns a dict mapping field names to lists of validator functions. With a
    ``mode``, each list has the validators of that mode in the order that they
    run. With no ``mode``, each list has all the validators in definition
    order, those of a parent class first. ``subset()`` copies them in that order.
    A validator that a subclass defines again under the same name is in the
    list one time: the definition of the subclass.
    """
    marked: list[Any] = _collect_marked(cls, "__validator_field__", allow_classmethod=True)
    if mode is not None:
        marked = _in_run_order(marked, mode)
    validators: dict[str, list[FieldValidatorFunc]] = {}
    for value in marked:
        validators.setdefault(value.__validator_field__, []).append(cast(FieldValidatorFunc, value))
    return validators


def collect_model_validators(cls: type[Serializer], mode: ValidatorMode = "after") -> list[ModelValidatorFunc]:
    """
    Collect the model validators of one mode from a class and its bases.

    Returns a list of validator functions, in the order that they run.
    """
    marked = _collect_marked(cls, "__model_validator__", allow_classmethod=False)
    return cast(list[ModelValidatorFunc], _in_run_order(marked, mode))


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
