"""The ``Context`` injected into tools — a FastMCP-style per-call handle.

A tool declares a parameter annotated ``Context`` (excluded from the input
schema, like ``request``). Through it a tool can, while running:

- ``report_progress`` / ``debug``/``info``/``warning``/``error`` — push one-way
  ``notifications/progress`` / ``notifications/message`` onto the response
  stream. On 2026-07-28 requests, log lines are emitted only when the request
  carried ``io.modelcontextprotocol/logLevel`` in ``_meta`` (per spec).
- ``read_resource`` — read one of this server's own resources (local call).
- ``elicit`` / ``sample`` — ask the client (its user, or its LLM) for input.
  On legacy session clients this is a live server->client request awaited
  in-flight. On 2026-07-28 clients it uses the MRTR pattern: the tool call
  ends with an ``input_required`` result and the client retries with the
  answer attached — **the tool function re-runs from the top on each retry**
  (earlier ``elicit`` calls return instantly from the replayed answers).
  Code before an ``elicit`` must therefore be idempotent, or guarded with
  ``ctx.is_replay``.

The heavy lifting lives in Rust (``django_bolt._core.McpCallContext``): this
class is a thin asyncio-friendly wrapper.
"""

from __future__ import annotations

import asyncio
from typing import Any

from django_bolt._json import decode as json_decode
from django_bolt._json import encode as json_encode


class McpClientError(Exception):
    """Raised inside a tool when the client returns an error to a sample/elicit request."""

    def __init__(self, error: Any) -> None:
        super().__init__(str(error))
        self.error = error


class _InputRequired(BaseException):
    """Control-flow signal: an MRTR input point had no replayed answer.

    BaseException so tool code's ``except Exception`` blocks (and
    ``execute_tool``'s error mapping) cannot swallow it — the dispatch layer
    converts it into the wire ``input_required`` result.
    """

    def __init__(self, key: str, kind: str, params: dict[str, Any]) -> None:
        super().__init__(f"MCP input required: {kind} ({key})")
        self.key = key
        self.kind = kind
        self.params = params


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    if isinstance(messages, str):
        return [{"role": "user", "content": {"type": "text", "text": messages}}]
    return list(messages)


class Context:
    """Per-call handle a tool uses to stream notifications and call back into the client."""

    __slots__ = ("_rust", "_resolver", "request", "_input_seq")

    def __init__(self, rust_ctx: Any, request: Any = None, resolver: Any = None) -> None:
        self._rust = rust_ctx
        self._resolver = resolver  # _dispatch resolver for read_resource
        self.request = request
        self._input_seq = 0

    # ── request/era introspection ────────────────────────────────────────────
    @property
    def client_era(self) -> str:
        """``"legacy"`` (session client) or ``"modern"`` (2026-07-28 stateless)."""
        return self._rust.era

    @property
    def is_replay(self) -> bool:
        """True when this invocation is an MRTR retry (requestState was present)."""
        return self._rust.is_replay

    @property
    def has_progress_token(self) -> bool:
        """Whether the client asked for progress (sent a ``progressToken``)."""
        return self._rust.has_progress_token

    @property
    def meta(self) -> dict[str, Any]:
        """The request's raw ``_meta`` object ({} when absent)."""
        raw = self._rust.meta_json
        return json_decode(raw) if raw else {}

    # ── one-way notifications (no reply) ─────────────────────────────────────
    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        """Emit a ``notifications/progress`` (dropped without a client progressToken)."""
        self._rust.notify_progress(float(progress), None if total is None else float(total), message)

    async def log(self, level: str, data: Any, *, logger: str | None = None) -> None:
        """Emit a ``notifications/message`` log line.

        Deprecated in MCP 2026-07-28 (SEP-2577) but still functional: modern
        requests receive it only when they carried a ``logLevel`` in ``_meta``.
        """
        if not self._rust.wants_log(level):
            return
        self._rust.notify_log(level, json_encode(data).decode(), logger)

    async def debug(self, data: Any, *, logger: str | None = None) -> None:
        await self.log("debug", data, logger=logger)

    async def info(self, data: Any, *, logger: str | None = None) -> None:
        await self.log("info", data, logger=logger)

    async def warning(self, data: Any, *, logger: str | None = None) -> None:
        await self.log("warning", data, logger=logger)

    async def error(self, data: Any, *, logger: str | None = None) -> None:
        await self.log("error", data, logger=logger)

    # ── local capability ─────────────────────────────────────────────────────
    async def read_resource(self, uri: str) -> Any:
        """Read one of this server's own resources (static or templated)."""
        if self._resolver is None:
            raise RuntimeError("read_resource is unavailable in this context")
        text, _mime = await self._resolver(uri)
        return text

    # ── bidirectional: ask the client, await the answer ──────────────────────
    async def _input(self, kind: str, params: dict[str, Any], *, key: str | None) -> Any:
        self._input_seq += 1
        key = key or f"input-{self._input_seq}"
        if self._rust.era == "legacy":
            capability_ok = self._rust.supports_elicitation if kind == "elicitation" else self._rust.supports_sampling
            if not capability_ok:
                raise RuntimeError(
                    f"The connected MCP client did not advertise the {kind!r} capability at "
                    f"initialize, so this tool cannot call back into it. Use a client that "
                    f"supports {kind} (e.g. MCP Inspector)."
                )
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._rust.server_request_send(kind, json_encode(params).decode(), future)
            envelope = json_decode(await future)
        else:
            if kind == "sampling":
                raise RuntimeError(
                    "ctx.sample() is not supported for 2026-07-28 clients: sampling is "
                    "deprecated by the MCP spec (SEP-2577) — integrate with an LLM "
                    "provider API directly instead."
                )
            raw = self._rust.input_response(key)
            if raw is None:
                # Terminates the tool call; the client retries with the answer
                # and the tool re-runs from the top (see module docstring).
                raise _InputRequired(key, kind, params)
            envelope = json_decode(raw)
        if envelope.get("error") is not None:
            raise McpClientError(envelope["error"])
        return envelope["result"]

    async def elicit(self, message: str, schema: dict[str, Any] | None = None, *, key: str | None = None) -> Any:
        """Ask the user for input (``elicitation/create``); await/replay their response.

        ``key`` names the input point for MRTR replay matching. The default
        (call order) is correct when the sequence of ``elicit`` calls is
        deterministic for the same arguments; pass an explicit stable ``key=``
        when it is not (e.g. data-dependent branching).
        """
        params = {
            "mode": "form",
            "message": message,
            "requestedSchema": schema or {"type": "object", "properties": {}},
        }
        return await self._input("elicitation", params, key=key)

    async def sample(
        self,
        messages: Any,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 512,
        temperature: float | None = None,
        model_preferences: Any = None,
    ) -> dict[str, Any]:
        """Ask the client's LLM to generate (``sampling/createMessage``); await the result.

        Deprecated in MCP 2026-07-28 (SEP-2577); supported only for legacy
        session clients.
        """
        params: dict[str, Any] = {"messages": _normalize_messages(messages), "maxTokens": max_tokens}
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = temperature
        if model_preferences is not None:
            params["modelPreferences"] = model_preferences
        return await self._input("sampling", params, key=None)
