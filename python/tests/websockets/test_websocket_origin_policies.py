"""Security, flexibility and precedence of global/per-endpoint WS Origin policies."""
from __future__ import annotations

import asyncio
import json

import pytest
from django.test import override_settings

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import IsAuthenticated
from django_bolt.testing import WebSocketTestClient
from django_bolt.websocket.origin_policy import resolve_origin_options


async def mounted(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    if scope["query_string"] == b"report_origins":
        origins = [value.decode() for name, value in scope["headers"] if name == b"origin"]
        await send({"type": "websocket.send", "text": json.dumps(origins)})
    else:
        await send({"type": "websocket.send", "text": "mounted"})


def make_api(*, native_options=None, mount_options=None):
    api = BoltAPI()

    @api.websocket("/native", **(native_options or {}))
    async def native(ws: WebSocket):
        await ws.accept()
        await ws.send_text("native")

    api.mount_asgi("/mounted", mounted, **(mount_options or {}))
    return api


async def connect(api, path, origin, *, read_settings=True):
    headers = {} if origin is None else {"Origin": origin}
    async with WebSocketTestClient(
        api, path, headers=headers, read_django_settings=read_settings
    ) as ws:
        return await ws.receive_text()


@pytest.mark.asyncio
async def test_global_allowlist_applies_to_native_and_asgi_and_can_be_overridden():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="allowlist",
        BOLT_WEBSOCKET_ALLOWED_ORIGINS=["https://global.example"],
    ):
        api = make_api(mount_options={
            "websocket_origin_policy": "allowlist",
            "websocket_allowed_origins": ["https://local.example"],
        })
        assert await connect(api, "/native", "https://global.example") == "native"
        assert await connect(api, "/mounted/ws", "https://local.example") == "mounted"
        for path, origin in [
            ("/native", "https://local.example"),
            ("/mounted/ws", "https://global.example"),
        ]:
            with pytest.raises(PermissionError, match="Origin not allowed"):
                await connect(api, path, origin)


@pytest.mark.asyncio
async def test_global_missing_origin_can_be_overridden_per_route():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="allowlist",
        BOLT_WEBSOCKET_ALLOWED_ORIGINS=["https://allowed.example"],
        BOLT_WEBSOCKET_ALLOW_MISSING_ORIGIN=False,
    ):
        api = make_api(native_options={"allow_missing_origin": True})
        assert await connect(api, "/native", None) == "native"
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/mounted/ws", None)


@pytest.mark.asyncio
async def test_any_is_opt_in_and_rejects_null_bad_origin_and_missing_by_default():
    api = make_api(native_options={"origin_policy": "any"})
    assert await connect(api, "/native", "https://external.example") == "native"
    for origin in (None, "null", "https://external.example/path"):
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", origin)

    api2 = make_api(native_options={"origin_policy": "any", "allow_missing_origin": True})
    assert await connect(api2, "/native", None) == "native"


@pytest.mark.asyncio
async def test_custom_sync_and_async_origin_validators_before_accept():
    seen = []

    def sync_validator(origin, context):
        seen.append((origin, context["path"]))
        return origin == "https://trusted.example" and context["path"] == "/native"

    async def async_validator(origin, context):
        return origin == "https://chat.example" and context["path"] == "/mounted/ws"

    api = make_api(
        native_options={"origin_policy": "custom", "origin_validator": sync_validator},
        mount_options={
            "websocket_origin_policy": "custom",
            "websocket_origin_validator": async_validator,
        },
    )
    assert await connect(api, "/native", "https://trusted.example") == "native"
    assert seen == [("https://trusted.example", "/native")]
    assert await connect(api, "/mounted/ws", "https://chat.example") == "mounted"
    for path, origin in [
        ("/native", "https://evil.example"),
        ("/mounted/ws", "https://evil.example"),
        ("/native", None),
    ]:
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, path, origin)


@pytest.mark.asyncio
async def test_custom_exception_and_non_bool_result_fail_closed():
    def crashes(origin, context):
        raise RuntimeError("validation failed")

    def invalid_result(origin, context):
        return "yes"

    for validator in (crashes, invalid_result):
        api = make_api(native_options={
            "origin_policy": "custom", "origin_validator": validator,
        })
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://allowed.example")


@pytest.mark.asyncio
async def test_global_custom_validator_and_explicit_inherit_override():
    def validator(origin, context):
        return origin == "https://global.example"

    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="custom",
        BOLT_WEBSOCKET_ORIGIN_VALIDATOR=validator,
    ):
        api = make_api()
        assert await connect(api, "/native", "https://global.example") == "native"
        assert await connect(api, "/mounted/ws", "https://global.example") == "mounted"
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://evil.example")

        # An explicit inherit choice overrides the global custom validator
        # and follows the default Bolt CORS policy (no CORS => reject).
        separate = make_api(native_options={"origin_policy": "inherit"})
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(separate, "/native", "https://global.example")


