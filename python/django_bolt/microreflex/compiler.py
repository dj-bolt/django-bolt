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

from django_bolt import _core

from .state import Session
from .vars import Var

if TYPE_CHECKING:
    from .components import Component, Element, TextNode

_RUNTIME_JS = (Path(__file__).parent / "runtime.js").read_text(encoding="utf-8")

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
    __slots__ = ("slot_id", "node")

    def __init__(self, slot_id: int, node: TextNode):
        self.slot_id = slot_id
        self.node = node

    def render_value(self, session: Session) -> str:
        return self.node.render_text(session)

    def rx_spec(self) -> tuple[int, int, str | None, int]:
        return (0, self.slot_id, None, 0)


class HtmlSlot:
    __slots__ = ("slot_id", "node")

    def __init__(self, slot_id: int, node: Component):
        self.slot_id = slot_id
        self.node = node

    def render_value(self, session: Session) -> str:
        return self.node.render_content(session)  # type: ignore[attr-defined]

    def rx_spec(self) -> tuple[int, int, str | None, int]:
        return (1, self.slot_id, None, 0)


class AttrSlot:
    __slots__ = ("elem_id", "attr", "evaluator")

    def __init__(self, elem_id: int, attr: str, evaluator: Callable[[Session], Any]):
        self.elem_id = elem_id
        self.attr = attr
        self.evaluator = evaluator

    def render_value(self, session: Session) -> Any:
        return self.evaluator(session)

    def rx_spec(self) -> tuple[int, int, str | None, int]:
        return (2, 0, self.attr, self.elem_id)


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
        "_evaluators",
        "_rx_page",
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
        # Registration-time precompute: the flat evaluator list Python runs
        # per event, and the Rust slot table (patch prefixes + cache layout).
        self._evaluators = [slot.render_value for slot in self.slots]
        self._rx_page = _core.RxPage([slot.rx_spec() for slot in self.slots])
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
        """Attach a Rust render cache filled to match the initial document."""
        session.rx = self._rx_page.new_session()
        session.rx.prime([evaluator(session) for evaluator in self._evaluators])

    def compute_patch_frame(self, session: Session, force: bool = False) -> str | None:
        """Diff and encode in Rust: returns the complete `{"t":"p","p":[...]}`
        wire frame, or None when no slot changed."""
        return session.rx.diff([evaluator(session) for evaluator in self._evaluators], force)

    def compute_patches(self, session: Session, force: bool = False) -> list[dict[str, Any]]:
        """Patch list view of the Rust-encoded frame (tests/introspection)."""
        frame = self.compute_patch_frame(session, force)
        if frame is None:
            return []
        return json.loads(frame)["p"]

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
