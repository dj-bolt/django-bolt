"""Tests for micro-reflex — Reflex-compatible mini framework on django-bolt.

Covers the three layers:
- state/vars: Var evaluation, computed vars, auto-setters, session isolation
- compile/render: slot markers, escaping, cond/foreach, event wiring
- transport: page GET via TestClient, event round-trips via WebSocketTestClient
"""

from __future__ import annotations

import json
import time

import pytest

import django_bolt.microreflex as rx
from django_bolt import _core
from django_bolt.microreflex.compiler import Page
from django_bolt.microreflex.components import normalize_child
from django_bolt.microreflex.state import Session
from django_bolt.testing import TestClient, WebSocketTestClient
from tests.integration.apps import microreflex_demo

# ---------------------------------------------------------------------------
# State & vars
# ---------------------------------------------------------------------------


class SimpleState(rx.State):
    count: int = 0
    name: str = "bolt"
    items: list = ["a"]

    def increment(self):
        self.count += 1

    def add(self, amount: int):
        self.count += amount

    @rx.var
    def double(self) -> int:
        return self.count * 2


def test_state_defaults_and_isolation():
    first, second = SimpleState(), SimpleState()
    assert first.count == 0
    assert first.items == ["a"]
    first.items.append("b")
    assert second.items == ["a"], "mutable defaults must be copied per instance"


def test_class_access_returns_var_and_instance_access_returns_value():
    session = Session()
    var = SimpleState.count
    assert isinstance(var, rx.Var)
    assert var._ev(session) == 0
    session.get(SimpleState).count = 5
    assert var._ev(session) == 5


def test_computed_var():
    session = Session()
    session.get(SimpleState).count = 3
    assert SimpleState.double._ev(session) == 6
    assert session.get(SimpleState).double == 6


def test_var_operators():
    session = Session()
    session.get(SimpleState).count = 4
    assert (SimpleState.count + 1)._ev(session) == 5
    assert (SimpleState.count > 3)._ev(session) is True
    assert (~(SimpleState.count > 3))._ev(session) is False
    assert SimpleState.name.upper()._ev(session) == "BOLT"
    assert SimpleState.items.length()._ev(session) == 1
    assert SimpleState.items[0]._ev(session) == "a"


def test_var_in_if_raises():
    with pytest.raises(TypeError, match="rx.cond"):
        if SimpleState.count:  # noqa: B015
            pass


def test_auto_setter_and_explicit_handler():
    setter = SimpleState.set_name
    assert isinstance(setter, rx.EventRef)
    assert setter.name == "set_name"
    handler = SimpleState.increment
    assert isinstance(handler, rx.EventRef)
    partial = SimpleState.add(5)
    assert partial.args == (5,)


def test_unknown_class_attribute_raises():
    with pytest.raises(AttributeError):
        SimpleState.missing_thing  # noqa: B018


# ---------------------------------------------------------------------------
# Components & rendering
# ---------------------------------------------------------------------------


class RenderState(rx.State):
    message: str = "hello <world>"
    on: bool = False
    todos: list = ["one", "two"]

    def toggle(self):
        self.on = not self.on


def _compile(component):
    """Compile a component into a Page the way App.add_page does."""
    return Page(normalize_child(component), route="/t", title="t", ws_path="/_rx/ws")


def test_static_render():
    page = _compile(rx.box(rx.text("plain"), class_name="wrap"))
    assert '<div class="wrap"><p>plain</p></div>' in page.document


def test_text_binding_renders_marker_and_escapes():
    page = _compile(rx.text(RenderState.message))
    assert '<span data-rx-t="0">hello &lt;world&gt;</span>' in page.document


def test_fstring_binding():
    page = _compile(rx.text(f"Message: {RenderState.message}!"))
    assert "Message: hello &lt;world&gt;!" in page.document


def test_cond_initial_render():
    page = _compile(rx.cond(RenderState.on, rx.text("yes"), rx.text("no")))
    assert 'data-rx-h="0"' in page.document
    assert "<p>no</p>" in page.document
    assert "<p>yes</p>" not in page.document


