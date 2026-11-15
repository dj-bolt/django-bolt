"""Input of serializers: the data of a client cannot set a ``read_only`` field.

This module has no import from the serializers package. So the request
pipeline can import it before that package.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from functools import wraps
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


def _any_serializer_node(node: Any, check: Callable[[Any], bool], seen: set[type]) -> bool:
    found = False
    cls = getattr(node, "cls", None)
    if isinstance(cls, type):
        if cls in seen:
            return False
        seen.add(cls)
        if getattr(cls, "__is_bolt_serializer__", False):
            found = check(cls)
    for child in _input_nodes(node):
        # No short circuit: ``check`` reads each serializer of the type.
        found = _any_serializer_node(child, check, seen) or found
    return found


def any_serializer(annotation: Any, check: Callable[[Any], bool]) -> bool:
    """Tell if ``check`` is true for a serializer class that the type holds, at each depth."""
    return _any_serializer_node(msgspec.inspect.type_info(annotation), check, set())


def _check_read_only_input(cls: Any) -> bool:
    return cls._check_read_only_input()


def input_has_read_only_fields(annotation: Any) -> bool:
    """
    Tell if input of this type can build a serializer that has a ``read_only`` field.

    The check reads each serializer that the type holds, at each depth.

    Raises:
        ImproperlyConfigured: A ``read_only`` field has no default. Input cannot
            set the field, so there is no value for it.
    """
    return any_serializer(annotation, _check_read_only_input)


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


def read_only_input_extractor(factory: Callable[..., Callable[..., Any]]) -> Callable[..., Callable[..., Any]]:
    """
    Decorate the factory of a parameter model extractor (query, header, cookie, form).

    The factory gets the struct type as its first argument, one time at
    registration. The extractor of a type with no ``read_only`` field comes
    back unchanged. Each other extractor ignores the input values of
    ``read_only`` fields, as the JSON body does.
    """

    @wraps(factory)
    def build(struct_type: Any, *args: Any, **kwargs: Any) -> Callable[..., Any]:
        extract = factory(struct_type, *args, **kwargs)
        if not input_has_read_only_fields(struct_type):
            return extract

        def extract_input(*maps: Any) -> Any:
            token = INPUT_MODE.set(True)
            try:
                return extract(*maps)
            finally:
                INPUT_MODE.reset(token)

        # Callers read attributes of the extractor, such as needs_files_map.
        extract_input.__dict__.update(extract.__dict__)
        return extract_input

    return build
