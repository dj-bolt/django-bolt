"""State classes for micro-reflex — Reflex-compatible ``rx.State`` syntax.

    class CounterState(rx.State):
        count: int = 0

        def increment(self):
            self.count += 1

Class-level access (``CounterState.count``) yields a :class:`Var` for building
component trees; instance access yields the concrete per-session value.
Event handlers accessed on the class (``CounterState.increment``) yield an
:class:`EventRef` that components serialize into ``data-rx-on`` attributes.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable
from typing import Any

from .vars import Var

# Name → State subclass, used to resolve incoming client events. Only classes
# created through StateMeta are reachable, and only their registered public
# event handlers (or auto-setters for declared fields) can be invoked.
STATE_REGISTRY: dict[str, type[State]] = {}

_ZERO_DEFAULTS: dict[type, Callable[[], Any]] = {
    int: int,
    float: float,
    str: str,
    bool: bool,
    list: list,
    dict: dict,
    set: set,
}


class EventRef:
    """A reference to a state event handler, usable as an ``on_*`` prop.

    Calling an EventRef partially applies arguments (evaluated at render
    time, so foreach item vars work): ``on_click=State.remove(item)``.
    """

    __slots__ = ("state_cls", "name", "args")

    def __init__(self, state_cls: type[State], name: str, args: tuple[Any, ...] = ()):
        self.state_cls = state_cls
        self.name = name
        self.args = args

    def __call__(self, *args: Any) -> EventRef:
        return EventRef(self.state_cls, self.name, self.args + args)


class StateVar(Var):
    """Class-level access to a declared state field (``State.count``).

    Kept as a distinct type so event-handler arguments referencing plain state
    fields can be serialized as stable references (resolved at dispatch time)
    instead of being baked into HTML rendered once at startup.
    """

    __slots__ = ("state_cls", "field")

    def __init__(self, state_cls: type[State], field: str):
        self.state_cls = state_cls
        self.field = field
        super().__init__(lambda session, _cls=state_cls, _f=field: getattr(session.get(_cls), _f))


class _EventHandlerDescriptor:
    """Wraps handler functions so class access returns an EventRef."""

    __slots__ = ("fn", "name")

    def __init__(self, fn: Callable, name: str):
        self.fn = fn
        self.name = name

    def __get__(self, instance: Any, owner: type) -> Any:
        if instance is None:
            return EventRef(owner, self.name)
        return self.fn.__get__(instance, owner)


class ComputedVar:
    """Descriptor created by ``@rx.var`` — a derived, read-only state var."""

    __slots__ = ("fn", "name")

    def __init__(self, fn: Callable):
        self.fn = fn
        self.name = fn.__name__

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, instance: Any, owner: type) -> Any:
        if instance is None:
            return Var(lambda session, _cls=owner, _fn=self.fn: _fn(session.get(_cls)))
        return self.fn(instance)


def var(fn: Callable | None = None, **_kwargs: Any):
    """``@rx.var`` — declare a computed var. ``cache=``/``deps=`` are accepted
    and ignored (micro-reflex recomputes on every render and diffs)."""
    if fn is None:
        return lambda f: ComputedVar(f)
    return ComputedVar(fn)


def event(fn: Callable | None = None, **_kwargs: Any):
    """``@rx.event`` — compatibility no-op marker (all public methods on a
    State subclass are event handlers in micro-reflex)."""
    if fn is None:
        return lambda f: f
    return fn


class StateMeta(type):
    def __new__(mcs, name: str, bases: tuple, namespace: dict, **kwargs: Any):
        # Collect field defaults from bases, then this class's annotations.
        fields: dict[str, Any] = {}
        for base in bases:
            fields.update(getattr(base, "_rx_fields", {}))
        annotations = namespace.get("__annotations__", {})
        for field_name, field_type in annotations.items():
            if field_name.startswith("_"):
                continue
            if field_name in namespace:
                # Remove the default from the class body so class-level access
                # falls through to __getattr__ and yields a Var.
                fields[field_name] = namespace.pop(field_name)
            else:
                factory = _ZERO_DEFAULTS.get(field_type)
                fields[field_name] = factory() if factory is not None else None

        # Wrap public handler functions so class access yields EventRefs.
        handlers: dict[str, Callable] = {}
        for base in bases:
            handlers.update(getattr(base, "_rx_handlers", {}))
        for attr_name, attr_value in list(namespace.items()):
            if attr_name.startswith("_"):
                continue
            if inspect.isfunction(attr_value):
                handlers[attr_name] = attr_value
                namespace[attr_name] = _EventHandlerDescriptor(attr_value, attr_name)

        namespace["_rx_fields"] = fields
        namespace["_rx_handlers"] = handlers
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        if bases:  # don't register the State base itself
            STATE_REGISTRY[name] = cls  # type: ignore[assignment]
        return cls

    def __getattr__(cls, name: str) -> Any:
        # Class-level access to declared fields → Var; auto-setters → EventRef.
        fields = cls.__dict__.get("_rx_fields") or getattr(cls, "_rx_fields", {})
        if name in fields:
            return StateVar(cls, name)  # type: ignore[arg-type]
        if name.startswith("set_") and name[4:] in fields:
            return EventRef(cls, name)  # type: ignore[arg-type]
        raise AttributeError(f"{cls.__name__} has no var or handler named {name!r}")


class State(metaclass=StateMeta):
    """Base class for micro-reflex state. Subclass with annotated vars."""

    _rx_fields: dict[str, Any] = {}
    _rx_handlers: dict[str, Callable] = {}

    def __init__(self) -> None:
        for field_name, default in type(self)._rx_fields.items():
            object.__setattr__(self, field_name, copy.deepcopy(default))


class Session:
    """Per-connection state container + render cache for slot diffing."""

    __slots__ = ("states", "cache")

    def __init__(self) -> None:
        self.states: dict[type[State], State] = {}
        self.cache: dict[str, Any] = {}

    def get(self, cls: type[State]) -> State:
        instance = self.states.get(cls)
        if instance is None:
            instance = cls()
            self.states[cls] = instance
        return instance
