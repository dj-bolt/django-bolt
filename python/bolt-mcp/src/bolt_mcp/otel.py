"""OpenTelemetry spans for MCP calls. Install ``bolt-mcp[otel]`` to use this module.

    from bolt_mcp.otel import instrument

    mcp = MCP("travel")
    instrument(mcp)

``instrument(mcp)`` adds tool, resource and prompt middleware that make one
``SERVER`` span for each ``tools/call``, ``resources/read`` and ``prompts/get``.
The span follows the OpenTelemetry semantic conventions for MCP. Its parent is
the trace context in the request ``_meta``. ``bolt_mcp`` does not import this
module, so an app that does not call ``instrument()`` does not load OpenTelemetry.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

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


@contextmanager
def _server_span(tracer: Tracer, name: str, meta: dict[str, Any], attributes: dict[str, str]) -> Iterator[Span]:
    parent = propagate.extract(meta, getter=_META_GETTER)
    # The span records an Exception and sets the ERROR status. It ignores a BaseException,
    # for example an MRTR input request.
    with tracer.start_as_current_span(name, context=parent, kind=SpanKind.SERVER, attributes=attributes) as span:
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__qualname__)
            raise


def instrument(mcp: MCP, *, tracer_provider: TracerProvider | None = None) -> None:
    """Make a ``SERVER`` span for each ``tools/call``, ``resources/read`` and ``prompts/get`` of ``mcp``.

    The spans use ``tracer_provider``, or else the global tracer provider.
    The tracing middleware takes its place in the middleware order at this call.
    Call ``instrument()`` before you register your own middleware, so that the span contains it.

    The span records the tool arguments and the tool result only when the environment variable
    ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is ``true`` at this call.
    """
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
        with _server_span(tracer, f"tools/call {call.name}", call.meta, attributes) as span:
            result = await call_next()
            if result.get("isError"):
                span.set_attribute("error.type", "tool_error")
                span.set_status(StatusCode.ERROR)
            if capture_content:
                span.set_attribute("gen_ai.tool.call.result", json_encode(result).decode())
            return result

    @mcp.resource_middleware
    async def trace_resource_read(read: ResourceRead, call_next) -> dict[str, Any]:
        # A URI can hold an id, so it stays out of the span name.
        attributes = {"mcp.method.name": "resources/read", "mcp.resource.uri": read.uri}
        with _server_span(tracer, "resources/read", read.meta, attributes):
            return await call_next()

    @mcp.prompt_middleware
    async def trace_prompt_get(get: PromptGet, call_next) -> dict[str, Any]:
        # The client chooses the name. Only the name of a registered prompt goes in the span name.
        name = f"prompts/get {get.name}" if get.name in mcp._prompts else "prompts/get"
        attributes = {"mcp.method.name": "prompts/get", "gen_ai.prompt.name": get.name}
        with _server_span(tracer, name, get.meta, attributes):
            return await call_next()
