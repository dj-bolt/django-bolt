"""micro-reflex App — Reflex-compatible ``rx.App`` on django-bolt.

    import django_bolt.microreflex as rx

    class State(rx.State):
        count: int = 0

        def increment(self):
            self.count += 1

    def index():
        return rx.hstack(
            rx.button("Decrement", on_click=State.decrement),
            rx.heading(State.count),
            rx.button("Increment", on_click=State.increment),
        )

    app = rx.App()
    app.add_page(index)
    api = app.api  # expose for runbolt autodiscovery

Pages are served as pre-rendered HTML strings by trivially-async handlers
(Rust sync-dispatch fast path); interactivity flows over one WebSocket route
handled by django-bolt's Rust/Actix WebSocket stack.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from django_bolt import BoltAPI
from django_bolt.responses import HTML
from django_bolt.websocket import WebSocket

from .compiler import Page
from .components import normalize_child
from .state import STATE_REGISTRY, EventRef, Session
from .vars import _eval_maybe

logger = logging.getLogger("django_bolt.microreflex")


def _resolve_arg(arg: Any, session: Session) -> Any:
    """Resolve a handler-spec argument sent back by the client."""
    if isinstance(arg, dict) and "$s" in arg and "$f" in arg:
        state_cls = STATE_REGISTRY.get(arg["$s"])
        if state_cls is None or arg["$f"] not in state_cls._rx_fields:
            raise ValueError(f"Unknown state field reference: {arg!r}")
        return getattr(session.get(state_cls), arg["$f"])
    return arg


class App:
    """Reflex-compatible application object backed by a BoltAPI."""

    def __init__(self, api: BoltAPI | None = None, ws_path: str = "/_rx/ws", **_ignored: Any):
        self.api = api if api is not None else BoltAPI()
        self.ws_path = ws_path
        self._pages: dict[str, Page] = {}
        self._register_websocket()

    # ------------------------------------------------------------------ pages
    def add_page(
        self,
        component: Callable[[], Any],
        route: str | None = None,
        title: str | None = None,
        on_load: EventRef | list[EventRef] | None = None,
        **_ignored: Any,
    ) -> None:
        """Compile ``component()`` once and register its GET + event routes."""
        if route is None:
            name = component.__name__
            route = "/" if name == "index" else f"/{name}"
        if not route.startswith("/"):
            route = f"/{route}"
        if route in self._pages:
            raise ValueError(f"A page is already registered at route {route!r}")

        root = normalize_child(component())
        page = Page(
            root,
            route=route,
            title=title or component.__name__.replace("_", " ").title(),
            ws_path=self.ws_path,
            on_load=on_load,
        )
        self._pages[route] = page

        document = page.document

        async def page_handler() -> HTML:
            return HTML(document)

        page_handler.__name__ = f"rx_page_{component.__name__}"
        self.api.get(route)(page_handler)

    # -------------------------------------------------------------- websocket
    def _register_websocket(self) -> None:
        app = self

        @self.api.websocket(self.ws_path)
        async def microreflex_ws(websocket: WebSocket) -> None:
            page = app._pages.get(websocket.query_params.get("page", "/"))
            if page is None:
                await websocket.accept()
                await websocket.close(code=1008, reason="unknown page")
                return
            await websocket.accept()
            session = Session()
            page.prime(session)

            if page.on_load is not None:
                loads = page.on_load if isinstance(page.on_load, list) else [page.on_load]
                for ref in loads:
                    await app._run_event_ref(page, session, websocket, ref)

            async for message in websocket.iter_json():
                kind = message.get("t")
                if kind == "e":
                    await app._dispatch(page, session, websocket, message)
                elif kind == "sync":
                    patches = page.compute_patches(session, force=True)
                    await websocket.send_json({"t": "p", "p": patches})

    # --------------------------------------------------------------- dispatch
    async def _flush(self, page: Page, session: Session, websocket: WebSocket) -> None:
        patches = page.compute_patches(session)
        if patches:
            await websocket.send_json({"t": "p", "p": patches})

    async def _drive(self, result: Any, page: Page, session: Session, websocket: WebSocket) -> None:
        """Await/iterate a handler result, flushing patches at each yield."""
        if inspect.iscoroutine(result):
            result = await result
        if inspect.isasyncgen(result):
            async for _ in result:
                await self._flush(page, session, websocket)
        elif inspect.isgenerator(result):
            for _ in result:
                await self._flush(page, session, websocket)

    async def _run_event_ref(self, page: Page, session: Session, websocket: WebSocket, ref: EventRef) -> None:
        instance = session.get(ref.state_cls)
        fn = ref.state_cls._rx_handlers[ref.name]
        args = [_eval_maybe(a, session) for a in ref.args]
        result = fn(instance, *args)
        await self._drive(result, page, session, websocket)
        await self._flush(page, session, websocket)

    async def _dispatch(self, page: Page, session: Session, websocket: WebSocket, message: dict[str, Any]) -> None:
        spec = message.get("h") or {}
        try:
            state_cls = STATE_REGISTRY.get(spec.get("s", ""))
            if state_cls is None:
                raise ValueError(f"Unknown state class: {spec.get('s')!r}")
            name = spec.get("n", "")
            args = [_resolve_arg(a, session) for a in spec.get("a", [])]
            if "v" in message:
                args.append(message["v"])

            instance = session.get(state_cls)
            fn = state_cls._rx_handlers.get(name)
            if fn is not None:
                result = fn(instance, *args)
                await self._drive(result, page, session, websocket)
            elif name.startswith("set_") and name[4:] in state_cls._rx_fields:
                if not args:
                    raise ValueError(f"Setter {name} called without a value")
                setattr(instance, name[4:], args[0])
            else:
                raise ValueError(f"Unknown event handler: {spec.get('s')}.{name}")
        except Exception as exc:
            logger.exception("micro-reflex event dispatch failed: %s", spec)
            await websocket.send_json({"t": "err", "m": f"{type(exc).__name__}: {exc}"})
            return
        await self._flush(page, session, websocket)
