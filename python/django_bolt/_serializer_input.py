"""Input of serializers: the data of a client cannot set a ``read_only`` field.

This module has no import from the serializers package. So the request
pipeline can import it before that package.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any

import msgspec

# True while msgspec builds serializers from the data of a client: a request
# body, model_validate() or model_validate_json(). A serializer with a
# ``read_only`` field reads it in ``__post_init__``. Python code that builds a
# serializer (``S(id=5)``, ``from_model()``) does not set it.
INPUT_MODE: ContextVar[bool] = ContextVar("django_bolt_serializer_input", default=False)


def _input_nodes(node: Any) -> Iterable[Any]:
    """The ``msgspec.inspect`` nodes that ``node`` holds."""
    for attr in ("type", "item_type", "key_type", "value_type"):
        child = getattr(node, attr, None)
        if child is not None:
            yield child
    for attr in ("types", "item_types", "fields"):
        yield from getattr(node, attr, None) or ()


def _input_node_has_read_only(node: Any, seen: set[type]) -> bool:
    found = False
    cls = getattr(node, "cls", None)
    if isinstance(cls, type):
        if cls in seen:
            return False
        seen.add(cls)
        if getattr(cls, "__is_bolt_serializer__", False):
            found = cls._check_read_only_input()
    for child in _input_nodes(node):
        # No short circuit: each serializer of the type must pass its check.
        found = _input_node_has_read_only(child, seen) or found
    return found


def input_has_read_only_fields(annotation: Any) -> bool:
    """
    Tell if input of this type can build a serializer that has a ``read_only`` field.

    The check reads each serializer that the type holds, at each depth.

    Raises:
        ImproperlyConfigured: A ``read_only`` field has no default. Input cannot
            set the field, so there is no value for it.
    """
    return _input_node_has_read_only(msgspec.inspect.type_info(annotation), set())


def ignore_read_only_input(annotation: Any, decode: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """
    Make ``decode`` ignore the input values of ``read_only`` fields.

    ``decode`` builds an object of type ``annotation`` from the data of a client.
    Call this one time for each type, not for each request. A type with no
    ``read_only`` field gets ``decode`` back, unchanged.
    """
    if not input_has_read_only_fields(annotation):
        return decode

    def decode_input(data: Any) -> Any:
        token = INPUT_MODE.set(True)
        try:
            return decode(data)
        finally:
            INPUT_MODE.reset(token)

    return decode_input