def test_foreach_initial_render():
    page = _compile(rx.foreach(RenderState.todos, lambda item, i: rx.text(f"{i}:{item}")))
    assert "0:one" in page.document
    assert "1:two" in page.document


def test_dynamic_attr_and_event_wiring():
    page = _compile(rx.input(value=RenderState.message, on_change=RenderState.set_message))
    doc = page.document
    assert 'data-rx-e="0"' in doc
    assert 'value="hello &lt;world&gt;"' in doc
    assert "data-rx-on=" in doc
    assert "set_message" in doc


def test_patch_diffing():
    page = _compile(
        rx.vstack(
            rx.text(RenderState.message),
            rx.cond(RenderState.on, rx.text("yes"), rx.text("no")),
        )
    )
    session = Session()
    page.prime(session)
    assert page.compute_patches(session) == []

    session.get(RenderState).on = True
    patches = page.compute_patches(session)
    assert len(patches) == 1
    assert patches[0]["k"] == "h"
    assert "<p>yes</p>" in patches[0]["v"]
    # Diff is against last-sent values: no repeat patch without a change.
    assert page.compute_patches(session) == []
    # force=True resends everything (used for client resync after reconnect).
    assert len(page.compute_patches(session, force=True)) == 2


# ---------------------------------------------------------------------------
# Page serving (TestClient through the Rust HTTP pipeline)
# ---------------------------------------------------------------------------


def test_page_get_serves_prerendered_html():
    with TestClient(microreflex_demo.api) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "micro-reflex on django-bolt" in body
        assert "data-rx-t=" in body  # bound counter text
        assert "data-rx-on=" in body  # wired events
        assert "WebSocket" in body  # inlined JS runtime
        assert "ship micro-reflex" in body  # initial foreach item

        health = client.get("/health")
        assert health.status_code == 200


# ---------------------------------------------------------------------------
# Event round-trips (WebSocketTestClient through the Rust WS pipeline)
# ---------------------------------------------------------------------------


def _increment_msg():
    return {"t": "e", "h": {"s": "DemoCounterState", "n": "increment"}}


@pytest.mark.asyncio
async def test_ws_event_updates_state_and_patches():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        await ws.send_json(_increment_msg())
        msg = await ws.receive_json()
        assert msg["t"] == "p"
        texts = {p["i"]: p["v"] for p in msg["p"] if p["k"] == "t"}
        assert "1" in texts.values()
        assert "odd" in texts.values()  # computed var updated in same patch


@pytest.mark.asyncio
async def test_ws_setter_and_foreach_patch():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        await ws.send_json({"t": "e", "h": {"s": "DemoTodoState", "n": "set_new_item"}, "v": "write tests"})
        msg = await ws.receive_json()  # value attr patch for the input
        values = [p for p in msg["p"] if p["k"] == "a" and p["a"] == "value"]
        assert values and values[0]["v"] == "write tests"

        await ws.send_json({"t": "e", "h": {"s": "DemoTodoState", "n": "add_item"}})
        msg = await ws.receive_json()
        html_patches = [p for p in msg["p"] if p["k"] == "h"]
        assert html_patches and "write tests" in html_patches[0]["v"]


@pytest.mark.asyncio
async def test_ws_event_with_baked_args():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        await ws.send_json({"t": "e", "h": {"s": "DemoTodoState", "n": "remove_item", "a": [0]}})
        msg = await ws.receive_json()
        html_patches = [p for p in msg["p"] if p["k"] == "h"]
        assert html_patches and "ship micro-reflex" not in html_patches[0]["v"]


@pytest.mark.asyncio
async def test_ws_sessions_are_isolated():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as first:
        await first.send_json(_increment_msg())
        await first.receive_json()
        async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as second:
            await second.send_json(_increment_msg())
            msg = await second.receive_json()
            texts = {p["v"] for p in msg["p"] if p["k"] == "t"}
            assert "1" in texts, "second session must start from default state"


