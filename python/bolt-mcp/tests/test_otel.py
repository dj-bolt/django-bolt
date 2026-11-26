"""bolt_mcp.otel: OpenTelemetry spans for tools/call, resources/read and prompts/get."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap

import pytest
from _helpers import initialize, parse_rpc, post_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, ToolCall, ToolError, mount_mcp
from bolt_mcp.otel import instrument
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
CLIENT_SPAN_ID = 0x00F067AA0BA902B7
TRACEPARENT = f"00-{TRACE_ID:032x}-{CLIENT_SPAN_ID:016x}-01"
CAPTURE_CONTENT = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
SECRET = "hunter2-7f3a"


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider
    provider.shutdown()


def _server(provider, *, before=(), after=()):
    """An instrumented server. ``before`` middleware is registered before ``instrument()``, ``after`` after it."""
    api = BoltAPI()
    mcp = MCP("otel-test")
    for fn in before:
        mcp.tool_middleware(fn)
    instrument(mcp, tracer_provider=provider)
    for fn in after:
        mcp.tool_middleware(fn)
    tool_tracer = provider.get_tracer("tools")

    @mcp.tool
    def plan_journey(origin: str, destination: str) -> dict:
        with tool_tracer.start_as_current_span("plan in sync tool"):
            return {"route": [origin, destination]}

    @mcp.tool
    async def lookup_fare(route: str) -> dict:
        # Suspend on a thread hop first, so the span is made after the tool resumes.
        await asyncio.to_thread(len, route)
        with tool_tracer.start_as_current_span("fare in async tool"):
            return {"fare": 12}

    @mcp.tool
    def refuse() -> str:
        raise ToolError("No route")

    @mcp.tool
    async def book(journey: str, ctx: Context) -> dict:
        return {"booked": await ctx.elicit(f"Book {journey}?")}

    @mcp.resource("timetable://{line}")
    def timetable(line: str) -> str:
        return f"{line}: every 10 minutes"

    @mcp.resource("boom://db")
    def boom() -> str:
        raise RuntimeError("connect to postgres://admin:hunter2@db.internal")

    @mcp.resource("vault://{key}")
    def vault(key: str) -> str:
        raise ValueError(f"secret {key}")

    @mcp.prompt
    def greet(name: str) -> str:
        return f"Hello {name}"

    @mcp.prompt
    def unlock(code: str) -> str:
        raise ValueError(f"secret {code}")

    mount_mcp(api, mcp)
    return api


def _rpc(client, method: str, params: dict, meta: dict | None = None) -> dict:
    _, session_id = initialize(client)
    if meta is not None:
        params = {**params, "_meta": meta}
    return parse_rpc(post_rpc(client, method, params, session_id=session_id))


def _server_span(exporter):
    (span,) = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    return span


def test_tool_call_gets_one_server_span_with_the_mcp_attributes(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "tools/call", {"name": "plan_journey", "arguments": {"origin": "A", "destination": "B"}})
    assert body["result"]["structuredContent"] == {"route": ["A", "B"]}
    span = _server_span(exporter)
    assert span.name == "tools/call plan_journey"
    assert span.kind is SpanKind.SERVER
    assert dict(span.attributes) == {
        "mcp.method.name": "tools/call",
        "gen_ai.tool.name": "plan_journey",
        "gen_ai.operation.name": "execute_tool",
    }
    assert span.status.status_code is StatusCode.UNSET


def test_span_parent_is_the_trace_context_in_the_request_meta(provider, exporter):
    with TestClient(_server(provider)) as client:
        _rpc(client, "tools/call", {"name": "refuse", "arguments": {}}, meta={"traceparent": TRACEPARENT})
    span = _server_span(exporter)
    assert span.context.trace_id == TRACE_ID
    assert span.parent.span_id == CLIENT_SPAN_ID
    assert span.parent.is_remote


def test_span_is_a_root_span_when_the_request_meta_has_no_trace_context(provider, exporter):
    with TestClient(_server(provider)) as client:
        _rpc(client, "tools/call", {"name": "refuse", "arguments": {}})
    assert _server_span(exporter).parent is None


def test_trace_context_values_that_are_not_strings_are_ignored(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(
            client,
            "tools/call",
            {"name": "plan_journey", "arguments": {"origin": "A", "destination": "B"}},
            meta={"traceparent": 5, "baggage": ["a=b"]},
        )
    assert body["result"]["isError"] is False
    assert _server_span(exporter).parent is None


def test_tool_error_result_sets_error_status(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "tools/call", {"name": "refuse", "arguments": {}})
    assert body["result"]["isError"] is True
    span = _server_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "tool_error"


def test_exception_in_middleware_inside_the_span_sets_error_status(provider, exporter, monkeypatch):
    monkeypatch.delenv(CAPTURE_CONTENT, raising=False)

    async def broken(call: ToolCall, call_next) -> dict:
        raise RuntimeError("quota store is down")

    with TestClient(_server(provider, after=[broken])) as client:
        body = _rpc(client, "tools/call", {"name": "plan_journey", "arguments": {"origin": "A", "destination": "B"}})
    assert body["result"]["isError"] is True
    span = _server_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes["error.type"] == "RuntimeError"
    assert span.events == ()


FAILING_CALLS = pytest.mark.parametrize(
    ("method", "params"),
    [
        ("resources/read", {"uri": f"vault://db?sig={SECRET}"}),
        ("prompts/get", {"name": "unlock", "arguments": {"code": SECRET}}),
    ],
)


@FAILING_CALLS
def test_exception_text_stays_out_of_the_span_without_content_capture(provider, exporter, monkeypatch, method, params):
    monkeypatch.delenv(CAPTURE_CONTENT, raising=False)
    with TestClient(_server(provider)) as client:
        body = _rpc(client, method, params)
    assert "error" in body
    span = _server_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes["error.type"] == "ValueError"
    assert span.events == ()
    # The name, the attributes, the events and the status of the exported span.
    assert SECRET not in span.to_json()


@FAILING_CALLS
def test_exception_is_recorded_when_content_capture_is_on(provider, exporter, monkeypatch, method, params):
    monkeypatch.setenv(CAPTURE_CONTENT, "true")
    with TestClient(_server(provider)) as client:
        _rpc(client, method, params)
    span = _server_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "ValueError"
    assert SECRET in event.attributes["exception.message"]


def test_input_request_passes_through_the_span_without_an_error(provider, exporter):
    with TestClient(_server(provider)) as client:
        response = post_rpc_modern(
            client, "tools/call", {"name": "book", "arguments": {"journey": "A-B"}}, capabilities={"elicitation": {}}
        )
    assert parse_rpc(response)["result"]["resultType"] == "input_required"
    span = _server_span(exporter)
    assert span.name == "tools/call book"
    assert span.status.status_code is StatusCode.UNSET
    assert "error.type" not in span.attributes
    assert span.events == ()


def test_resource_read_gets_a_server_span(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "resources/read", {"uri": "timetable://north"}, meta={"traceparent": TRACEPARENT})
    assert body["result"]["contents"][0]["text"] == "north: every 10 minutes"
    span = _server_span(exporter)
    assert span.name == "resources/read"
    assert span.kind is SpanKind.SERVER
    assert dict(span.attributes) == {"mcp.method.name": "resources/read", "mcp.resource.uri": "timetable://north"}
    assert span.parent.span_id == CLIENT_SPAN_ID
    assert span.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(
    ("uri", "recorded"),
    [
        ("timetable://north", "timetable://north"),
        ("note:42", "note:42"),
        ("vault://admin:hunter2@db.internal/key?day=mon&sig=abc", "vault://db.internal/key?day=mon&sig=REDACTED"),
        (
            "files://reports/q3?AWSAccessKeyId=AKIA1&Signature=c2ln&X-Goog-Signature=Z29v&page=2",
            "files://reports/q3?AWSAccessKeyId=REDACTED&Signature=REDACTED&X-Goog-Signature=REDACTED&page=2",
        ),
    ],
)
def test_resource_uri_attribute_has_no_credentials(provider, exporter, uri, recorded):
    with TestClient(_server(provider)) as client:
        _rpc(client, "resources/read", {"uri": uri})
    assert _server_span(exporter).attributes["mcp.resource.uri"] == recorded


def test_resource_reads_the_uri_as_the_client_sent_it(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "resources/read", {"uri": "timetable://north?sig=abc"})
    assert body["result"]["contents"][0]["text"] == "north?sig=abc: every 10 minutes"
    assert _server_span(exporter).attributes["mcp.resource.uri"] == "timetable://north?sig=REDACTED"


def test_resource_uri_that_cannot_be_parsed_is_not_recorded(provider, exporter, monkeypatch):
    monkeypatch.delenv(CAPTURE_CONTENT, raising=False)
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "resources/read", {"uri": "vault://admin:hunter2@[::1/key"})
    assert body["error"]["message"].startswith("Unknown resource")
    span = _server_span(exporter)
    assert "mcp.resource.uri" not in span.attributes
    assert "hunter2" not in span.to_json()


@pytest.mark.parametrize(
    ("uri", "error_type"),
    [("boom://db", "RuntimeError"), ("nothing://here", "JsonRpcError")],
)
def test_failed_resource_read_sets_error_status(provider, exporter, uri, error_type):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "resources/read", {"uri": uri})
    assert "error" in body
    span = _server_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == error_type


def test_prompt_get_gets_a_server_span(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(
            client, "prompts/get", {"name": "greet", "arguments": {"name": "Ada"}}, meta={"traceparent": TRACEPARENT}
        )
    assert body["result"]["messages"][0]["content"]["text"] == "Hello Ada"
    span = _server_span(exporter)
    assert span.name == "prompts/get greet"
    assert span.kind is SpanKind.SERVER
    assert dict(span.attributes) == {"mcp.method.name": "prompts/get", "gen_ai.prompt.name": "greet"}
    assert span.parent.span_id == CLIENT_SPAN_ID


def test_unknown_prompt_name_stays_out_of_the_span_name(provider, exporter):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "prompts/get", {"name": "no-such-prompt-7f3a", "arguments": {}})
    assert "error" in body
    span = _server_span(exporter)
    assert span.name == "prompts/get"
    assert span.attributes["gen_ai.prompt.name"] == "no-such-prompt-7f3a"
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "JsonRpcError"


def test_tool_arguments_and_result_are_not_recorded_by_default(provider, exporter, monkeypatch):
    monkeypatch.delenv(CAPTURE_CONTENT, raising=False)
    with TestClient(_server(provider)) as client:
        _rpc(client, "tools/call", {"name": "plan_journey", "arguments": {"origin": "Home", "destination": "Clinic"}})
    attributes = _server_span(exporter).attributes
    assert "gen_ai.tool.call.arguments" not in attributes
    assert "gen_ai.tool.call.result" not in attributes


def test_tool_arguments_and_result_are_recorded_when_content_capture_is_on(provider, exporter, monkeypatch):
    monkeypatch.setenv(CAPTURE_CONTENT, "true")
    with TestClient(_server(provider)) as client:
        _rpc(client, "tools/call", {"name": "plan_journey", "arguments": {"origin": "Home", "destination": "Clinic"}})
    attributes = _server_span(exporter).attributes
    assert json.loads(attributes["gen_ai.tool.call.arguments"]) == {"origin": "Home", "destination": "Clinic"}
    assert json.loads(attributes["gen_ai.tool.call.result"])["structuredContent"] == {"route": ["Home", "Clinic"]}


@pytest.mark.parametrize(
    ("tool", "arguments", "child_name"),
    [
        ("plan_journey", {"origin": "A", "destination": "B"}, "plan in sync tool"),
        ("lookup_fare", {"route": "A-B"}, "fare in async tool"),
    ],
)
def test_span_made_in_a_tool_is_a_child_of_the_server_span(provider, exporter, tool, arguments, child_name):
    with TestClient(_server(provider)) as client:
        body = _rpc(client, "tools/call", {"name": tool, "arguments": arguments}, meta={"traceparent": TRACEPARENT})
    assert body["result"]["isError"] is False
    server = _server_span(exporter)
    (child,) = [s for s in exporter.get_finished_spans() if s.name == child_name]
    assert child.parent.span_id == server.context.span_id
    assert child.context.trace_id == TRACE_ID


def test_tracing_runs_at_its_place_in_the_middleware_order(provider, exporter):
    current: dict[str, int] = {}

    async def registered_before(call: ToolCall, call_next) -> dict:
        current["before"] = trace.get_current_span().get_span_context().span_id
        return await call_next()

    async def registered_after(call: ToolCall, call_next) -> dict:
        current["after"] = trace.get_current_span().get_span_context().span_id
        result = await call_next()
        return {**result, "isError": True}

    with TestClient(_server(provider, before=[registered_before], after=[registered_after])) as client:
        body = _rpc(client, "tools/call", {"name": "plan_journey", "arguments": {"origin": "A", "destination": "B"}})
    assert body["result"]["isError"] is True
    span = _server_span(exporter)
    assert current["before"] != span.context.span_id
    assert current["after"] == span.context.span_id
    # The span sees the result of the middleware inside it.
    assert span.attributes["error.type"] == "tool_error"


def test_second_instrument_call_raises(provider, exporter):
    api = BoltAPI()
    mcp = MCP("instrumented-twice")
    instrument(mcp, tracer_provider=provider)
    with pytest.raises(ValueError, match="already instrumented"):
        instrument(mcp, tracer_provider=provider)

    @mcp.tool
    def ping() -> str:
        return "pong"

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        _rpc(client, "tools/call", {"name": "ping", "arguments": {}})
    assert [span.name for span in exporter.get_finished_spans()] == ["tools/call ping"]


def _run_python(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )


def test_without_instrument_bolt_mcp_imports_no_opentelemetry_and_makes_no_spans():
    result = _run_python(
        """
        import sys

        import django
        from django.conf import settings

        settings.configure(SECRET_KEY="x" * 50, INSTALLED_APPS=["django_bolt"], MIDDLEWARE=[])
        django.setup()

        from _helpers import parse_rpc, post_rpc_modern
        from bolt_mcp import MCP, mount_mcp
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()
        mcp = MCP("no-otel")

        @mcp.tool
        def ping() -> str:
            return "pong"

        mount_mcp(api, mcp)

        def call(client):
            response = post_rpc_modern(client, "tools/call", {"name": "ping", "arguments": {}})
            result = parse_rpc(response)["result"]
            assert result["content"][0]["text"] == "pong", result

        with TestClient(api) as client:
            call(client)
            loaded = sorted(name for name in sys.modules if name.split(".")[0] == "opentelemetry")
            assert not loaded, loaded

            # An app that sets up OpenTelemetry itself still gets no bolt-mcp spans.
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            call(client)
            assert exporter.get_finished_spans() == (), exporter.get_finished_spans()
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_instrument_without_opentelemetry_api_raises_an_import_error_that_names_the_extra():
    result = _run_python(
        """
        import sys

        sys.modules["opentelemetry"] = None  # as if opentelemetry-api is not installed

        try:
            import bolt_mcp.otel
        except ImportError as exc:
            print(exc)
        """
    )
    assert result.returncode == 0, result.stderr
    assert "bolt-mcp[otel]" in result.stdout
