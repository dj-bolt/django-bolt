# micro-reflex — Reflex Syntax on the Rust Engine (Proof of Concept)

`django_bolt.microreflex` is a proof of concept answering one question: **how
fast could a Reflex-style framework be if it were built on Rust?** It runs
Reflex's developer syntax — `rx.State`, component functions, event handlers —
directly on django-bolt's Actix/Rust engine, and replaces the React/Next.js
frontend with **pure HTML plus ~180 lines of dependency-free vanilla JS**.

For small apps, only the import changes:

```python
import django_bolt.microreflex as rx   # instead of: import reflex as rx


class State(rx.State):
    count: int = 0

    def increment(self):
        self.count += 1

    def decrement(self):
        self.count -= 1


def index():
    return rx.hstack(
        rx.button("Decrement", on_click=State.decrement),
        rx.heading(State.count),
        rx.button("Increment", on_click=State.increment),
    )


app = rx.App()
app.add_page(index)
api = app.api  # picked up by `python manage.py runbolt` autodiscovery
```

Put that in an `api.py` that `runbolt` discovers (or point `settings.BOLT_API`
at the module) and run `python manage.py runbolt`.

## How it works

micro-reflex is server-driven (in the spirit of Phoenix LiveView), not a
compile-to-React pipeline:

```
Browser                          django-bolt (Rust)            Python
───────                          ──────────────────            ──────
GET /            ──────────────▶ Actix + matchit router ─────▶ returns pre-rendered
                                 (sync-dispatch fast path)     HTML string (constant)

click/input      ──WebSocket───▶ Actix WS + Rust routing ────▶ run event handler,
                                                               re-eval dynamic slots,
DOM patch        ◀──────────────  patch frame (JSON)  ◀─────── diff, send changes
```

- **Compile once, serve forever.** `app.add_page()` builds the component tree
  once, assigns stable ids to every dynamic slot, and pre-renders the full
  HTML document into a plain string. The page GET handler is a trivially-async
  function returning that constant — it rides django-bolt's Rust
  sync-dispatch fast path, so pages serve at plain-endpoint speed (60k+ RPS
  territory) instead of running SSR per request.
- **Events over one WebSocket.** Elements carry
  `data-rx-on='[["click",{"s":"State","n":"increment"}]]'`. The JS runtime
  posts events; the server runs the handler on the connection's private state,
  re-evaluates dynamic slots, diffs against what that client last saw, and
  sends minimal patches (`textContent`, `innerHTML`, or attribute updates).
- **No JS expression compiler.** Because rendering is server-side, a var is
  just a lazily-evaluated Python function — `State.count * 2`,
  `State.text.strip().upper()`, f-strings, and computed vars all work
  without transpilation.

## Supported Reflex surface

- `rx.State` with typed vars, event handlers (sync, `async`, and
  generator/`yield` streaming), `@rx.var` computed vars, `@rx.event`
  (no-op marker), auto-generated `State.set_<field>` setters, partial
  argument application (`State.remove_item(i)`).
- Components: `vstack` `hstack` `center` `container` `box` `text` `heading`
  `button` `input` `text_area` `checkbox` `select` `form` `link` `image`
  `code` `badge` `divider` `spacer` `fragment`, plus `rx.el.<any_tag>` for
  arbitrary HTML.
- `rx.cond(cond, then, otherwise)` and `rx.foreach(State.items, render_fn)`
  (with optional index argument).
- Event props: `on_click`, `on_change` (debounced live input; `change` for
  select/checkbox/radio), `on_input`, `on_blur`, `on_focus`, `on_submit`
  (sends the form's fields as a dict), `on_key_down`/`on_key_up` (send
  `event.key`), `on_mouse_enter`/`on_mouse_leave`, `on_double_click`.
- `app.add_page(component, route=..., title=..., on_load=...)`.
- Multiple `rx.State` subclasses per app; per-connection (per-tab) state
  isolation; automatic resync after WebSocket reconnect.

### Knowingly out of scope (PoC)

Client-side-only interactivity (everything round-trips), Reflex's styling
system/themes (use `style={...}` dicts and `class_name`), state persistence
across reconnects, routing with path parameters in pages, `rx.markdown`,
plugins, and the Reflex compiler/deploy toolchain. Event-handler arguments
must be JSON-serializable constants, foreach items, or plain state fields.

## Performance notes

Numbers from the in-process test harness on the CI container
(`python/tests/test_microreflex.py`, printed by the throughput smoke test):

- **Page GET**: a pre-rendered string through the Rust sync-dispatch path —
  same cost as any static django-bolt HTML endpoint.
- **Event round-trip** (event → handler → diff → patch): ~90µs each,
  ~11,000/s on a single connection, through the real Rust WebSocket pipeline.

For comparison, a stock Reflex event round-trip crosses a Node/Next.js
frontend, a socket.io layer, and a Python backend that serializes full state
deltas — typically single-digit milliseconds at best. The PoC point: with
routing, HTTP, WebSocket framing, and middleware in Rust, the per-event
budget is dominated by the user's handler, not the framework.

Run it yourself:

```bash
uv run --with pytest --with pytest-asyncio pytest \
    python/tests/test_microreflex.py::test_ws_event_roundtrip_throughput_smoke -s
```

## Demo app

`python/tests/integration/apps/microreflex_demo.py` is a complete counter +
todo app (also used by the test suite). To serve it for real:

```bash
cd python/example
DJANGO_SETTINGS_MODULE=... python manage.py runbolt  # with BOLT_API pointing at the module
```

or drop its contents into any autodiscovered `api.py`.

## Architecture map

| Piece | File | Role |
| --- | --- | --- |
| Vars | `python/django_bolt/microreflex/vars.py` | Lazy state references, operator composition, f-string capture |
| State | `python/django_bolt/microreflex/state.py` | `rx.State` metaclass, event refs, computed vars, sessions |
| Components | `python/django_bolt/microreflex/components.py` | Element tree, `cond`/`foreach`, Reflex-named factories |
| Compiler | `python/django_bolt/microreflex/compiler.py` | One-time page compile, slot table, diffing, HTML document |
| App | `python/django_bolt/microreflex/app.py` | `rx.App`, page routes, WebSocket event dispatch |
| JS runtime | `python/django_bolt/microreflex/runtime.js` | Event delegation, patch application, reconnect/resync |
