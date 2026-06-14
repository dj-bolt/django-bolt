"""The ``MCP`` server object: component registration + JSON-RPC dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, get_type_hints
from urllib.parse import unquote

import msgspec

from . import schema
from ._execute import error_result, execute_tool
from .context import Context
from .registry import PromptDef, ResourceDef, ResourceTemplateDef, ToolDef
from .sessions import SessionManager, StatelessSessions
from .types import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Incoming,
)

# Sentinel queued after a streaming tool finishes; carries its final ("result", ...) item.
_STREAM_DONE = "__stream_done__"

_TEMPLATE_VAR = re.compile(r"\{(\w+)\}")


def _compile_uri_template(uri_template: str) -> tuple[re.Pattern[str], list[str]]:
    """Compile a ``{var}`` URI template into a matching regex + ordered variable names.

    Each ``{var}`` becomes a named group capturing a single path segment (``[^/]+``);
    literal text between vars is matched verbatim. A template with no ``{var}`` yields
    an empty name list, signalling the caller to treat the URI as a static resource.
    """
    names: list[str] = []
    parts: list[str] = []
    last = 0
    for m in _TEMPLATE_VAR.finditer(uri_template):
        parts.append(re.escape(uri_template[last : m.start()]))
        names.append(m.group(1))
        parts.append(f"(?P<{m.group(1)}>[^/]+)")
        last = m.end()
    parts.append(re.escape(uri_template[last:]))
    return re.compile(f"^{''.join(parts)}$"), names


class McpError(Exception):
    """A JSON-RPC-level error (becomes a JSON-RPC error response, not in-band)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def principal(request: Any) -> dict:
    """Return the authenticated principal dict for ``request``, regardless of auth tier.

    Tier-1 (Rust) auth populates ``request.context``; the Python OAuth path populates
    ``request.state["context"]`` (``request.context`` is read-only from Python). Tools and
    guards should use this helper instead of reading ``request.context`` directly so they
    work under both. Returns ``{}`` when unauthenticated.
    """
    ctx = getattr(request, "context", None)
    if isinstance(ctx, dict):
        return ctx
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        stashed = state.get("context")
        if isinstance(stashed, dict):
            return stashed
    return {}


class _GuardAuthContext:
    """Adapts the request's auth-context dict to the attribute shape guards expect."""

    __slots__ = ("user_id", "is_staff", "is_superuser", "permissions")

    def __init__(self, ctx: dict | None) -> None:
        ctx = ctx or {}
        self.user_id = ctx.get("user_id")
        self.is_staff = bool(ctx.get("is_staff"))
        self.is_superuser = bool(ctx.get("is_superuser"))
        self.permissions = ctx.get("permissions")


def _arguments_from_signature(fn: Callable) -> list[dict[str, Any]]:
    args: list[dict[str, Any]] = []
    for p in inspect.signature(fn).parameters.values():
        if p.name in schema.INJECTED_PARAMS or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        args.append({"name": p.name, "required": p.default is inspect.Parameter.empty})
    return args


async def _invoke(fn: Callable, is_async: bool, /, **kwargs: Any) -> Any:
    """Call a registered resource/prompt handler, awaiting it when registered async."""
    return await fn(**kwargs) if is_async else fn(**kwargs)


