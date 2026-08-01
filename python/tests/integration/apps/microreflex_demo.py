"""micro-reflex demo app: a counter and a todo list in Reflex syntax.

Doubles as an in-process fixture (``TestClient(microreflex_demo.api)``) and a
runnable server app::

    uv run python -m django_bolt.testing.run  # or point runbolt at it via
    # settings.BOLT_API = "tests.integration.apps.microreflex_demo"

Everything below the import line is valid Reflex syntax — the point of the
PoC is that small Reflex apps can drop onto django-bolt's Rust engine by
changing only the import.
"""

from __future__ import annotations

import django_bolt.microreflex as rx


class DemoCounterState(rx.State):
    count: int = 0

    def increment(self):
        self.count += 1

    def decrement(self):
        self.count -= 1

    @rx.var
    def parity(self) -> str:
        return "even" if self.count % 2 == 0 else "odd"


class DemoTodoState(rx.State):
    items: list = ["ship micro-reflex"]
    new_item: str = ""

    def add_item(self):
        if self.new_item.strip():
            self.items.append(self.new_item.strip())
            self.new_item = ""

    def remove_item(self, index: int):
        self.items.pop(index)


def index():
    return rx.container(
        rx.vstack(
            rx.heading("micro-reflex on django-bolt", size="7"),
            rx.hstack(
                rx.button("-", on_click=DemoCounterState.decrement),
                rx.heading(DemoCounterState.count, size="6"),
                rx.button("+", on_click=DemoCounterState.increment),
                rx.badge(DemoCounterState.parity),
            ),
            rx.cond(
                DemoCounterState.count > 4,
                rx.text("That's a big number!"),
            ),
            rx.divider(),
            rx.hstack(
                rx.input(
                    value=DemoTodoState.new_item,
                    on_change=DemoTodoState.set_new_item,
                    placeholder="Add a todo...",
                ),
                rx.button("Add", on_click=DemoTodoState.add_item),
            ),
            rx.foreach(
                DemoTodoState.items,
                lambda item, i: rx.hstack(
                    rx.text(item),
                    rx.button("x", on_click=DemoTodoState.remove_item(i)),
                ),
            ),
            spacing="4",
        )
    )


app = rx.App()
app.add_page(index, title="micro-reflex demo")
api = app.api


@api.get("/health")
async def health():
    return {"status": "ok"}
