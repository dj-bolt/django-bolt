"""OpenTelemetry spans for MCP calls. Install ``bolt-mcp[otel]`` to use this module.

    from bolt_mcp.otel import instrument

    mcp = MCP("travel")
    instrument(mcp)

``instrument(mcp)`` adds tool, resource and prompt middleware that make one
``SERVER`` span for each ``tools/call``, ``resources/read`` and ``prompts/get``.
The span follows the OpenTelemetry semantic conventions for MCP. Its parent is
the trace context in the request ``_meta``. ``bolt_mcp`` does not import this
module, so an app that does not call ``instrument()`` does not load OpenTelemetry.

Request content leaves the process only when the environment variable
``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is ``true``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

try:
    from opentelemetry import propagate, trace
    from opentelemetry.propagators.textmap import Getter
    from opentelemetry.trace import SpanKind, StatusCode
except ImportError as exc:
    raise ImportError(
        "bolt_mcp.otel needs the opentelemetry-api package. Install it with: pip install 'bolt-mcp[otel]'"
    ) from exc

from django_bolt._json import encode as json_encode

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer, TracerProvider

    from .registry import PromptGet, ResourceRead, ToolCall
    from .server import MCP

_CAPTURE_MESSAGE_CONTENT = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
# The OpenTelemetry HTTP conventions redact the values of these query parameters by default.
_REDACTED_QUERY_KEYS = frozenset({"AWSAccessKeyId", "Signature", "sig", "X-Goog-Signature"})


class _MetaGetter(Getter[dict[str, Any]]):
    """Read the trace context from the request ``_meta``.

    The client sets ``_meta``, so a value that is not a string counts as absent.
    """

    def get(self, carrier: dict[str, Any], key: str) -> list[str] | None:
        value = carrier.get(key)
        return [value] if isinstance(value, str) else None

    def keys(self, carrier: dict[str, Any]) -> list[str]:
        return list(carrier)


_META_GETTER = _MetaGetter()


def _redact_query_pair(pair: str) -> str:
    key = pair.partition("=")[0]
    return f"{key}=REDACTED" if key in _REDACTED_QUERY_KEYS else pair


def _redact_uri(uri: str) -> str | None:
    """Give ``uri`` with no userinfo, and with ``REDACTED`` as the value of each key in ``_REDACTED_QUERY_KEYS``.

    Give ``None`` for a URI that ``urlsplit`` cannot parse, because such a URI can hold credentials.
    """
    if "@" not in uri and "?" not in uri:
        return uri
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    netloc = parts.netloc.rpartition("@")[2]
    query = "&".join(_redact_query_pair(pair) for pair in parts.query.split("&"))
    if netloc == parts.netloc and query == parts.query:
        return uri
    return urlunsplit(parts._replace(netloc=netloc, query=query))


def _set_error(span: Span, error_type: str) -> None:
    span.set_attribute("error.type", error_type)
    span.set_status(StatusCode.ERROR)


@contextmanager
def _server_span(
    tracer: Tracer, name: str, meta: dict[str, Any], attributes: dict[str, str], capture_content: bool
) -> Iterator[Span]:
    parent = propagate.extract(meta, getter=_META_GETTER)
    # The exception text can hold request content, so it goes in the span only with content capture.
    # A BaseException, for example an MRTR input request, is not an error.
    with tracer.start_as_current_span(
        name,
        context=parent,
        kind=SpanKind.SERVER,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception as exc:
            _set_error(span, type(exc).__qualname__)
            if capture_content:
                span.record_exception(exc)
            raise


def instrument(mcp: MCP, *, tracer_provider: TracerProvider | None = None) -> None:
    """Make a ``SERVER`` span for each ``tools/call``, ``resources/read`` and ``prompts/get`` of ``mcp``.

    The spans use ``tracer_provider``, or else the global tracer provider.
    The tracing middleware takes its place in the middleware order at this call.
    Call ``instrument()`` before you register your own middleware, so that the span contains it.

    An ``isError`` tool result sets ``error.type`` to ``tool_error`` and the ``ERROR`` status.
    Bolt turns an exception in a tool into such a result before middleware sees it.
    An exception from a resource, a prompt or middleware in the span sets ``error.type``
    to the exception class name and the ``ERROR`` status.

    The span records the tool arguments, the tool result and the exception event (with the
    exception message) only when the environment variable
    ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is ``true`` at this call.
    The ``mcp.resource.uri`` attribute has no userinfo and no signature values.

    Raise ``ValueError`` when ``mcp`` is already instrumented.
    """
    # The middleware of an earlier instrument() call comes from this module.
    if any(getattr(fn, "__module__", None) == __name__ for fn in mcp._tool_middleware):
        raise ValueError(f"MCP server {mcp.name!r} is already instrumented")
    tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
    capture_content = os.environ.get(_CAPTURE_MESSAGE_CONTENT, "").lower() == "true"

    @mcp.tool_middleware
    async def trace_tool_call(call: ToolCall, call_next) -> dict[str, Any]:
        attributes = {
            "mcp.method.name": "tools/call",
            "gen_ai.tool.name": call.name,
            "gen_ai.operation.name": "execute_tool",
        }
        if capture_content:
            attributes["gen_ai.tool.call.arguments"] = json_encode(call.arguments).decode()
        with _server_span(tracer, f"tools/call {call.name}", call.meta, attributes, capture_content) as span:
            result = await call_next()
            if result.get("isError"):
                _set_error(span, "tool_error")
            if capture_content:
                span.set_attribute("gen_ai.tool.call.result", json_encode(result).decode())
            return result

    @mcp.resource_middleware
    async def trace_resource_read(read: ResourceRead, call_next) -> dict[str, Any]:
        # A URI can hold an id, so it stays out of the span name.
        attributes = {"mcp.method.name": "resources/read"}
        uri = _redact_uri(read.uri)
        if uri is not None:
            attributes["mcp.resource.uri"] = uri
        with _server_span(tracer, "resources/read", read.meta, attributes, capture_content):
            return await call_next()

    @mcp.prompt_middleware
    async def trace_prompt_get(get: PromptGet, call_next) -> dict[str, Any]:
        # The client chooses the name. Only the name of a registered prompt goes in the span name.
        name = f"prompts/get {get.name}" if get.name in mcp._prompts else "prompts/get"
        attributes = {"mcp.method.name": "prompts/get", "gen_ai.prompt.name": get.name}
        with _server_span(tracer, name, get.meta, attributes, capture_content):
            return await call_next()
