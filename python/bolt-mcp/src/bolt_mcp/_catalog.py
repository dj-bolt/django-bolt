"""Compile an ``MCP`` registry into the Rust mount definition.

Everything the Rust core needs to answer catalog requests with zero Python is
serialized once here, at mount time: one wire-format JSON catalog (tool /
resource / prompt models exactly as they appear on the wire) plus the Python
execution surface (dispatch callables, guards metadata, auth config).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from django_bolt._json import encode as json_encode

if TYPE_CHECKING:
    from .server import MCP

# Domain separator: the MRTR requestState HMAC key must never equal any other
# key derived from SECRET_KEY.
_STATE_KEY_DOMAIN = b"django-bolt.mcp.request-state\x00"


def derive_state_key(secret_key: str) -> bytes:
    """Derive the 32-byte MRTR requestState HMAC key from Django's SECRET_KEY."""
    return hashlib.sha256(_STATE_KEY_DOMAIN + secret_key.encode()).digest()


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def compile_catalog(mcp: MCP) -> str:
    """Serialize the registry into the wire-format catalog JSON.

    Shapes mirror the MCP schema (camelCase) so Rust deserializes them with
    rmcp's own model types — the catalog cannot drift from the spec.
    """
    tools = [
        _drop_none(
            {
                "name": t.name,
                "title": t.title,
                "description": t.description,
                "inputSchema": t.input_schema,
                "outputSchema": t.output_schema,
                "annotations": t.annotations,
                "icons": t.icons,
            }
        )
        for t in mcp._tools.values()
    ]
    resources = [
        _drop_none(
            {
                "uri": r.uri,
                "name": r.name,
                "description": r.description,
                "mimeType": r.mime_type,
            }
        )
        for r in mcp._resources.values()
    ]
    templates = [
        _drop_none(
            {
                "uriTemplate": t.uri_template,
                "name": t.name,
                "description": t.description,
                "mimeType": t.mime_type,
            }
        )
        for t in mcp._resource_templates.values()
    ]
    prompts = [
        _drop_none(
            {
                "name": p.name,
                "description": p.description,
                "arguments": p.arguments,
            }
        )
        for p in mcp._prompts.values()
    ]
    catalog = {
        "serverInfo": _drop_none(
            {
                "name": mcp.name,
                "title": mcp.title,
                "version": mcp.version,
                "websiteUrl": mcp.website_url,
                "icons": mcp.icons,
            }
        ),
        "instructions": mcp.instructions,
        "tools": tools,
        "resources": resources,
        "resourceTemplates": templates,
        "prompts": prompts,
        "listTtlMs": mcp.list_ttl_ms,
        "listCacheScope": mcp.list_cache_scope,
    }
    return json_encode(catalog).decode()


def build_mount_definition(
    mcp: MCP,
    *,
    path: str,
    secret_key: str,
    max_body_bytes: int,
    auth_metadata: list[dict[str, Any]] | None,
    guards_metadata: list[dict[str, Any]],
    verify_token: Any | None,
    www_authenticate: str | None,
    allowed_hosts: list[str] | None,
    allowed_origins: list[str] | None,
) -> dict[str, Any]:
    """Build the mount definition dict consumed by ``_core.register_mcp_mounts``.

    Every key the Rust parser reads is always present (registration-time
    guarantee rule) — Rust uses direct access, never defaults.
    """
    from ._dispatch import prompt_dispatch, resource_dispatch, tool_entry  # noqa: PLC0415 — avoid import cycle

    return {
        "path": path,
        "catalog_json": compile_catalog(mcp),
        # json_response implies the sessionless transport: plain-JSON answers
        # exist for multi-process deployments, where in-process sessions are
        # broken anyway, and the rmcp core only shapes sessionless responses
        # as JSON.
        "legacy_sessions": not (mcp.stateless or mcp.json_response),
        "json_response": mcp.json_response,
        "max_body_bytes": max_body_bytes,
        "allowed_hosts": allowed_hosts,
        "allowed_origins": allowed_origins,
        "auth_backends": auth_metadata,
        "guards": guards_metadata,
        "tools": {name: tool_entry(mcp, tool) for name, tool in mcp._tools.items()},
        "resource_dispatch": resource_dispatch(mcp),
        "prompt_dispatch": prompt_dispatch(mcp),
        "verify_token": verify_token,
        "www_authenticate": www_authenticate,
        "state_key": derive_state_key(secret_key),
    }
