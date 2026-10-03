"""Component definitions held by the MCP server (tools, resources, prompts).

These are plain dataclasses (the registry's storage shape). The *behavior* —
schema generation, execution, dispatch — lives in schema.py / _execute.py /
server.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# SEP-2663 task defaults (milliseconds), matching rmcp's TaskManager.
DEFAULT_TASK_TTL_MS = 300_000
DEFAULT_TASK_POLL_INTERVAL_MS = 1_000


@dataclass
class ToolDef:
    name: str
    fn: Callable
    title: str | None = None
    description: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None  # MCP ToolAnnotations (readOnlyHint, ...)
    icons: list[dict[str, Any]] | None = None
    guards: list[Any] = field(default_factory=list)
    # Computed at registration:
    args_struct: Any = None
    input_schema: dict[str, Any] | None = None
    is_async: bool = False
    injects_request: bool = False
    ctx_param: str | None = None  # name of the parameter to inject the Context under
    # SEP-2663 Tasks opt-in; the TTL and poll interval are in milliseconds.
    task: bool = False
    task_ttl_ms: int = DEFAULT_TASK_TTL_MS
    task_poll_interval_ms: int = DEFAULT_TASK_POLL_INTERVAL_MS


@dataclass
class ResourceDef:
    uri: str
    fn: Callable
    name: str | None = None
    description: str | None = None
    mime_type: str = "text/plain"
    is_async: bool = False


@dataclass
class ResourceTemplateDef:
    uri_template: str
    fn: Callable
    name: str | None = None
    description: str | None = None
    mime_type: str = "text/plain"
    param_names: list[str] = field(default_factory=list)
    is_async: bool = False
    # Computed at registration:
    pattern: Any = None  # compiled regex matching concrete URIs to this template
    args_struct: Any = None  # msgspec struct coercing extracted {vars} to the handler's types


@dataclass
class PromptDef:
    name: str
    fn: Callable
    description: str | None = None
    args_struct: Any = None
    arguments: list[dict[str, Any]] = field(default_factory=list)
    is_async: bool = False
