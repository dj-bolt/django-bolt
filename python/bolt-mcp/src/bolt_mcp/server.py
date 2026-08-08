"""The ``MCP`` server object: component registration.

Pure registry — decorators collect tools/resources/prompts and pre-compute
their schemas. The protocol itself (JSON-RPC dispatch, sessions, catalog
serving, guard evaluation) lives in django-bolt's Rust core (rmcp): at mount
time the registry is compiled into a catalog + dispatch surface (see
``_catalog.py`` / ``_dispatch.py``) and handed to Rust.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any, get_type_hints

from . import schema
from .context import Context
from .registry import PromptDef, ResourceDef, ResourceTemplateDef, ToolDef

_TEMPLATE_VAR = re.compile(r"\{(\w+)\}")

_CACHE_SCOPES = frozenset({"public", "private"})


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


def principal(request: Any) -> dict:
    """Return the authenticated principal dict for ``request``, regardless of auth tier.

    All auth tiers (Rust JWT/API-key and OAuth) resolve the principal in Rust
    before a tool runs, and it arrives as ``request.context``. The
    ``request.state["context"]`` fallback is kept for compatibility with code
    written against bolt-mcp < 0.2. Returns ``{}`` when unauthenticated.
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


def _arguments_from_signature(fn: Callable) -> list[dict[str, Any]]:
    args: list[dict[str, Any]] = []
    for p in inspect.signature(fn).parameters.values():
        if p.name in schema.INJECTED_PARAMS or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        args.append({"name": p.name, "required": p.default is inspect.Parameter.empty})
    return args


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
        title: str | None = None,
        instructions: str | None = None,
        website_url: str | None = None,
        icons: list[dict[str, Any]] | None = None,
        stateless: bool = False,
        json_response: bool = False,
        list_ttl_ms: int = 0,
        list_cache_scope: str = "private",
    ) -> None:
        """Declare an MCP server.

        ``stateless``/``json_response`` shape how *legacy* (pre-2026-07-28)
        clients are served: ``stateless=True`` disables sessions (multi-worker
        safe, no server->client requests), ``json_response=True`` answers
        legacy POSTs with plain JSON instead of SSE. 2026-07-28 clients are
        always served statelessly regardless.

        ``list_ttl_ms``/``list_cache_scope`` are the SEP-2549 caching hints
        stamped on every list result (``0``/``"private"`` = no caching).
        """
        if list_cache_scope not in _CACHE_SCOPES:
            raise ValueError(f"list_cache_scope must be 'public' or 'private', got {list_cache_scope!r}")
        self.name = name
        self.version = version
        self.title = title
        self.instructions = instructions
        self.website_url = website_url
        self.icons = icons
        self.stateless = stateless
        self.json_response = json_response
        self.list_ttl_ms = list_ttl_ms
        self.list_cache_scope = list_cache_scope
        self._tools: dict[str, ToolDef] = {}
        self._resources: dict[str, ResourceDef] = {}
        self._resource_templates: dict[str, ResourceTemplateDef] = {}
        self._prompts: dict[str, PromptDef] = {}

    # ── Registration decorators ──────────────────────────────────────────────
    def tool(
        self,
        name_or_fn: Callable | str | None = None,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        output_schema: dict[str, Any] | str | None = None,
        annotations: dict[str, Any] | None = None,
        icons: list[dict[str, Any]] | None = None,
        guards: list[Any] | None = None,
    ):
        """Register a tool.

        ``output_schema`` is a JSON Schema dict, or the string ``"auto"`` to
        derive it from the return annotation (msgspec-representable types
        only). ``annotations`` is an MCP ToolAnnotations dict (``readOnlyHint``
        etc.). ``guards`` are evaluated natively in Rust, both for
        ``tools/call`` denial and per-principal ``tools/list`` filtering.
        """

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
            resolved_output = (
                schema.output_schema_from_return(fn) if output_schema == "auto" else output_schema
            )
            self._tools[tool_name] = ToolDef(
                name=tool_name,
                fn=fn,
                title=title,
                description=description or inspect.getdoc(fn),
                output_schema=resolved_output,
                annotations=annotations,
                icons=icons,
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