def _find_context_param(fn: Callable) -> str | None:
    """Return the name of a parameter annotated ``Context``, if any."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        return None
    for pname, hint in hints.items():
        if hint is Context or (isinstance(hint, type) and issubclass(hint, Context)):
            return pname
    return None


class MCP:
    def __init__(
        self,
        name: str = "django-bolt",
        version: str = "0.1.0",
        *,
        stateless: bool = False,
        json_response: bool = False,
    ) -> None:
        self.name = name
        self.version = version
        self.stateless = stateless
        self.json_response = json_response
        self._tools: dict[str, ToolDef] = {}
        self._resources: dict[str, ResourceDef] = {}
        self._resource_templates: dict[str, ResourceTemplateDef] = {}
        self._prompts: dict[str, PromptDef] = {}
        self.sessions: SessionManager | StatelessSessions = StatelessSessions() if stateless else SessionManager()

    # ── Registration decorators ──────────────────────────────────────────────
    def tool(
        self,
        name_or_fn: Callable | str | None = None,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        output_schema: dict[str, Any] | None = None,
        guards: list[Any] | None = None,
    ):
        def register(fn: Callable) -> Callable:
            tool_name = name or getattr(fn, "__name__", "tool")
            if inspect.isasyncgenfunction(fn):
                raise TypeError(
                    f"Tool {tool_name!r} is an async generator. Generator-yield streaming has "
                    "been removed — declare a Context parameter and call ctx.report_progress(...) "
                    "/ ctx.info(...) for progress, then return the final result."
                )
            params = set(inspect.signature(fn).parameters)
            ctx_param = _find_context_param(fn)
            exclude = schema.INJECTED_PARAMS | ({ctx_param} if ctx_param else set())
            args_struct = schema.struct_from_signature(fn, exclude=exclude)
            self._tools[tool_name] = ToolDef(
                name=tool_name,
                fn=fn,
                title=title,
                description=description or inspect.getdoc(fn),
                output_schema=output_schema,
                guards=list(guards or []),
                args_struct=args_struct,
                input_schema=schema.input_schema_for(args_struct),
                is_async=inspect.iscoroutinefunction(fn),
                injects_request=bool(params & schema.INJECTED_PARAMS),
                ctx_param=ctx_param,
            )
            return fn

        if callable(name_or_fn):
            return register(name_or_fn)
        if isinstance(name_or_fn, str):
            name = name_or_fn
        return register

    def add_tool(self, tool: ToolDef) -> None:
        """Register a pre-built ToolDef (used by the auto-expose path)."""
        self._tools[tool.name] = tool

    def resource(
        self,
        uri: str,
        *,
        name: str | None = None,
        mime_type: str = "text/plain",
        description: str | None = None,
    ):
        """Register a resource. A ``uri`` containing ``{var}`` placeholders registers a
        *resource template*: the handler's parameters must match the placeholders, and a
        ``resources/read`` for any matching concrete URI extracts + type-coerces them."""

        def register(fn: Callable) -> Callable:
            pattern, var_names = _compile_uri_template(uri)
            res_name = name or getattr(fn, "__name__", uri)
            res_desc = description or inspect.getdoc(fn)
            if var_names:
                params = list(inspect.signature(fn).parameters)
                if set(params) != set(var_names):
                    raise ValueError(
                        f"Resource template {uri!r} variables {sorted(var_names)} do not match "
                        f"handler parameters {sorted(params)} — they must be identical."
                    )
                self._resource_templates[uri] = ResourceTemplateDef(
                    uri_template=uri,
                    fn=fn,
                    name=res_name,
                    description=res_desc,
                    mime_type=mime_type,
                    param_names=var_names,
                    is_async=inspect.iscoroutinefunction(fn),
                    pattern=pattern,
                    args_struct=schema.struct_from_signature(fn, exclude=frozenset()),
                )
            else:
                self._resources[uri] = ResourceDef(
                    uri=uri,
                    fn=fn,
                    name=res_name,
                    description=res_desc,
                    mime_type=mime_type,
                    is_async=inspect.iscoroutinefunction(fn),
                )
            return fn

        return register

    def prompt(
        self,
        name_or_fn: Callable | str | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
    ):
        def register(fn: Callable) -> Callable:
            prompt_name = name or getattr(fn, "__name__", "prompt")
            self._prompts[prompt_name] = PromptDef(
                name=prompt_name,
                fn=fn,
                description=description or inspect.getdoc(fn),
                args_struct=schema.struct_from_signature(fn),
                arguments=_arguments_from_signature(fn),
                is_async=inspect.iscoroutinefunction(fn),
            )
            return fn

        if callable(name_or_fn):
            return register(name_or_fn)
        if isinstance(name_or_fn, str):
            name = name_or_fn
        return register

    # ── Capabilities ─────────────────────────────────────────────────────────
    def capabilities(self) -> dict[str, Any]:
        cap: dict[str, Any] = {}
        if self._tools:
            cap["tools"] = {"listChanged": False}
        if self._resources or self._resource_templates:
            cap["resources"] = {"listChanged": False, "subscribe": False}
        if self._prompts:
            cap["prompts"] = {"listChanged": False}
        return cap

    # ── JSON-RPC dispatch (transport-agnostic) ───────────────────────────────
    async def dispatch(
        self, msg: Incoming, *, session: Any = None, request: Any = None
    ) -> dict | AsyncIterator[tuple] | None:
        """Handle one JSON-RPC request.

        Returns a result ``dict`` for ordinary methods. A ``tools/call`` for a streaming
        (Context-taking) tool instead returns an async iterator of tagged items — the
        transport frames each as its own SSE event — so the streaming decision lives here,
        not in the transport.
        """
        method = msg.method
        params = msg.params or {}
        if method == "initialize":
            return self._on_initialize(params, session)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._list_tools(request)
        if method == "tools/call":
            if not self.json_response and self._is_streaming_tool(params.get("name")):
                return self.stream_call(params, request, session=session, request_id=msg.id)
            return await self._call_tool(params, request, session=session, request_id=msg.id)
        if method == "resources/list":
            return self._list_resources()
        if method == "resources/read":
            return await self._read_resource(params)
        if method == "resources/templates/list":
            return self._list_resource_templates()
        if method == "prompts/list":
            return self._list_prompts()
        if method == "prompts/get":
            return await self._get_prompt(params)
        if method.startswith("notifications/"):
            return None
        raise McpError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _on_initialize(self, params: dict, session: Any) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        if session is not None:
            session.protocol_version = version
            session.client_capabilities = params.get("capabilities") or {}
        return {
            "protocolVersion": version,
            "capabilities": self.capabilities(),
            "serverInfo": {"name": self.name, "version": self.version},
        }

    # ── Guards ────────────────────────────────────────────────────────────────
    def _guards_pass(self, guards: list[Any], request: Any) -> bool:
        if not guards:
            return True
        adapter = _GuardAuthContext(principal(request))
        return all(g.has_permission(adapter) for g in guards)

    # ── Tools ─────────────────────────────────────────────────────────────────
    def _tool_dict(self, tool: ToolDef) -> dict[str, Any]:
        d: dict[str, Any] = {"name": tool.name, "inputSchema": tool.input_schema}
        if tool.title:
            d["title"] = tool.title
        if tool.description:
            d["description"] = tool.description
        if tool.output_schema:
            d["outputSchema"] = tool.output_schema
        return d

    def _list_tools(self, request: Any) -> dict:
        tools = [self._tool_dict(t) for t in self._tools.values() if self._guards_pass(t.guards, request)]
        return {"tools": tools}

    def _is_streaming_tool(self, name: str | None) -> bool:
        """A tool that streams over SSE — one taking a Context (its notifications drive the stream)."""
        tool = self._tools.get(name)
        return tool is not None and tool.ctx_param is not None

    def _resolve_tool(self, params: dict, request: Any) -> tuple[ToolDef | None, Any]:
        """Resolve + authorize a tool and build its kwargs.

        Returns ``(tool, kwargs)`` on success, or ``(None, error_result_dict)`` for
        unknown tool / failed guard / invalid arguments (all in-band errors).
        """
        name = params.get("name")
        tool = self._tools.get(name)
        if tool is None:
            return None, error_result(f"Unknown tool: {name!r}")
        if not self._guards_pass(tool.guards, request):
            return None, error_result(f"Permission denied for tool {name!r}")
        try:
            args = msgspec.convert(params.get("arguments") or {}, tool.args_struct)
        except msgspec.ValidationError as exc:
            return None, error_result(f"Invalid arguments: {exc}")
        kwargs = msgspec.structs.asdict(args)
        if tool.injects_request:
            kwargs["request"] = request
        return tool, kwargs

    def _make_context(self, params: dict, request: Any, session: Any, request_id: Any, outgoing: Any) -> Context:
        return Context(
            mcp=self,
            request=request,
            session=session,
            request_id=request_id,
            progress_token=(params.get("_meta") or {}).get("progressToken"),
            outgoing=outgoing,
        )

    async def _call_tool(self, params: dict, request: Any, *, session: Any = None, request_id: Any = None) -> dict:
        tool, prepared = self._resolve_tool(params, request)
        if tool is None:
            return prepared  # in-band error result
        if tool.ctx_param:
            # Non-streaming path (json_response): notifications are dropped (outgoing=None),
            # sample/elicit raise — they require the streaming SSE path.
            prepared[tool.ctx_param] = self._make_context(params, request, session, request_id, None)
        return await execute_tool(tool, prepared)

    async def stream_call(self, params: dict, request: Any, *, session: Any = None, request_id: Any = None):
        """Drive a Context-taking tool, yielding tagged items as the tool produces them.

        The tool's Context pushes ``("notification", method, params)`` and
        ``("request", id, method, params)`` onto the queue as side effects; once the tool
        returns, a single ``("result", call_tool_result)`` follows. The transport wraps each
        item as an SSE ``message`` event.
        """
        tool, prepared = self._resolve_tool(params, request)
        if tool is None:
            yield ("result", prepared)
            return

        outgoing: asyncio.Queue = asyncio.Queue()
        prepared[tool.ctx_param] = self._make_context(params, request, session, request_id, outgoing)

        async def _run_and_signal() -> None:
            result = await execute_tool(tool, prepared)
            await outgoing.put((_STREAM_DONE, ("result", result)))

        task = asyncio.create_task(_run_and_signal())
        try:
            while True:
                item = await outgoing.get()
                if item[0] == _STREAM_DONE:
                    yield item[1]
                    return
                yield item
        finally:
            # On normal completion the task is already done; on client disconnect
            # (GeneratorExit) it may be blocked awaiting a sample/elicit reply that
            # will never come — cancel it so cleanup can't hang.
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ── Resources ───────────────────────────────────────────────────────────--
    def _list_resources(self) -> dict:
        resources = []
        for r in self._resources.values():
            entry: dict[str, Any] = {"uri": r.uri, "name": r.name, "mimeType": r.mime_type}
            if r.description:
                entry["description"] = r.description
            resources.append(entry)
        return {"resources": resources}

    def _list_resource_templates(self) -> dict:
        templates = []
        for t in self._resource_templates.values():
            entry: dict[str, Any] = {"uriTemplate": t.uri_template, "name": t.name, "mimeType": t.mime_type}
            if t.description:
                entry["description"] = t.description
            templates.append(entry)
        return {"resourceTemplates": templates}

    async def _resolve_resource(self, uri: str) -> tuple[str, str] | None:
        """Resolve a URI to ``(text, mime_type)``, trying static resources then templates.

        Returns ``None`` when nothing matches. May raise ``msgspec.ValidationError`` if a
        template matches but its extracted ``{vars}`` don't coerce to the handler's types.
        Shared by ``resources/read`` and ``Context.read_resource`` so both honor templates.
        """
        resource = self._resources.get(uri)
        if resource is not None:
            return await _invoke(resource.fn, resource.is_async), resource.mime_type
        for tmpl in self._resource_templates.values():
            match = tmpl.pattern.match(uri)
            if match is None:
                continue
            raw = {k: unquote(v) for k, v in match.groupdict().items()}
            kwargs = msgspec.structs.asdict(msgspec.convert(raw, tmpl.args_struct, strict=False))
            return await _invoke(tmpl.fn, tmpl.is_async, **kwargs), tmpl.mime_type
        return None

    async def _read_resource(self, params: dict) -> dict:
        uri = params.get("uri")
        try:
            resolved = await self._resolve_resource(uri)
        except msgspec.ValidationError as exc:
            raise McpError(INVALID_PARAMS, f"Invalid resource URI {uri!r}: {exc}") from exc
        if resolved is None:
            raise McpError(INVALID_PARAMS, f"Unknown resource: {uri!r}")
        text, mime_type = resolved
        return {"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]}

    # ── Prompts ──────────────────────────────────────────────────────────────
    def _list_prompts(self) -> dict:
        prompts = []
        for p in self._prompts.values():
            entry: dict[str, Any] = {"name": p.name, "arguments": p.arguments}
            if p.description:
                entry["description"] = p.description
            prompts.append(entry)
        return {"prompts": prompts}

    async def _get_prompt(self, params: dict) -> dict:
        name = params.get("name")
        prompt = self._prompts.get(name)
        if prompt is None:
            raise McpError(INVALID_PARAMS, f"Unknown prompt: {name!r}")
        try:
            args = msgspec.convert(params.get("arguments") or {}, prompt.args_struct)
        except msgspec.ValidationError as exc:
            raise McpError(INVALID_PARAMS, f"Invalid arguments: {exc}") from exc
        kwargs = msgspec.structs.asdict(args)
        rendered = await _invoke(prompt.fn, prompt.is_async, **kwargs)
        if isinstance(rendered, str):
            messages = [{"role": "user", "content": {"type": "text", "text": rendered}}]
        else:
            messages = rendered
        result: dict[str, Any] = {"messages": messages}
        if prompt.description:
            result["description"] = prompt.description
        return result
