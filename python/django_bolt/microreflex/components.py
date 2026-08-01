"""Component tree for micro-reflex.

Components are built ONCE at page registration (``app.add_page``), following
django-bolt's "do it once at registration, reuse forever at runtime"
principle. The compile pass assigns stable ids to dynamic slots (var-bound
text, var-bound attributes, ``cond``/``foreach`` fragments); at request time
the server only re-evaluates slots and diffs their rendered values.

Rendering is plain HTML — no React, no build step. Event wiring is stateless:
elements carry a ``data-rx-on`` attribute holding a JSON list of
``[dom_event, handler_spec]`` pairs that the JS runtime posts back verbatim.
"""

from __future__ import annotations

import html
import inspect
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .state import EventRef, Session, StateVar
from .vars import ConstVar, Var, split_format_string

if TYPE_CHECKING:
    from .compiler import Page

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)

BOOLEAN_ATTRS = frozenset(
    {
        "disabled",
        "checked",
        "autofocus",
        "required",
        "readonly",
        "hidden",
        "selected",
        "multiple",
        "open",
        "defer",
        "async",
    }
)

PROP_ALIASES = {
    "class_name": "class",
    "auto_focus": "autofocus",
    "max_length": "maxlength",
    "min_length": "minlength",
    "read_only": "readonly",
    "html_for": "for",
    "for_": "for",
    "type_": "type",
    "tab_index": "tabindex",
}

# on_change on free-text inputs maps to the live DOM "input" event (debounced
# client-side); on discrete controls it maps to "change".
_EVENT_PROP_TO_DOM = {
    "on_click": "click",
    "on_double_click": "dblclick",
    "on_input": "input",
    "on_blur": "blur",
    "on_focus": "focus",
    "on_submit": "submit",
    "on_key_down": "keydown",
    "on_key_up": "keyup",
    "on_mouse_enter": "mouseenter",
    "on_mouse_leave": "mouseleave",
}

_HEADING_SIZES = {
    "1": "12px",
    "2": "14px",
    "3": "16px",
    "4": "18px",
    "5": "20px",
    "6": "24px",
    "7": "30px",
    "8": "36px",
    "9": "60px",
}

