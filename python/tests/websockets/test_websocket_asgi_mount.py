"""WebSocket dispatch to a mounted ASGI application, in process.

Uses `WebSocketTestClient`, which routes through Rust for path matching and
scope construction, then drives the mounted app over a mock transport.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import HandshakeRejected, WebSocketTestClient


async def echo_app(scope, receive, send):
    assert scope["type"] == "websocket"

    connect = await receive()
    assert connect["type"] == "websocket.connect"

    await send({"type": "websocket.accept"})

    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        if "bytes" in message and message["bytes"] is not None:
            await send({"type": "websocket.send", "bytes": message["bytes"]})
        else:
            await send({"type": "websocket.send", "text": f"echo:{message['text']}"})


async def scope_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    headers = {name.decode(): value.decode() for name, value in scope["headers"]}
    await send(
        {
            "type": "websocket.send",
            "text": "|".join(
                [
                    scope["path"],
                    scope["root_path"],
                    scope["query_string"].decode(),
                    headers.get("authorization", ""),
                ]
            ),
        }
    )


async def scheme_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.send", "text": scope["scheme"]})


def router_app(scope, receive, send):
    """Route like `channels.routing.URLRouter`: strip `root_path`, then match.

    A scope that already removed the prefix makes such a router strip it twice
    and match nothing.
    """
    path = scope["path"]
    root_path = scope["root_path"]
    if root_path and not path.startswith(root_path):
        raise AssertionError(f"path {path!r} must keep the prefix {root_path!r}")
    remainder = path[len(root_path) :].lstrip("/")

    prefix, _, room = remainder.partition("/")
    if prefix != "room" or not room:
        raise AssertionError(f"no route for {remainder!r}")

    async def run():
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": f"room:{room}"})

    return run()


@pytest.fixture
def api():
    api = BoltAPI()

    @api.websocket("/ws/direct")
    async def direct(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("route")

    api.mount_asgi("/mounted", echo_app)
    api.mount_asgi("/scope", scope_app)
    api.mount_asgi("/routed", router_app)
    api.mount_asgi("/scheme", scheme_app)
    return api


@pytest.mark.asyncio
async def test_mounted_app_echoes_text(api):
    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        assert ws.accepted
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"


@pytest.mark.asyncio
async def test_default_mount_inherits_origin_policy(api):
    """A legacy mount retains Bolt's global protection without configuration."""
    options = {
        "headers": {"Origin": "https://different.example"},
        "cors_allowed_origins": ["https://allowed-by-bolt.example"],
        "read_django_settings": False,
    }
    with pytest.raises(PermissionError, match="Origin not allowed"):
        async with WebSocketTestClient(api, "/mounted/ws", **options):
            pass

    with pytest.raises(PermissionError, match="Origin not allowed"):
        async with WebSocketTestClient(api, "/ws/direct", **options):
            pass


@pytest.mark.asyncio
async def test_explicit_mount_allowlist_is_strict():
    api = BoltAPI()
    api.mount_asgi(
        "/restricted", echo_app, websocket_origin_policy="allowlist",
        websocket_allowed_origins=["https://restricted.example"],
    )
    options = {"cors_allowed_origins": ["https://global.example"], "read_django_settings": False}
    async with WebSocketTestClient(
        api, "/restricted/ws", headers={"Origin": "https://restricted.example"}, **options
    ) as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"

    for headers in ({}, {"Origin": "https://global.example"}, {"Origin": "https://evil.example"}):
        with pytest.raises(PermissionError, match="Origin not allowed"):
            async with WebSocketTestClient(api, "/restricted/ws", headers=headers, **options):
                pass


@pytest.mark.asyncio
async def test_mounted_asgi_app_can_reject_origins_it_does_not_trust():
    """The mounted app, not Bolt, decides which browser origins are trusted."""
    async def protected_app(scope, receive, send):
        await receive()
        origin = dict(scope["headers"]).get(b"origin")
        if origin != b"https://trusted.example":
            await send({"type": "websocket.close"})
            return
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "trusted"})

    api = BoltAPI()
    api.mount_asgi("/protected", protected_app, websocket_origin_policy="delegate")
    options = {
        "cors_allowed_origins": ["https://bolt-policy.example"],
        "read_django_settings": False,
    }
    async with WebSocketTestClient(
        api, "/protected/ws", headers={"Origin": "https://trusted.example"}, **options
    ) as ws:
        assert await ws.receive_text() == "trusted"

    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(
            api, "/protected/ws", headers={"Origin": "https://untrusted.example"}, **options
        ):
            pass
    assert rejected.value.status_code == 403

    # The delegated app also refuses a client which omits Origin entirely.
    with pytest.raises(HandshakeRejected) as missing:
        async with WebSocketTestClient(api, "/protected/ws", headers={}, **options):
            pass
    assert missing.value.status_code == 403


def test_mount_origin_policy_validation():
    async def app(scope, receive, send):
        return None

    for policy in ("unknown", "INHERIT", ""):
        with pytest.raises(ValueError, match="Origin policy"):
            BoltAPI().mount_asgi("/bad", app, websocket_origin_policy=policy)

    with pytest.raises(ValueError, match="nonempty list"):
        BoltAPI().mount_asgi("/bad", app, websocket_origin_policy="allowlist")

    for origin in ("*", "https://a.example/path", "https://a.example:99999", "https://a.example@evil.test"):
        with pytest.raises(ValueError, match="Invalid WebSocket origin"):
            BoltAPI().mount_asgi(
                "/bad", app, websocket_origin_policy="allowlist", websocket_allowed_origins=[origin]
            )

    with pytest.raises(ValueError, match="requires"):
        BoltAPI().mount_asgi("/bad", app, websocket_allowed_origins=["https://a.example"])


