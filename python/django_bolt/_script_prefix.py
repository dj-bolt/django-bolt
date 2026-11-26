"""The script prefix (``FORCE_SCRIPT_NAME``) for the code of a request.

Django keeps the prefix of ``reverse()`` in an asgiref ``Local``. A value is
visible only in the context that set it. From asgiref 3.13, it is also
visible only on the thread that set it. Bolt sets the prefix at two kinds of
points:

- An entry: each call from Rust into Python for a request, a WebSocket, an MCP
  call or an ASGI mount. A sync entry sets the prefix on its thread, and puts
  the previous value back when the call returns. A sync call does not suspend,
  so no other work on the thread sees the prefix. An async entry sets the
  prefix in the task of the call.
- A hop: each move of request work to a new context, on a pool thread or on a
  request lane. The new context gets the prefix of the caller. A hop never
  sets a prefix that the caller does not have.

An entry does not copy the context. Thus the other state of a thread, such as
the cache backends of Django, stays on the thread, as without the setting.

The server picks the entries at startup. ``configure()`` picks the hops when the
first server or client with the setting starts. Without the setting, the code of
each request is the same as before.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import clear_script_prefix, get_script_prefix, set_script_prefix
from django.utils.encoding import escape_uri_path

# The context of a hop: copy_context() for a pool thread, new_context() for a
# request lane. Until a server or a client with the setting starts, they are the
# functions of contextvars, so each hop is the same as before.
copy_context: Callable[[], Any] = contextvars.copy_context
new_context: Callable[[], Any] = contextvars.Context


def configure() -> str | None:
    """Read ``FORCE_SCRIPT_NAME`` at the start of a server or a test client.

    Returns:
        The script prefix for the entries, as ``get_script_prefix()`` gives it,
        or None when the setting adds no prefix.

    Raises:
        ImproperlyConfigured: The setting has a character that Django
            percent-encodes in a URL. For a value that is not a str, Django's
            ``django.setup()`` raises first, in runbolt; a test client raises
            this error under ``override_settings``.
    """
    script_name = settings.FORCE_SCRIPT_NAME
    if script_name is None:
        return None
    if not isinstance(script_name, str):
        raise ImproperlyConfigured(f"FORCE_SCRIPT_NAME must be a str or None, not {type(script_name).__name__}.")
    if escape_uri_path(script_name) != script_name:
        raise ImproperlyConfigured(
            f"FORCE_SCRIPT_NAME {script_name!r} has a character that a URL path must percent-encode. "
            "Bolt puts the script name in request paths and redirects as it is. "
            "Use ASCII letters, digits and the characters /:@&+$,-_.!~*'()."
        )
    # The prefix that Django's set_script_prefix() makes of the setting.
    script_prefix = script_name if script_name.endswith("/") else script_name + "/"
    if script_prefix == "/":
        return None
    _carry_in_hops()
    return script_prefix


def refuse_files_at_script_name(script_prefix: str | None, *, static_files_replaced: bool = False) -> None:
    """Refuse a ``STATIC_URL`` or ``MEDIA_URL`` that is the script name, where Bolt serves those files.

    The proxy removes the script name, so Bolt would serve the files at "/",
    and that hides each route. Django also gives a relative value the script
    prefix. The default ``MEDIA_URL`` "" becomes the script name in this way,
    and Bolt serves no files for it, as without the setting. Thus only the
    value in the settings module counts. Call this before a lifespan or the
    startup banner.

    Args:
        script_prefix: The script prefix of ``configure()``.
        static_files_replaced: True when a test client got a ``static_files_config``.
            That config replaces ``STATIC_URL``, so only ``MEDIA_URL`` counts.

    Raises:
        ImproperlyConfigured: ``STATIC_URL`` or ``MEDIA_URL`` is the script
            name with a slash, and Bolt serves those files.
    """
    if script_prefix is None:
        return
    script_name = script_prefix.rstrip("/")
    if not script_name:
        return
    serves = {
        "STATIC_URL": lambda: bool(settings.DEBUG) or _is_path(settings.STATIC_ROOT) or bool(settings.STATICFILES_DIRS),
        "MEDIA_URL": lambda: _is_path(settings.MEDIA_ROOT),
    }
    if static_files_replaced:
        del serves["STATIC_URL"]
    for name, serves_files in serves.items():
        # settings.STATIC_URL has the script prefix in front of a relative
        # value. The wrapped settings give the value as the user wrote it.
        written = getattr(settings._wrapped, name)
        if (
            isinstance(written, str)
            and written != script_name
            and written.rstrip("/") == script_name
            and serves_files()
        ):
            raise ImproperlyConfigured(
                f'{name} "{written}" is FORCE_SCRIPT_NAME "{settings.FORCE_SCRIPT_NAME}" with a slash. '
                'The proxy removes the script name, so Bolt would serve the files at "/", and that hides each route. '
                f'Give the files a path of their own, for example {name} = "files/".'
            )


def _is_path(value: Any) -> bool:
    """Whether a ``STATIC_ROOT`` or ``MEDIA_ROOT`` names a directory, as Rust reads it."""
    return value is not None and str(value) != ""


def _carry_in_hops() -> None:
    """Give each hop of request work the script prefix of its caller, from now on."""
    global copy_context, new_context
    if copy_context is _copy_context_with_prefix:
        return
    copy_context = _copy_context_with_prefix
    new_context = _new_context_with_prefix
    # django_adapter imports this module through concurrency.
    from .middleware import django_adapter  # noqa: PLC0415

    django_adapter.use_script_name()


class _PrefixedContext:
    """A context that runs work with a script prefix, on any thread."""

    __slots__ = ("_context", "_script_prefix")

    def __init__(self, context: contextvars.Context, script_prefix: str) -> None:
        self._context = context
        self._script_prefix = script_prefix

    def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return self._context.run(_call, self._script_prefix, fn, *args, **kwargs)


def _call(script_prefix: str, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    # The thread that runs the work sets the prefix. With asgiref 3.13, a value
    # that another thread set is not visible here.
    set_script_prefix(script_prefix)
    return fn(*args, **kwargs)


def _copy_context_with_prefix() -> Any:
    context = contextvars.copy_context()
    script_prefix = get_script_prefix()
    return context if script_prefix == "/" else _PrefixedContext(context, script_prefix)


def _new_context_with_prefix() -> Any:
    script_prefix = get_script_prefix()
    context = contextvars.Context()
    return context if script_prefix == "/" else _PrefixedContext(context, script_prefix)


# --- Entries ------------------------------------------------------------------


def restore(previous: str) -> None:
    """Put back the script prefix that ``get_script_prefix()`` gave before an entry."""
    if previous == "/":
        clear_script_prefix()
    else:
        set_script_prefix(previous)


def sync_entry[F: Callable[..., Any]](fn: F, script_prefix: str | None) -> F:
    """Return ``fn``, or ``fn`` that runs with the script prefix on its thread."""
    if script_prefix is None:
        return fn

    def entry(*args: Any, **kwargs: Any) -> Any:
        previous = get_script_prefix()
        set_script_prefix(script_prefix)
        try:
            return fn(*args, **kwargs)
        finally:
            restore(previous)

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


def asgi_mounts(mounts: list[tuple[str, Any]], script_prefix: str | None) -> list[tuple[str, Any]]:
    """Return the ASGI mounts, with apps that run with the script prefix.

    The app of a request runs in a task of its own, so the prefix ends with it.
    """
    if script_prefix is None:
        return list(mounts)
    return [(path, _asgi_entry(app, script_prefix)) for path, app in mounts]


def _asgi_entry(app: Any, script_prefix: str) -> Callable[..., Any]:
    async def entry(scope: Any, receive: Any, send: Any) -> Any:
        set_script_prefix(script_prefix)
        return await app(scope, receive, send)

    # Rust reads this mark from the app. See BoltAPI.mount_django().
    if getattr(app, "_bolt_http_only", False):
        entry._bolt_http_only = True  # type: ignore[attr-defined]
    return entry


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
        restore(previous)


# --- Streaming bodies ---------------------------------------------------------


def stream_body(content: Any, is_async: bool, script_prefix: str) -> Any:
    """Return the body of a streaming response that iterates ``content`` with the script prefix.

    Rust iterates a body after the entry returned: an async body in a task of
    its own, a sync body on a new thread of its own. The body that this returns
    iterates ``content`` as Rust does.
    """
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
    # Each step runs in one context of the body, whatever thread iterates it.
    # Rust gives a sync body a new thread, so the context is as new as the thread.
    context = contextvars.copy_context()
    context.run(set_script_prefix, script_prefix)
    # As Rust does: read the __iter__ and __next__ attributes, not the methods of
    # the type, and read __next__ again on each step.
    iterator = content if hasattr(content, "__next__") else context.run(content.__iter__)
    try:
        while True:
            try:
                item = context.run(iterator.__next__)
            except StopIteration:
                return
            if type(item) is not bytes and type(item) is not str:
                # Rust would read the chunk after the step, with no prefix. A lazy
                # object, such as reverse_lazy(), gives its text only then.
                item = context.run(_read_chunk, item)
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            context.run(close)


def _read_chunk(item: Any) -> Any:
    """Read ``item`` as Rust reads a chunk, in the order of ``convert_python_chunk``.

    Rust takes bytes, bytearray, str and memoryview with no call into Python.
    It takes the bytes of ``__bytes__()``. When that call gives no bytes, as for
    a ``SimpleLazyObject`` of a str, the text of the object is the chunk.
    """
    if isinstance(item, (bytes, bytearray, str, memoryview)):
        return item
    if hasattr(item, "__bytes__"):
        try:
            buffer = item.__bytes__()
        except Exception:  # Rust reads the text of the chunk after each error of __bytes__().
            return str(item)
        if isinstance(buffer, (bytes, bytearray)):
            return buffer
    return str(item)