_SPACING_SCALE = {
    "0": "0",
    "1": "4px",
    "2": "8px",
    "3": "12px",
    "4": "16px",
    "5": "24px",
    "6": "32px",
    "7": "40px",
    "8": "48px",
    "9": "64px",
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _style_to_css(style: dict[str, Any], session: Session | None) -> str:
    parts = []
    for key, value in style.items():
        if isinstance(value, Var):
            if session is None:
                raise TypeError("Var style values require render-time evaluation")
            value = value._ev(session)
        parts.append(f"{key.replace('_', '-')}:{value}")
    return ";".join(parts)


def _serialize_event_arg(arg: Any, session: Session) -> Any:
    """Serialize an EventRef argument into the handler spec.

    Constants and foreach item vars are baked in by value; plain state field
    vars are serialized as references resolved at dispatch time (so they are
    never stale in HTML rendered once at startup).
    """
    if isinstance(arg, StateVar):
        return {"$s": arg.state_cls.__name__, "$f": arg.field}
    if isinstance(arg, ConstVar):
        return arg._ev(session)
    if isinstance(arg, Var):
        return arg._ev(session)
    return arg


class Component:
    """Base class for renderable nodes."""

    __slots__ = ()

    def compile(self, page: Page, inline: bool) -> None:
        raise NotImplementedError

    def render(self, session: Session) -> str:
        raise NotImplementedError


class TextNode(Component):
    """Text content, possibly containing Var parts (from f-strings or bare Vars)."""

    __slots__ = ("parts", "dynamic", "slot_id")

    def __init__(self, parts: list[str | Var]):
        self.parts = parts
        self.dynamic = any(isinstance(p, Var) for p in parts)
        self.slot_id: int | None = None

    def compile(self, page: Page, inline: bool) -> None:
        if self.dynamic and not inline:
            self.slot_id = page.add_text_slot(self)

    def render_text(self, session: Session) -> str:
        out = []
        for part in self.parts:
            if isinstance(part, Var):
                out.append(str(part._ev(session)))
            else:
                out.append(part)
        return "".join(out)

    def render(self, session: Session) -> str:
        text = html.escape(self.render_text(session))
        if self.slot_id is not None:
            return f'<span data-rx-t="{self.slot_id}">{text}</span>'
        return text


class Element(Component):
    """A single HTML element with static/dynamic attrs, events, and children."""

    __slots__ = ("tag", "children", "static_attrs", "dynamic_attrs", "events", "elem_id")

    def __init__(self, tag: str, *children: Any, **props: Any):
        self.tag = props.pop("as_", None) or tag
        self.elem_id: int | None = None
        self.events: list[tuple[str, EventRef, dict[str, Any]]] = []
        self.static_attrs: list[tuple[str, Any]] = []
        self.dynamic_attrs: list[tuple[str, Var | Callable[[Session], Any]]] = []

        props.pop("key", None)
        style = props.pop("style", None)
        input_type = props.get("type") or props.get("type_")

        for prop_name, prop_value in props.items():
            if prop_value is None:
                continue
            if prop_name.startswith("on_"):
                self._add_event(prop_name, prop_value, input_type)
                continue
            attr = PROP_ALIASES.get(prop_name, prop_name.replace("_", "-"))
            if isinstance(prop_value, Var):
                self.dynamic_attrs.append((attr, prop_value))
            else:
                self.static_attrs.append((attr, prop_value))

        if style is not None:
            if isinstance(style, str):
                self.static_attrs.append(("style", style))
            elif any(isinstance(v, Var) for v in style.values()):
                self.dynamic_attrs.append(("style", lambda s, _st=style: _style_to_css(_st, s)))
            else:
                self.static_attrs.append(("style", _style_to_css(style, None)))

        self.children = normalize_children(children)

    def _add_event(self, prop_name: str, value: Any, input_type: Any) -> None:
        if not isinstance(value, EventRef):
            raise TypeError(
                f"{prop_name} must be a state event handler reference "
                f"(e.g. State.increment or State.set_text), got {value!r}"
            )
        if prop_name == "on_change":
            dom_event = "change" if self.tag == "select" or input_type in ("checkbox", "radio") else "input"
        else:
            dom_event = _EVENT_PROP_TO_DOM.get(prop_name)
            if dom_event is None:
                raise TypeError(f"Unsupported event prop: {prop_name}")
        opts: dict[str, Any] = {}
        if dom_event == "input" and prop_name == "on_change":
            opts["d"] = 1  # debounce hint for the JS runtime
        self.events.append((dom_event, value, opts))

    def compile(self, page: Page, inline: bool) -> None:
        if self.dynamic_attrs and not inline:
            self.elem_id = page.add_element(self)
        for child in self.children:
            child.compile(page, inline)

    def _render_attr(self, name: str, value: Any, out: list[str]) -> None:
        if name in BOOLEAN_ATTRS or isinstance(value, bool):
            if value:
                out.append(f" {name}")
            return
        out.append(f' {name}="{_escape(value)}"')

    def render(self, session: Session) -> str:
        out = [f"<{self.tag}"]
        if self.elem_id is not None:
            out.append(f' data-rx-e="{self.elem_id}"')
        for name, value in self.static_attrs:
            self._render_attr(name, value, out)
        for name, value in self.dynamic_attrs:
            evaluated = value._ev(session) if isinstance(value, Var) else value(session)
            if evaluated is not None:
                self._render_attr(name, evaluated, out)
        if self.events:
            serialized = json.dumps(_serialize_specs(self.events, session), separators=(",", ":"))
            out.append(f' data-rx-on="{_escape(serialized)}"')
        if self.tag in VOID_TAGS:
            out.append("/>")
            return "".join(out)
        out.append(">")
        for child in self.children:
            out.append(child.render(session))
        out.append(f"</{self.tag}>")
        return "".join(out)


def _serialize_specs(events: list[tuple[str, EventRef, dict[str, Any]]], session: Session) -> list[list[Any]]:
    serialized = []
    for dom_event, ref, opts in events:
        spec: dict[str, Any] = {"s": ref.state_cls.__name__, "n": ref.name}
        if ref.args:
            spec["a"] = [_serialize_event_arg(a, session) for a in ref.args]
        if opts:
            spec["o"] = opts
        serialized.append([dom_event, spec])
    return serialized


class Fragment(Component):
    """Children without a wrapper element."""

    __slots__ = ("children",)

    def __init__(self, *children: Any):
        self.children = normalize_children(children)

    def compile(self, page: Page, inline: bool) -> None:
        for child in self.children:
            child.compile(page, inline)

    def render(self, session: Session) -> str:
        return "".join(child.render(session) for child in self.children)


class CondFragment(Component):
    """``rx.cond(condition, then, otherwise)`` — server-evaluated branch."""

    __slots__ = ("condition", "then", "otherwise", "slot_id")

    def __init__(self, condition: Var, then: Any, otherwise: Any = None):
        if not isinstance(condition, Var):
            raise TypeError("rx.cond() condition must be a state Var")
        self.condition = condition
        self.then = normalize_child(then)
        self.otherwise = normalize_child(otherwise) if otherwise is not None else None
        self.slot_id: int | None = None

    def compile(self, page: Page, inline: bool) -> None:
        if not inline:
            self.slot_id = page.add_html_slot(self)
        # Branch subtrees always render inline within this fragment's patch.
        self.then.compile(page, True)
        if self.otherwise is not None:
            self.otherwise.compile(page, True)

    def render_content(self, session: Session) -> str:
        if self.condition._ev(session):
            return self.then.render(session)
        if self.otherwise is not None:
            return self.otherwise.render(session)
        return ""

    def render(self, session: Session) -> str:
        content = self.render_content(session)
        if self.slot_id is not None:
            return f'<div data-rx-h="{self.slot_id}" style="display:contents">{content}</div>'
        return content


class ForeachFragment(Component):
    """``rx.foreach(State.items, render_fn)`` — server-rendered list."""

    __slots__ = ("iterable", "render_fn", "wants_index", "slot_id")

    def __init__(self, iterable: Var, render_fn: Callable):
        if not isinstance(iterable, Var):
            raise TypeError("rx.foreach() iterable must be a state Var")
        self.iterable = iterable
        self.render_fn = render_fn
        params = [
            p
            for p in inspect.signature(render_fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.wants_index = len(params) >= 2
        self.slot_id: int | None = None

    def compile(self, page: Page, inline: bool) -> None:
        if not inline:
            self.slot_id = page.add_html_slot(self)
        # Item subtrees are constructed fresh on every render — nothing to
        # compile ahead of time.

    def render_content(self, session: Session) -> str:
        out = []
        for index, item in enumerate(self.iterable._ev(session)):
            if self.wants_index:
                node = self.render_fn(ConstVar(item), ConstVar(index))
            else:
                node = self.render_fn(ConstVar(item))
            out.append(normalize_child(node).render(session))
        return "".join(out)

    def render(self, session: Session) -> str:
        content = self.render_content(session)
        if self.slot_id is not None:
            return f'<div data-rx-h="{self.slot_id}" style="display:contents">{content}</div>'
        return content


def normalize_child(child: Any) -> Component:
    """Coerce a single child value into a Component."""
    if isinstance(child, Component):
        return child
    if isinstance(child, Var):
        return TextNode([child])
    if isinstance(child, str):
        parts = split_format_string(child)
        return TextNode(parts) if parts is not None else TextNode([child])
    if isinstance(child, (int, float)):
        return TextNode([str(child)])
    raise TypeError(f"Cannot render {child!r} as a component child")


def normalize_children(children: tuple[Any, ...]) -> list[Component]:
    out: list[Component] = []
    for child in children:
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            out.extend(normalize_children(tuple(child)))
        else:
            out.append(normalize_child(child))
    return out


def cond(condition: Var, then: Any, otherwise: Any = None) -> CondFragment:
    return CondFragment(condition, then, otherwise)


def foreach(iterable: Var, render_fn: Callable) -> ForeachFragment:
    return ForeachFragment(iterable, render_fn)


# ---------------------------------------------------------------------------
# Reflex-compatible component factories
# ---------------------------------------------------------------------------


class _ElNamespace:
    """``rx.el.<tag>(...)`` — generic factory for any HTML tag."""

    def __getattr__(self, tag: str) -> Callable[..., Element]:
        return lambda *children, **props: Element(tag, *children, **props)


el = _ElNamespace()


def fragment(*children: Any, **_props: Any) -> Fragment:
    return Fragment(*children)


def box(*children: Any, **props: Any) -> Element:
    return Element("div", *children, **props)


def text(*children: Any, **props: Any) -> Element:
    return Element("p", *children, **props)


def heading(*children: Any, **props: Any) -> Element:
    size = props.pop("size", None)
    if size is not None:
        style = props.setdefault("style", {})
        if isinstance(style, dict):
            style.setdefault("font-size", _HEADING_SIZES.get(str(size), "24px"))
    return Element("h1", *children, **props)


def _stack(direction: str, children: tuple[Any, ...], props: dict[str, Any]) -> Element:
    spacing = props.pop("spacing", "3")
    align = props.pop("align", None)
    justify = props.pop("justify", None)
    style = props.setdefault("style", {})
    if isinstance(style, dict):
        style.setdefault("display", "flex")
        style.setdefault("flex-direction", direction)
        style.setdefault("gap", _SPACING_SCALE.get(str(spacing), "12px"))
        if align:
            style.setdefault("align-items", align)
        if justify:
            style.setdefault("justify-content", justify)
    return Element("div", *children, **props)


def vstack(*children: Any, **props: Any) -> Element:
    return _stack("column", children, props)


def hstack(*children: Any, **props: Any) -> Element:
    props.setdefault("align", "center")
    return _stack("row", children, props)


def center(*children: Any, **props: Any) -> Element:
    props.setdefault("align", "center")
    props.setdefault("justify", "center")
    return _stack("column", children, props)


def container(*children: Any, **props: Any) -> Element:
    style = props.setdefault("style", {})
    if isinstance(style, dict):
        style.setdefault("max-width", "60rem")
        style.setdefault("margin", "0 auto")
        style.setdefault("padding", "1rem")
    return Element("div", *children, **props)


def spacer(**props: Any) -> Element:
    style = props.setdefault("style", {})
    if isinstance(style, dict):
        style.setdefault("flex", "1")
    return Element("div", **props)


def divider(**props: Any) -> Element:
    return Element("hr", **props)


def button(*children: Any, **props: Any) -> Element:
    props.setdefault("type", "button")
    return Element("button", *children, **props)


def input(**props: Any) -> Element:  # noqa: A001 - reflex-compatible name
    return Element("input", **props)


def text_area(*children: Any, **props: Any) -> Element:
    return Element("textarea", *children, **props)


def checkbox(*children: Any, **props: Any) -> Element:
    checked = props.pop("checked", None)
    label_children = children
    field = Element("input", type="checkbox", checked=checked, **props)
    if not label_children:
        return field
    style = {"display": "inline-flex", "align-items": "center", "gap": "6px"}
    return Element("label", field, *label_children, style=style)


def select(items: Any = None, **props: Any) -> Element:
    children: list[Any] = []
    if isinstance(items, Var):
        children.append(foreach(items, lambda item: Element("option", item)))
    elif items is not None:
        children.extend(Element("option", item) for item in items)
    return Element("select", *children, **props)


def form(*children: Any, **props: Any) -> Element:
    return Element("form", *children, **props)


def link(*children: Any, **props: Any) -> Element:
    return Element("a", *children, **props)


def image(src: Any = None, **props: Any) -> Element:
    if src is not None:
        props["src"] = src
    return Element("img", **props)


def code(*children: Any, **props: Any) -> Element:
    return Element("code", *children, **props)


def badge(*children: Any, **props: Any) -> Element:
    style = props.setdefault("style", {})
    if isinstance(style, dict):
        style.setdefault("display", "inline-block")
        style.setdefault("padding", "2px 8px")
        style.setdefault("border-radius", "999px")
        style.setdefault("background", "#eee")
        style.setdefault("font-size", "12px")
    return Element("span", *children, **props)
