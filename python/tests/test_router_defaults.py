"""Router defaults, prefixes and nesting, checked through ``BoltAPI.include_router``."""

from __future__ import annotations

import time

import jwt

from django_bolt import BoltAPI, Router
from django_bolt.auth import AllowAny, IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient


def _token(secret: str) -> dict[str, str]:
    payload = {"sub": "user1", "exp": int(time.time()) + 3600, "iat": int(time.time())}
    return {"Authorization": "Bearer " + jwt.encode(payload, secret, algorithm="HS256")}


def test_router_defaults_apply_and_a_route_setting_wins():
    router = Router(
        prefix="/r",
        auth=[JWTAuthentication(secret="router-secret")],
        guards=[IsAuthenticated()],
    )

    @router.get("/default")
    async def default():
        return {"ok": True}

    @router.get("/own-auth", auth=[JWTAuthentication(secret="route-secret")])
    async def own_auth():
        return {"ok": True}

    @router.get("/own-guards", guards=[AllowAny()])
    async def own_guards():
        return {"ok": True}

    @router.get("/no-guards", guards=[])
    async def no_guards():
        return {"ok": True}

    plain = Router()

    @plain.get("/plain/x")
    async def plain_x():
        return {"ok": True}

    api = BoltAPI()
    api.include_router(router)
    api.include_router(plain)

    with TestClient(api) as client:
        assert client.get("/r/default").status_code == 401
        assert client.get("/r/default", headers=_token("router-secret")).status_code == 200
        assert client.get("/r/default", headers=_token("route-secret")).status_code == 401
        assert client.get("/r/own-auth", headers=_token("route-secret")).status_code == 200
        assert client.get("/r/own-auth", headers=_token("router-secret")).status_code == 401
        assert client.get("/r/own-guards").status_code == 200
        assert client.get("/r/no-guards").status_code == 200
        assert client.get("/plain/x").status_code == 200


def test_router_prefixes_combine_through_nesting_and_parent():
    top = Router(prefix="/aX/")
    middle = Router(prefix="/b/")
    leaf = Router(prefix="/c")
    via_parent = Router(prefix="/d", parent=top)

    @leaf.get("/x")
    async def leaf_x():
        return {"where": "leaf"}

    @via_parent.get("/y")
    async def parent_y():
        return {"where": "parent"}

    middle.include_router(leaf)
    top.include_router(middle)

    api = BoltAPI()
    api.include_router(top, prefix="/apiX/")

    with TestClient(api) as client:
        assert client.get("/apiX/aX/b/c/x").json() == {"where": "leaf"}
        assert client.get("/apiX/aX/d/y").json() == {"where": "parent"}


def test_middleware_chain_runs_parent_first():
    parent_entry = {"type": "rate_limit", "rps": 5, "burst": 5, "key": "ip"}
    child_entry = {"type": "rate_limit", "rps": 7, "burst": 7, "key": "ip"}
    parent = Router(prefix="/p", middleware=[parent_entry])
    child = Router(prefix="/c", middleware=[child_entry], parent=parent)

    assert child.get_middleware_chain() == [parent_entry, child_entry]
    assert parent.get_middleware_chain() == [parent_entry]

    included = Router(prefix="/i", middleware=[child_entry])

    @included.get("/x")
    async def included_x():
        return {}

    parent.include_router(included)
    assert included.get_middleware_chain() == [parent_entry, child_entry]
    assert [meta["_router_middleware"] for *_rest, meta in included.get_all_routes()] == [[parent_entry, child_entry]]
