"""The script prefix (``FORCE_SCRIPT_NAME``) for the code of a request.

Django keeps the prefix of ``reverse()`` in an asgiref ``Local``. A value is
visible only in the context that set it. From asgiref 3.13, it is also
visible only on the thread that set it. Bolt sets the prefix at two kinds of
points:

- An entry: each call from Rust into Python for a request or a WebSocket. It
  sets the prefix of the server in a context that ends with the call: the
  task of an async call, or a copy of the context for a sync call.
- A carry: each move of request work to another thread or to a new context.
  It sets the prefix of the caller there, also in a context that ends with
  the work. It never sets a prefix that the caller does not have.

Thus a prefix is visible only to the work of a request of a server with the
setting. The server picks the entries at startup. Without the setting, the
code of each request is the same as before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, copy_context
from functools import partial, wraps
from typing import Any

from django.conf import settings
from django.urls import get_script_prefix, set_script_prefix

# True after a server or a test client with FORCE_SCRIPT_NAME started in this
# process. The carries run only then. A carry copies the prefix of the caller
# only when it is not "/", so a later client without the setting gets no change.
carries = False


def configure() -> str | None:
    """Read ``FORCE_SCRIPT_NAME`` at the start of a server or a test client.

    Returns:
        The script prefix for the entries, as ``get_script_prefix()`` gives it,
        or None when the setting adds no prefix.
    """
    global carries
    script_name = (settings.FORCE_SCRIPT_NAME or "").rstrip("/")
    if not script_name:
        return None
    carries = True
    return script_name + "/"


def _call(script_prefix: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    set_script_prefix(script_prefix)
    return fn(*args, **kwargs)


def sync_entry[F: Callable[..., Any]](fn: F, script_prefix: str | None) -> F:
    """Return ``fn``, or ``fn`` that runs in a copy of the context with the script prefix."""
    if script_prefix is None:
        return fn

    def entry(*args: Any, **kwargs: Any) -> Any:
        return copy_context().run(_call, script_prefix, fn, *args, **kwargs)

    return entry  # type: ignore[return-value]


def async_entry[F: Callable[..., Any]](fn: F, script_prefix: str | None) -> F:
    """Return ``fn``, or ``fn`` that sets the script prefix in its own task first."""
    if script_prefix is None:
        return fn

    @wraps(fn)
    async def entry(*args: Any, **kwargs: Any) -> Any:
        set_script_prefix(script_prefix)
        return await fn(*args, **kwargs)

    return entry  # type: ignore[return-value]


def mcp_mounts(mounts: list[dict[str, Any]], script_prefix: str | None) -> list[dict[str, Any]]:
    """Return the MCP mounts, with tools, resources and prompts that run with the script prefix."""
    if script_prefix is None:
        return mounts
    return [
        {
            **mount,
            "tools": {
                name: {**tool, "dispatch": async_entry(tool["dispatch"], script_prefix)}
                for name, tool in mount["tools"].items()
            },
            "resource_dispatch": async_entry(mount["resource_dispatch"], script_prefix),
            "prompt_dispatch": async_entry(mount["prompt_dispatch"], script_prefix),
        }
        for mount in mounts
    ]


@contextmanager
def lifespan_entry(script_prefix: str | None) -> Iterator[None]:
    """Give the code in the block the script prefix, as runbolt gives it to a lifespan.

    ``django.setup()`` gives the main thread of runbolt the prefix. The block
    gets it, and the context of the caller gets its own prefix back after it.
    """
    if script_prefix is None:
        yield
        return
    previous = get_script_prefix()
    set_script_prefix(script_prefix)
    try:
        yield
    finally:
        set_script_prefix(previous)


# --- Carries ------------------------------------------------------------------


def _caller_prefix() -> str | None:
    """The script prefix of the caller, or None when it has none to carry."""
    if not carries:
        return None
    script_prefix = get_script_prefix()
    return None if script_prefix == "/" else script_prefix


def carry(call: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``call``, with the script prefix of the caller set first on the thread that runs it.

    Read before a hop to a thread pool, on the thread of the caller. The pool
    runs ``call`` in a copy of the context of the caller. With asgiref 3.13,
    the copy does not show a prefix that another thread set.
    """
    script_prefix = _caller_prefix()
    if script_prefix is None:
        return call
    return partial(_call, script_prefix, call)


def carry_into(context: Context) -> Context:
    """Set the script prefix of the caller in ``context``, a new context for the work of a request."""
    script_prefix = _caller_prefix()
    if script_prefix is not None:
        context.run(set_script_prefix, script_prefix)
    return context


def carry_into_body(content: Any, is_async: bool) -> Any:
    """Return the body of a streaming response with the script prefix of the caller.

    Rust iterates a body after the handler returned, out of the context of
    the request: an async body in a task of its own, a sync body on a thread
    of its own. The body that this returns iterates ``content`` as Rust does.
    """
    script_prefix = _caller_prefix()
    if script_prefix is None:
        return content
    if is_async:
        return _async_body(content, script_prefix)
    return _sync_body(content, script_prefix)


async def _async_body(content: Any, script_prefix: str) -> Any:
    # The task of the body has a context of its own.
    set_script_prefix(script_prefix)
    iterator = content.__aiter__() if hasattr(content, "__aiter__") else content
    try:
        while True:
            try:
                item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def _sync_body(content: Any, script_prefix: str) -> Any:
    # Each step runs in a context of its own, whatever thread iterates the body.
    context = copy_context()
    context.run(set_script_prefix, script_prefix)
    # Rust calls the __next__ and __iter__ attributes, not the methods of the type.
    iterator = content if hasattr(content, "__next__") else content.__iter__()
    step = iterator.__next__
    try:
        while True:
            try:
                item = context.run(step)
            except StopIteration:
                return
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            context.run(close)
