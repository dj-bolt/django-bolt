"""The ``Context`` injected into tools — a FastMCP-style per-call handle.

A tool declares a parameter annotated ``Context`` (excluded from the input schema,
like ``request``). Through it a tool can, while running:

- ``report_progress`` / ``debug``/``info``/``warning``/``error`` — push one-way
  ``notifications/progress`` / ``notifications/message`` onto the response SSE stream.
- ``read_resource`` — read one of this server's own resources (local, no round-trip).
- ``sample`` / ``elicit`` — send a request *to the client* (its LLM, or the user) and
  await the reply. These are bidirectional and require a stateful session (the reply
  arrives as a separate POST that is correlated by request id).
"""

from __future__ import annotations

import asyncio
from typing import Any


class McpClientError(Exception):
    """Raised inside a tool when the client returns an error to a sample/elicit request."""

    def __init__(self, error: Any) -> None:
        super().__init__(str(error))
        self.error = error


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    if isinstance(messages, str):
        return [{"role": "user", "content": {"type": "text", "text": messages}}]
    return list(messages)


class Context:
    """Per-call handle a tool uses to stream notifications and call back into the client."""

    def __init__(
        self,
        *,
        mcp: Any,
        request: Any = None,
        session: Any = None,
        request_id: Any = None,
        progress_token: Any = None,
        outgoing: asyncio.Queue | None = None,
    ) -> None:
        self._mcp = mcp
        self.request = request
        self._session = session
        self.request_id = request_id
        self.progress_token = progress_token
        self._outgoing = outgoing

    # ── one-way notifications (no reply) ─────────────────────────────────────--
    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        """Emit a ``notifications/progress`` (only if the client sent a progressToken)."""
        if self._outgoing is None or self.progress_token is None:
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self._outgoing.put(("notification", "notifications/progress", params))

    async def log(self, level: str, data: Any, *, logger: str | None = None) -> None:
        """Emit a ``notifications/message`` log line."""
        if self._outgoing is None:
            return
        params: dict[str, Any] = {"level": level, "data": data}
        if logger is not None:
            params["logger"] = logger
        await self._outgoing.put(("notification", "notifications/message", params))

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
        resolved = await self._mcp._resolve_resource(uri)
        if resolved is None:
            raise ValueError(f"Unknown resource: {uri!r}")
        return resolved[0]

    # ── bidirectional: server → client request, await reply ──────────────────--
    async def _request(self, method: str, params: dict[str, Any], *, capability: str) -> Any:
        if self._outgoing is None or self._session is None or not getattr(self._session, "id", ""):
            raise RuntimeError(
                f"{method} requires a stateful, streaming MCP session "
                "(use MCP(stateless=False, json_response=False) and a single worker)"
            )
        caps = getattr(self._session, "client_capabilities", None) or {}
        if capability not in caps:
            raise RuntimeError(
                f"The connected MCP client did not advertise the {capability!r} capability at "
                f"initialize (it sent: {sorted(caps) or 'none'}), so this tool cannot call back "
                f"into it. Use a client that supports {capability} (e.g. MCP Inspector); "
                "Claude Code supports elicitation but not sampling."
            )
        request_id = self._session.next_request_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._session.pending[request_id] = future
        await self._outgoing.put(("request", request_id, method, params))
        try:
            return await future
        finally:
            self._session.pending.pop(request_id, None)

    async def sample(
        self,
        messages: Any,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 512,
        temperature: float | None = None,
        model_preferences: Any = None,
    ) -> dict[str, Any]:
        """Ask the client's LLM to generate (``sampling/createMessage``); await the result."""
        params: dict[str, Any] = {"messages": _normalize_messages(messages), "maxTokens": max_tokens}
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = temperature
        if model_preferences is not None:
            params["modelPreferences"] = model_preferences
        return await self._request("sampling/createMessage", params, capability="sampling")

    async def elicit(self, message: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask the user for input (``elicitation/create``); await their response."""
        params = {"message": message, "requestedSchema": schema or {"type": "object", "properties": {}}}
        return await self._request("elicitation/create", params, capability="elicitation")
