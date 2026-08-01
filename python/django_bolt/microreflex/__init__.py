"""micro-reflex — a Reflex-compatible, Rust-powered mini web framework.

Proof of concept: Reflex's developer syntax (``rx.State``, component
functions, event handlers) running on django-bolt's Rust/Actix engine with a
dependency-free vanilla-JS runtime instead of React/Next.js.

Drop-in usage for small apps — only the import changes::

    import django_bolt.microreflex as rx

    class State(rx.State):
        count: int = 0

        def increment(self):
            self.count += 1

    def index():
        return rx.vstack(
            rx.heading(State.count),
            rx.button("Increment", on_click=State.increment),
        )

    app = rx.App()
    app.add_page(index)
    api = app.api  # picked up by `python manage.py runbolt` autodiscovery

See docs/MICROREFLEX.md for the full guide and the architecture notes.
"""

from __future__ import annotations

from .app import App
from .components import (
    Component,
    Element,
    badge,
    box,
    button,
    center,
    checkbox,
    code,
    cond,
    container,
    divider,
    el,
    foreach,
    form,
    fragment,
    heading,
    hstack,
    image,
    input,  # noqa: A004 - reflex-compatible name
    link,
    select,
    spacer,
    text,
    text_area,
    vstack,
)
from .state import EventRef, Session, State, event, var
from .vars import ConstVar, Var

__all__ = [
    "App",
    "Component",
    "ConstVar",
    "Element",
    "EventRef",
    "Session",
    "State",
    "Var",
    "badge",
    "box",
    "button",
    "center",
    "checkbox",
    "code",
    "cond",
    "container",
    "divider",
    "el",
    "event",
    "foreach",
    "form",
    "fragment",
    "heading",
    "hstack",
    "image",
    "input",
    "link",
    "select",
    "spacer",
    "text",
    "text_area",
    "var",
    "vstack",
]
