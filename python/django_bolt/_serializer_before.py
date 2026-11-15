"""Validators of serializers with ``mode="before"``: they get the raw input.

msgspec converts the input and builds a serializer in one step. A before
validator must see a value before that step. So a type that holds a serializer
with a before validator has a plan: decode the input to builtins, run the plan
on them, then call ``msgspec.convert()``. A type with no before validator has
no plan and keeps the direct msgspec decode.

This module has no import from the serializers package. So the request
pipeline can import it before that package.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import msgspec

from .exceptions import RequestValidationError

# A plan gets the raw value, the list for the errors and the location of the value.
Plan = Callable[[Any, list[dict[str, Any]], tuple[Any, ...]], Any]

# True while msgspec builds serializers from input that a plan has read. The
# before validators ran on the raw input, so ``__post_init__`` does not run them again.
RAW_VALIDATED: ContextVar[bool] = ContextVar("django_bolt_serializer_raw_validated", default=False)

# The value of ``Serializer.__before_plan__`` until the first input of the class.
PLAN_NOT_BUILT: Any = object()

# Nodes that hold other nodes in ``fields``, and that a plan does not read.
_OPAQUE_FIELD_NODES = ("DataclassType", "TypedDictType", "NamedTupleType")


def _child_nodes(node: Any) -> list[Any]:
    """The ``msgspec.inspect`` nodes that ``node`` holds."""
    children = [
        child
        for child in (getattr(node, attr, None) for attr in ("type", "item_type", "key_type", "value_type"))
        if child is not None
    ]
    for attr in ("types", "item_types", "fields"):
        children.extend(getattr(node, attr, None) or ())
    return children


def _has_before_validators(cls: Any) -> bool:
    return bool(getattr(cls, "__before_field_validators__", None) or getattr(cls, "__before_model_validators__", None))


def _holds_before_validators(node: Any, seen: set[type]) -> bool:
    """Tell if ``node`` holds a serializer class with a before validator, at each depth."""
    cls = getattr(node, "cls", None)
    if isinstance(cls, type):
        if cls in seen:
            return False
        seen.add(cls)
        if _has_before_validators(cls):
            return True
    return any(_holds_before_validators(child, seen) for child in _child_nodes(node))


def _value_error(exc: Exception, loc: tuple[Any, ...]) -> dict[str, Any]:
    return {"loc": list(loc), "msg": str(exc), "type": "value_error"}


class _PlanBuilder:
    """Build the plan of a type. One plan for each struct class, so a type can hold itself."""

    def __init__(self) -> None:
        self.struct_plans: dict[type, Plan | None] = {}

    def plan(self, node: Any) -> Plan | None:
        kind = type(node).__name__
        if kind == "StructType":
            return self._struct_plan(node)
        if kind == "Metadata":
            return self.plan(node.type)
        if kind == "UnionType":
            return self._union_plan(node)
        if kind in ("ListType", "SetType", "FrozenSetType", "VarTupleType"):
            return self._items_plan(self.plan(node.item_type))
        if kind == "TupleType":
            return self._tuple_plan([self.plan(item) for item in node.item_types])
        if kind == "DictType":
            return self._mapping_plan(self.plan(node.value_type))
        if kind in _OPAQUE_FIELD_NODES and _holds_before_validators(node, set()):
            raise TypeError(
                f'{node.cls.__name__} holds a serializer that has a validator with mode="before". '
                "Bolt runs before validators in a Serializer or msgspec.Struct, not in a dataclass, "
                "a TypedDict or a NamedTuple."
            )
        return None

    def _struct_plan(self, node: Any) -> Plan | None:
        cls = node.cls
        if cls in self.struct_plans:
            # The class holds itself, or the type has it two times. Read the plan at call time.
            def late(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
                plan = self.struct_plans[cls]
                return raw if plan is None else plan(raw, errors, loc)

            return late

        self.struct_plans[cls] = None
        model_validators = tuple(getattr(cls, "__before_model_validators__", ()))
        field_validators = dict(getattr(cls, "__before_field_validators__", ()))
        if (model_validators or field_validators) and node.array_like:
            raise TypeError(f'{cls.__name__}: a validator with mode="before" needs a struct that is not array_like.')

        steps = []
        for field in node.fields:
            validators = field_validators.get(field.name, ())
            child = self.plan(field.type)
            if validators or child is not None:
                steps.append((field.encode_name, validators, child))
        if not (model_validators or steps):
            return None

        def struct_plan(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
            data = raw
            try:
                for validator in model_validators:
                    data = validator(cls, data)
            except (ValueError, TypeError) as exc:
                errors.append(_value_error(exc, loc))
                return raw
            if type(data) is not dict:
                # msgspec.convert() reports the wrong type.
                return data
            # The input of the caller keeps its values.
            data = dict(data)
            for key, validators, child in steps:
                if key not in data:
                    continue
                value = data[key]
                try:
                    for validator in validators:
                        value = validator(cls, value)
                except (ValueError, TypeError) as exc:
                    errors.append(_value_error(exc, (*loc, key)))
                    continue
                data[key] = value if child is None else child(value, errors, (*loc, key))
            return data

        self.struct_plans[cls] = struct_plan
        return struct_plan

    def _union_plan(self, node: Any) -> Plan | None:
        """One plan for each JSON kind of the union: an object, an array."""
        struct_plans: list[tuple[Any, Any, Plan]] = []
        array_plan = mapping_plan = None
        for arm in node.types:
            plan = self.plan(arm)
            if plan is None:
                continue
            kind = type(arm).__name__
            if kind == "StructType":
                struct_plans.append((arm.tag_field, arm.tag, plan))
            elif kind == "DictType":
                mapping_plan = plan
            else:
                array_plan = plan
        if not (struct_plans or array_plan or mapping_plan):
            return None

        def union_plan(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
            if type(raw) is list:
                return raw if array_plan is None else array_plan(raw, errors, loc)
            if type(raw) is not dict:
                return raw
            for tag_field, tag, plan in struct_plans:
                # msgspec permits two struct types in a union only with tags.
                if tag_field is None or raw.get(tag_field) == tag:
                    return plan(raw, errors, loc)
            return raw if mapping_plan is None else mapping_plan(raw, errors, loc)

        return union_plan

    @staticmethod
    def _items_plan(item_plan: Plan | None) -> Plan | None:
        if item_plan is None:
            return None

        def items_plan(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
            if type(raw) is not list:
                return raw
            return [item_plan(item, errors, (*loc, index)) for index, item in enumerate(raw)]

        return items_plan

    @staticmethod
    def _tuple_plan(item_plans: list[Plan | None]) -> Plan | None:
        if not any(item_plans):
            return None

        def tuple_plan(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
            if type(raw) is not list or len(raw) != len(item_plans):
                return raw
            return [
                item if plan is None else plan(item, errors, (*loc, index))
                for index, (item, plan) in enumerate(zip(raw, item_plans, strict=True))
            ]

        return tuple_plan

    @staticmethod
    def _mapping_plan(value_plan: Plan | None) -> Plan | None:
        if value_plan is None:
            return None

        def mapping_plan(raw: Any, errors: list[dict[str, Any]], loc: tuple[Any, ...]) -> Any:
            if type(raw) is not dict:
                return raw
            return {key: value_plan(value, errors, (*loc, key)) for key, value in raw.items()}

        return mapping_plan


def before_plan(annotation: Any) -> Plan | None:
    """
    The plan that runs the before validators of a type on raw input.

    None if the type holds no serializer with a before validator. Such a type
    keeps the direct msgspec decode. Call this one time for each type.
    """
    node = msgspec.inspect.type_info(annotation)
    if not _holds_before_validators(node, set()):
        return None
    return _PlanBuilder().plan(node)


def run_before_validators(plan: Plan, raw: Any) -> Any:
    """
    Run a plan on raw input. Return the input for ``msgspec.convert()``.

    Raises:
        RequestValidationError: A before validator raised ValueError or TypeError.
    """
    errors: list[dict[str, Any]] = []
    data = plan(raw, errors, ("body",))
    if errors:
        raise RequestValidationError(errors=errors)
    return data