@pytest.mark.asyncio
async def test_same_asgi_callable_can_have_independent_origin_policies():
    """Per-mount options must not mutate or leak onto the ASGI callable."""
    api = BoltAPI()
    api.mount_asgi(
        "/team-a", echo_app, websocket_origin_policy="allowlist",
        websocket_allowed_origins=["https://a.example"],
    )
    api.mount_asgi(
        "/team-b", echo_app, websocket_origin_policy="allowlist",
        websocket_allowed_origins=["https://b.example"],
    )
    options = {"read_django_settings": False}

    async with WebSocketTestClient(api, "/team-a/ws", headers={"Origin": "https://a.example"}, **options) as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"

    with pytest.raises(PermissionError, match="Origin not allowed"):
        async with WebSocketTestClient(api, "/team-b/ws", headers={"Origin": "https://a.example"}, **options):
            pass


@pytest.mark.asyncio
async def test_mount_django_custom_asgi_can_select_origin_policy():
    api = BoltAPI()
    api.mount_django(
        "/custom", echo_app, websocket_origin_policy="allowlist",
        websocket_allowed_origins=["https://custom.example"],
    )
    options = {"read_django_settings": False}
    async with WebSocketTestClient(api, "/custom/ws", headers={"Origin": "https://custom.example"}, **options) as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"

    with pytest.raises(PermissionError, match="Origin not allowed"):
        async with WebSocketTestClient(api, "/custom/ws", headers={"Origin": "https://evil.example"}, **options):
            pass


@pytest.mark.asyncio
async def test_mounted_app_echoes_bytes(api):
    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        await ws.send_bytes(b"\x00\x01")
        assert await ws.receive_bytes() == b"\x00\x01"


@pytest.mark.asyncio
async def test_mounted_app_gets_asgi_scope(api):
    async with WebSocketTestClient(
        api,
        "/scope/inner/path",
        query_string="token=abc",
        headers={"Authorization": "Bearer secret"},
    ) as ws:
        path, root_path, query_string, authorization = (await ws.receive_text()).split("|")

    assert path == "/scope/inner/path"
    assert root_path == "/scope"
    assert query_string == "token=abc"
    assert authorization == "Bearer secret"


@pytest.mark.asyncio
async def test_mounted_app_path_keeps_trailing_slash(api):
    # A Channels route such as path("room/<name>/") needs the slash.
    async with WebSocketTestClient(api, "/scope/inner/path/") as ws:
        path, *_ = (await ws.receive_text()).split("|")

    assert path == "/scope/inner/path/"


@pytest.mark.asyncio
async def test_mounted_app_path_is_percent_decoded(api):
    async with WebSocketTestClient(api, "/scope/caf%C3%A9") as ws:
        path, *_ = (await ws.receive_text()).split("|")

    assert path == "/scope/café"


@pytest.mark.asyncio
async def test_router_app_reads_url_captures(api):
    async with WebSocketTestClient(api, "/routed/room/lobby") as ws:
        assert await ws.receive_text() == "room:lobby"


@pytest.mark.asyncio
async def test_registered_route_wins_over_mount(api):
    async with WebSocketTestClient(api, "/ws/direct") as ws:
        assert await ws.receive_text() == "route"


@pytest.mark.asyncio
async def test_unmatched_path_still_raises(api):
    with pytest.raises(ValueError, match="No WebSocket handler found"):
        async with WebSocketTestClient(api, "/nowhere"):
            pass


@pytest.mark.asyncio
async def test_django_mount_does_not_take_websocket_connections():
    # Django's ASGI handler serves HTTP only. The path must stay unmatched.
    api = BoltAPI()
    api.mount_django("/django")

    with pytest.raises(ValueError, match="No WebSocket handler found"):
        async with WebSocketTestClient(api, "/django/ws"):
            pass


@pytest.mark.asyncio
async def test_websocket_mount_wins_over_root_django_mount():
    api = BoltAPI()
    api.mount_django("/")
    api.mount_asgi("/mounted", echo_app)

    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"

    with pytest.raises(ValueError, match="No WebSocket handler found"):
        async with WebSocketTestClient(api, "/other"):
            pass


@pytest.mark.asyncio
async def test_django_mount_with_custom_app_keeps_websocket():
    api = BoltAPI()
    api.mount_django("/custom", echo_app)

    async with WebSocketTestClient(api, "/custom/ws") as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(("trusted_proxies", "scheme"), [([], "ws"), (["127.0.0.1"], "wss")])
async def test_mounted_app_scheme_follows_x_forwarded_proto_only_from_a_trusted_proxy(api, trusted_proxies, scheme):
    """As on the server. The test client connects from 127.0.0.1."""
    with override_settings(BOLT_TRUSTED_PROXIES=trusted_proxies):
        async with WebSocketTestClient(api, "/scheme/ws", headers={"X-Forwarded-Proto": "https"}) as ws:
            assert await ws.receive_text() == scheme
