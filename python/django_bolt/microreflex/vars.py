"""Reactive var references for micro-reflex.

A ``Var`` is a lazily-evaluated reference into session state. Component trees
are built once at registration time with ``Var`` objects in place of values;
at render time each var is evaluated against a concrete :class:`Session`.

Because all rendering happens server-side, a var's evaluator can be arbitrary
Python — operators on vars simply compose new evaluators. This is what lets
micro-reflex support ``State.count * 2`` or ``State.text.strip().upper()``
without a JS expression compiler.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import Session

# Registry of vars referenced from f-strings via ``Var.__format__``.
# Only populated at page-build time (once per f-string occurrence), never at
# request time, so growth is bounded by the number of compiled pages.
_FORMAT_REGISTRY: list[Var] = []

_SENTINEL_RE = re.compile("\x00RX(\\d+)\x00")


class Var:
    """A lazily-evaluated reference to (a function of) session state."""

    __slots__ = ("_evaluator",)

    def __init__(self, evaluator: Callable[[Session], Any]):
        self._evaluator = evaluator

    def _ev(self, session: Session) -> Any:
        return self._evaluator(session)

    # -- f-string support: f"Count: {State.count}" ---------------------------
    def __format__(self, format_spec: str) -> str:
        _FORMAT_REGISTRY.append(self)
        return f"\x00RX{len(_FORMAT_REGISTRY) - 1}\x00"

    def __bool__(self) -> bool:
        raise TypeError(
            "Cannot use a Var in a plain `if`/`and`/`or` — it has no value until "
            "render time. Use rx.cond(...) for conditional UI, or move the logic "
            "into an event handler / @rx.var computed var."
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__}>"

    # -- operator composition -------------------------------------------------
    def _binop(self, other: Any, op: Callable[[Any, Any], Any]) -> Var:
        return Var(lambda s, _v=self, _o=other, _op=op: _op(_v._ev(s), _eval_maybe(_o, s)))

    def _rbinop(self, other: Any, op: Callable[[Any, Any], Any]) -> Var:
        return Var(lambda s, _v=self, _o=other, _op=op: _op(_eval_maybe(_o, s), _v._ev(s)))

    def __add__(self, other):
        return self._binop(other, lambda a, b: a + b)

    def __radd__(self, other):
        return self._rbinop(other, lambda a, b: a + b)

    def __sub__(self, other):
        return self._binop(other, lambda a, b: a - b)

    def __rsub__(self, other):
        return self._rbinop(other, lambda a, b: a - b)

    def __mul__(self, other):
        return self._binop(other, lambda a, b: a * b)

    def __rmul__(self, other):
        return self._rbinop(other, lambda a, b: a * b)

    def __truediv__(self, other):
        return self._binop(other, lambda a, b: a / b)

    def __mod__(self, other):
        return self._binop(other, lambda a, b: a % b)

    def __neg__(self):
        return Var(lambda s, _v=self: -_v._ev(s))

    def __eq__(self, other):  # type: ignore[override]
        return self._binop(other, lambda a, b: a == b)

    def __ne__(self, other):  # type: ignore[override]
        return self._binop(other, lambda a, b: a != b)

    __hash__ = object.__hash__

    def __lt__(self, other):
        return self._binop(other, lambda a, b: a < b)

    def __le__(self, other):
        return self._binop(other, lambda a, b: a <= b)

    def __gt__(self, other):
        return self._binop(other, lambda a, b: a > b)

    def __ge__(self, other):
        return self._binop(other, lambda a, b: a >= b)

    def __and__(self, other):
        return self._binop(other, lambda a, b: a and b)

    def __or__(self, other):
        return self._binop(other, lambda a, b: a or b)

    def __invert__(self):
        return Var(lambda s, _v=self: not _v._ev(s))

    def __getitem__(self, key):
        return self._binop(key, lambda a, b: a[b])

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return _AttrVar(self, name)

    # -- reflex-compatible helpers -------------------------------------------
    def length(self) -> Var:
        return Var(lambda s, _v=self: len(_v._ev(s)))

    def to_string(self) -> Var:
        return Var(lambda s, _v=self: str(_v._ev(s)))

    def bool(self) -> Var:
        return Var(lambda s, _v=self: bool(_v._ev(s)))

    def contains(self, item: Any) -> Var:
        return self._binop(item, lambda a, b: b in a)

    def reverse(self) -> Var:
        return Var(lambda s, _v=self: list(reversed(_v._ev(s))))


class _AttrVar(Var):
    """``State.user.name`` / ``item.done`` — attribute or key access, callable."""

    __slots__ = ("_parent", "_name")

    def __init__(self, parent: Var, name: str):
        self._parent = parent
        self._name = name
        super().__init__(self._resolve)

    def _resolve(self, session: Session) -> Any:
        obj = self._parent._ev(session)
        if isinstance(obj, dict):
            return obj[self._name]
        return getattr(obj, self._name)

    def __call__(self, *args: Any, **kwargs: Any) -> Var:
        parent, name = self._parent, self._name

        def call(session: Session) -> Any:
            obj = parent._ev(session)
            ev_args = [_eval_maybe(a, session) for a in args]
            ev_kwargs = {k: _eval_maybe(v, session) for k, v in kwargs.items()}
            return getattr(obj, name)(*ev_args, **ev_kwargs)

        return Var(call)


class ConstVar(Var):
    """Wraps a concrete value — used for foreach items and indices."""

    __slots__ = ("_value",)

    def __init__(self, value: Any):
        self._value = value
        super().__init__(lambda _s, _v=value: _v)


def _eval_maybe(value: Any, session: Session) -> Any:
    """Evaluate ``value`` if it is a Var, else return it unchanged."""
    if isinstance(value, Var):
        return value._ev(session)
    return value


def split_format_string(text: str) -> list[str | Var] | None:
    """Split a string containing ``Var.__format__`` sentinels into parts.

    Returns None when the string has no sentinels (fully static).
    """
    if "\x00" not in text:
        return None
    parts: list[str | Var] = []
    pos = 0
    for match in _SENTINEL_RE.finditer(text):
        if match.start() > pos:
            parts.append(text[pos : match.start()])
        parts.append(_FORMAT_REGISTRY[int(match.group(1))])
        pos = match.end()
    if pos < len(text):
        parts.append(text[pos:])
    return parts
