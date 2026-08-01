"""Page compilation for micro-reflex.

A :class:`Page` is compiled exactly once at ``app.add_page()`` time:

- the component tree is walked and every *top-level* dynamic node (var-bound
  text, var-bound attribute, ``cond``/``foreach`` fragment) gets a stable
  slot id that appears in the HTML as a ``data-rx-*`` marker;
- the full HTML document (initial render + inlined JS runtime) is rendered
  from a fresh default-state session and cached as a plain string, so the
  page GET handler is a trivially-async function returning a constant —
  it rides django-bolt's Rust sync-dispatch fast path.

At request time only ``compute_patches`` runs: re-evaluate each slot against
the session's state and diff against the last value sent to that client.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import Session
from .vars import Var

if TYPE_CHECKING:
    from .components import Component, Element, TextNode

_RUNTIME_JS = (Path(__file__).parent / "runtime.js").read_text(encoding="utf-8")

_MISSING = object()

DEFAULT_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
     line-height:1.5;color:#1a1a1a;background:#fff}
button{font:inherit;padding:6px 14px;border:1px solid #ccc;border-radius:6px;
       background:#f6f6f6;cursor:pointer}
button:hover{background:#ececec}
input,textarea,select{font:inherit;padding:6px 10px;border:1px solid #ccc;border-radius:6px}
a{color:#0b6bcb}
h1,h2,h3,h4,h5,h6,p{margin:0}
hr{border:none;border-top:1px solid #e5e5e5;width:100%}
""".strip()


class TextSlot:
    __slots__ = ("slot_id", "node", "key")

    def __init__(self, slot_id: int, node: TextNode):
        self.slot_id = slot_id
        self.node = node
        self.key = f"t{slot_id}"

    def render_value(self, session: Session) -> str:
        return self.node.render_text(session)

    def patch(self, value: str) -> dict[str, Any]:
        return {"k": "t", "i": self.slot_id, "v": value}


class HtmlSlot:
    __slots__ = ("slot_id", "node", "key")

    def __init__(self, slot_id: int, node: Component):
        self.slot_id = slot_id
        self.node = node
        self.key = f"h{slot_id}"

    def render_value(self, session: Session) -> str:
        return self.node.render_content(session)  # type: ignore[attr-defined]

    def patch(self, value: str) -> dict[str, Any]:
        return {"k": "h", "i": self.slot_id, "v": value}


class AttrSlot:
    __slots__ = ("elem_id", "attr", "evaluator", "key")

    def __init__(self, elem_id: int, attr: str, evaluator: Callable[[Session], Any]):
        self.elem_id = elem_id
        self.attr = attr
        self.evaluator = evaluator
        self.key = f"a{elem_id}:{attr}"

    def render_value(self, session: Session) -> Any:
        return self.evaluator(session)

    def patch(self, value: Any) -> dict[str, Any]:
        return {"k": "a", "e": self.elem_id, "a": self.attr, "v": value}


class Page:
    """A compiled page: static HTML document + dynamic slot table."""

    __slots__ = (
        "route",
        "title",
        "on_load",
        "root",
        "slots",
        "_text_counter",
        "_html_counter",
        "_elem_counter",
        "document",
    )

    def __init__(self, root: Component, route: str, title: str, ws_path: str, on_load: Any = None):
        self.route = route
        self.title = title
        self.on_load = on_load
        self.root = root
        self.slots: list[TextSlot | HtmlSlot | AttrSlot] = []
        self._text_counter = 0
        self._html_counter = 0
        self._elem_counter = 0

        root.compile(self, inline=False)
        initial = root.render(Session())
        self.document = self._build_document(initial, ws_path)

    # -- compile-time slot registration (called from Component.compile) ------
    def add_text_slot(self, node: TextNode) -> int:
        slot_id = self._text_counter
        self._text_counter += 1
        self.slots.append(TextSlot(slot_id, node))
        return slot_id

    def add_html_slot(self, node: Component) -> int:
        slot_id = self._html_counter
        self._html_counter += 1
        self.slots.append(HtmlSlot(slot_id, node))
        return slot_id

    def add_element(self, element: Element) -> int:
        elem_id = self._elem_counter
        self._elem_counter += 1
        for attr, value in element.dynamic_attrs:
            evaluator = value._ev if isinstance(value, Var) else value
            self.slots.append(AttrSlot(elem_id, attr, evaluator))
        return elem_id

    # -- request-time --------------------------------------------------------
    def prime(self, session: Session) -> None:
        """Fill the session's slot cache to match the initial document."""
        for slot in self.slots:
            session.cache[slot.key] = slot.render_value(session)

    def compute_patches(self, session: Session, force: bool = False) -> list[dict[str, Any]]:
        patches = []
        for slot in self.slots:
            value = slot.render_value(session)
            if force or session.cache.get(slot.key, _MISSING) != value:
                session.cache[slot.key] = value
                patches.append(slot.patch(value))
        return patches

    def _build_document(self, body: str, ws_path: str) -> str:
        boot = f"window.__RX_PAGE={json.dumps(self.route)};window.__RX_WS={json.dumps(ws_path)};"
        return (
            "<!doctype html><html><head>"
            '<meta charset="utf-8"/>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f"<title>{html.escape(self.title)}</title>"
            f"<style>{DEFAULT_CSS}</style>"
            "</head><body>"
            f"{body}"
            f"<script>{boot}\n{_RUNTIME_JS}</script>"
            "</body></html>"
        )