@pytest.mark.asyncio
async def test_ws_unknown_handler_reports_error():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        await ws.send_json({"t": "e", "h": {"s": "DemoCounterState", "n": "nope"}})
        msg = await ws.receive_json()
        assert msg["t"] == "err"
        assert "nope" in msg["m"]


@pytest.mark.asyncio
async def test_ws_sync_resends_all_slots():
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        await ws.send_json({"t": "sync"})
        msg = await ws.receive_json()
        assert msg["t"] == "p"
        assert len(msg["p"]) >= 4


class StreamState(rx.State):
    progress: int = 0

    def run(self):
        for step in (25, 50, 100):
            self.progress = step
            yield


def _stream_app():
    app = rx.App()
    app.add_page(lambda: rx.text(StreamState.progress), route="/stream")
    return app


@pytest.mark.asyncio
async def test_ws_generator_handler_streams_patches():
    app = _stream_app()
    async with WebSocketTestClient(app.api, "/_rx/ws", query_string="page=/stream") as ws:
        await ws.send_json({"t": "e", "h": {"s": "StreamState", "n": "run"}})
        seen = [
            (await ws.receive_json())["p"][0]["v"],
            (await ws.receive_json())["p"][0]["v"],
            (await ws.receive_json())["p"][0]["v"],
        ]
        assert seen == ["25", "50", "100"]


@pytest.mark.asyncio
async def test_ws_event_roundtrip_throughput_smoke():
    """Not a benchmark gate — prints indicative in-process round-trip rate."""
    rounds = 300
    async with WebSocketTestClient(microreflex_demo.api, "/_rx/ws", query_string="page=/") as ws:
        start = time.perf_counter()
        for _ in range(rounds):
            await ws.send_json(_increment_msg())
            msg = await ws.receive_json()
            assert msg["t"] == "p"
        elapsed = time.perf_counter() - start
    rate = rounds / elapsed
    print(f"\nmicro-reflex event round-trips: {rate:,.0f}/s ({elapsed * 1e6 / rounds:.0f}µs each)")
    assert rate > 50, "event dispatch should comfortably exceed 50 round-trips/s"


# ---------------------------------------------------------------------------
# Rust-side state synchronization (session cache + diff + patch encoding)
# ---------------------------------------------------------------------------


def test_rust_diff_engine_is_engaged():
    """State sync must run in Rust: pages compile to a _core.RxPage and
    sessions hold a _core.RxSession render cache."""
    page = _compile(rx.text(RenderState.message))
    assert isinstance(page._rx_page, _core.RxPage)
    session = Session()
    page.prime(session)
    assert isinstance(session.rx, _core.RxSession)


def test_rust_patch_frame_encoding():
    """The Rust differ emits the complete wire frame with correct JSON
    escaping, and only for slots that changed."""
    page = _compile(
        rx.vstack(
            rx.text(RenderState.message),
            rx.cond(RenderState.on, rx.text("yes"), rx.text("no")),
        )
    )
    session = Session()
    page.prime(session)
    assert page.compute_patch_frame(session) is None

    session.get(RenderState).message = 'quote " and <tag>'
    frame = page.compute_patch_frame(session)
    assert isinstance(frame, str)
    parsed = json.loads(frame)
    assert parsed == {"t": "p", "p": [{"k": "t", "i": 0, "v": 'quote " and <tag>'}]}
    # unchanged on repeat, everything on force
    assert page.compute_patch_frame(session) is None
    assert len(json.loads(page.compute_patch_frame(session, force=True))["p"]) == 2


def test_rust_attr_patch_value_types():
    """Attr slot values keep their JSON types (bool/None/str) through Rust."""

    class AttrState(rx.State):
        text: str = ""
        busy: bool = False

    page = _compile(rx.input(value=AttrState.text, disabled=AttrState.busy))
    session = Session()
    page.prime(session)
    state = session.get(AttrState)
    state.busy = True
    state.text = "go"
    parsed = json.loads(page.compute_patch_frame(session))
    by_attr = {p["a"]: p["v"] for p in parsed["p"]}
    assert by_attr == {"value": "go", "disabled": True}