def test_invalid_local_policy_rejected_at_registration():
    with pytest.raises(ValueError, match="Origin policy"):
        make_api(native_options={"origin_policy": "typo"})
    with pytest.raises(ValueError, match="nonempty"):
        make_api(mount_options={"websocket_origin_policy": "allowlist"})
    with pytest.raises(ValueError, match="callable"):
        make_api(native_options={"origin_policy": "custom"})
    with pytest.raises(TypeError, match="allow_missing_origin"):
        make_api(native_options={"allow_missing_origin": "yes"})


@pytest.mark.asyncio
async def test_global_settings_not_read_when_test_client_opts_out():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="any",
    ):
        api = make_api()
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://external.example", read_settings=False)


@pytest.mark.asyncio
async def test_custom_can_explicitly_allow_missing_origin_after_validation():
    observed = []

    async def validator(origin, context):
        observed.append(origin)
        return origin is None

    api = make_api(native_options={
        "origin_policy": "custom",
        "origin_validator": validator,
        "allow_missing_origin": True,
    })
    assert await connect(api, "/native", None) == "native"
    assert observed == [None]
    with pytest.raises(PermissionError, match="Origin not allowed"):
        await connect(api, "/native", "https://untrusted.example")


def test_global_delegate_is_not_an_implicit_security_opt_out():
    with (
        override_settings(BOLT_WEBSOCKET_ORIGIN_POLICY="delegate"),
        pytest.raises(ValueError, match="delegate must be selected explicitly"),
    ):
        resolve_origin_options(asgi=True)


@pytest.mark.parametrize("origin", [
    "https://example.com:0", "https://example.com:65536", "https://example.com:",
    "https://exa\x00mple.com", "https://é.example", "https://example.com\\evil",
    "https://[not-ipv6]", "https://user@example.com", "https://example.com?",
    "https://example.com#", "ftp://example.com", "null",
])
def test_allowlist_rejects_malformed_configured_origins(origin):
    with pytest.raises(ValueError, match="Invalid WebSocket origin"):
        make_api(native_options={"origin_policy": "allowlist", "allowed_origins": [origin]})


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    "https://example.com:0", "https://example.com:65536", "https://example.com:",
    "https://[not-ipv6]", "https://example.com/path", "https://user@example.com",
    "https://example.com?", "https://example.com#", "https://example.com,https://evil.example",
])
async def test_any_rejects_malformed_origin_authority(origin):
    api = make_api(native_options={"origin_policy": "any"})
    with pytest.raises(PermissionError, match="Origin not allowed"):
        await connect(api, "/native", origin)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    "http://localhost", "https://EXAMPLE.com", "https://example.com:443",
    "http://127.0.0.1:8000", "https://[::1]", "http://[2001:db8::1]:65535",
])
async def test_explicit_policies_accept_serialized_web_origins(origin):
    for options in (
        {"origin_policy": "any"},
        {"origin_policy": "allowlist", "allowed_origins": [origin]},
    ):
        assert await connect(make_api(native_options=options), "/native", origin) == "native"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/native", "/mounted/ws"])
@pytest.mark.parametrize("policy", ["inherit", "allowlist", "any", "custom"])
async def test_repeated_origins_cannot_disagree_between_policy_and_context(path, policy):
    options = {"origin_policy": policy}
    origins = ["https://first.example", "https://second.example"]
    if policy == "allowlist":
        options["allowed_origins"] = origins
    elif policy == "custom":
        options["origin_validator"] = lambda _origin, _context: True
    api = make_api(
        native_options=options,
        mount_options={f"websocket_{key}": value for key, value in options.items()},
    )
    with pytest.raises(PermissionError, match="Origin not allowed"):
        async with WebSocketTestClient(
            api, path, headers={"Origin": origins[0], "origin": origins[1]},
            cors_allowed_origins=origins, read_django_settings=False,
        ):
            pass


@pytest.mark.asyncio
async def test_delegate_leaves_repeated_origin_decisions_to_the_app():
    api = make_api(mount_options={"websocket_origin_policy": "delegate"})
    async with WebSocketTestClient(
        api, "/mounted/ws", query_string="report_origins",
        headers={"Origin": "https://first.example", "origin": "https://second.example"},
    ) as ws:
        assert json.loads(await ws.receive_text()) == ["https://first.example", "https://second.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/native", "/mounted/ws"])
async def test_custom_timeout_denies_opening_handshake(path):
    async def validator(origin, context):
        await asyncio.sleep(10)
        return True

    api = make_api(
        native_options={"origin_policy": "custom", "origin_validator": validator},
        mount_options={"websocket_origin_policy": "custom", "websocket_origin_validator": validator},
    )
    with pytest.raises(PermissionError, match="Origin not allowed"):
        await connect(api, path, "https://allowed.example")


@pytest.mark.asyncio
async def test_custom_validator_does_not_bypass_native_authentication():
    seen = []

    def validator(origin, context):
        seen.append(origin)
        return True

    api = BoltAPI()

    @api.websocket(
        "/protected", guards=[IsAuthenticated()],
        origin_policy="custom", origin_validator=validator,
    )
    async def protected(ws: WebSocket):
        await ws.accept()

    with pytest.raises(PermissionError):
        await connect(api, "/protected", "https://allowed.example")
    assert seen == []
