"""Reproductions of suspected auth bugs. Each test asserts the ACTUAL behavior and names the expected one.

Not part of the suite. To run it, copy it into a worktree and remove it again:
    cp /home/user/wt/test-auth-backends-mutants.repro_auth_bugs.py <worktree>/python/tests/test_repro_auth_bugs.py
    cd <worktree> && uv run --project /home/user/django-bolt --no-sync python -m pytest python/tests/test_repro_auth_bugs.py -q -p no:cacheprovider
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from django.contrib.auth import alogin
from django.contrib.auth.models import User

from django_bolt import BoltAPI, CurrentUser, Request
from django_bolt.auth import (
    AllowAny,
    APIKeyAuthentication,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
    SessionAuthentication,
    create_jwt_for_user,
)
from django_bolt.error_handlers import problem_details
from django_bolt.testing import TestClient

SECRET = "repro-secret-repro-secret-repro-secret"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def tok(**claims):
    return pyjwt.encode({"sub": "1", "exp": int(time.time()) + 600, **claims}, SECRET, algorithm="HS256")


def test_bug1_an_empty_api_key_is_a_valid_key():
    api = BoltAPI()

    @api.get("/internal", auth=[APIKeyAuthentication(api_keys={""})], guards=[IsAuthenticated()])
    async def internal(request: Request):
        return {"user_id": request.context["user_id"]}

    with TestClient(api) as client:
        assert client.get("/internal").status_code == 401
        response = client.get("/internal", headers={"x-api-key": ""})
    # ACTUAL: 200. EXPECTED: ImproperlyConfigured at construction, or 401.
    assert (response.status_code, response.json()) == (200, {"user_id": "apikey:"})


def test_bug1b_a_str_for_api_keys_makes_each_character_a_valid_key():
    api = BoltAPI()

    @api.get("/internal", auth=[APIKeyAuthentication(api_keys="sk-prod-123abc")], guards=[IsAuthenticated()])
    async def internal(request: Request):
        return {"user_id": request.context["user_id"]}

    with TestClient(api) as client:
        real_key = client.get("/internal", headers={"x-api-key": "sk-prod-123abc"})
        one_character = client.get("/internal", headers={"x-api-key": "s"})
    # ACTUAL: the key is rejected and "s" is accepted. EXPECTED: ImproperlyConfigured, as Requires(all_of="x") gives.
    assert real_key.status_code == 401
    assert (one_character.status_code, one_character.json()) == (200, {"user_id": "apikey:s"})


@pytest.mark.django_db(transaction=True)
def test_bug2_session_guard_rejections_skip_the_error_formatter_and_the_challenge():
    api = BoltAPI(error_formatter=problem_details)

    @api.post("/login", auth=[SessionAuthentication()], guards=[AllowAny()])
    async def login(request: Request, username: str):
        await alogin(request, await User.objects.aget(username=username))
        return {"ok": True}

    @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def me():
        return {"ok": True}

    @api.get("/staff", auth=[SessionAuthentication()], guards=[Requires("is_staff", True)])
    async def staff():
        return {"ok": True}

    @api.get("/jwt-staff", auth=[JWTAuthentication(secret=SECRET)], guards=[Requires("is_staff", True)])
    async def jwt_staff():
        return {"ok": True}

    User.objects.create_user(username="repro_member", password="pw")
    with TestClient(api) as client:
        no_cookie = client.get("/me")
        stale_cookie = client.get("/me", headers={"cookie": "sessionid=stale"})
        jwt_403 = client.get("/jwt-staff", headers={"authorization": "Bearer " + tok()})
        client.post("/login?username=repro_member", headers=SAME_ORIGIN)
        session_403 = client.get("/staff")

    # Rust rejects the request with no cookie: formatted, with the challenge.
    assert no_cookie.status_code == 401
    assert no_cookie.headers["content-type"] == "application/problem+json"
    assert no_cookie.headers["www-authenticate"] == "Bearer"
    assert jwt_403.headers["content-type"] == "application/problem+json"
    # ACTUAL for the same route with a stale cookie: the default body, no challenge.
    # EXPECTED: the same response as above.
    assert stale_cookie.status_code == 401
    assert stale_cookie.headers["content-type"] == "application/json"
    assert stale_cookie.json() == {"detail": "Authentication required"}
    assert "www-authenticate" not in stale_cookie.headers
    assert session_403.status_code == 403
    assert session_403.headers["content-type"] == "application/json"
    assert session_403.json() == {"detail": "Permission denied"}


@pytest.mark.django_db(transaction=True)
def test_bug3_request_user_of_a_token_with_no_user_row_is_not_none():
    api = BoltAPI()

    @api.get("/user", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    def read_user(request: Request):
        user = request.user
        return {"is_none": user is None, "falsy": not user, "type": type(user).__name__}

    @api.get("/profile", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def profile(request):  # the first example of docs/src/topics/authentication.md
        return {"user_id": request.user.id}

    @api.get("/auser", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def read_auser(request: Request):
        return {"is_none": (await request.auser()) is None}

    headers = {"authorization": "Bearer " + tok(sub="424242")}
    with TestClient(api, raise_server_exceptions=False) as client:
        user = client.get("/user", headers=headers)
        profile_response = client.get("/profile", headers=headers)
        auser = client.get("/auser", headers=headers)
    # ACTUAL: a falsy LazyUser. types.py Request.user documents "None if ... user not found".
    assert user.json() == {"is_none": False, "falsy": True, "type": "LazyUser"}
    # ACTUAL: 500 (AttributeError: 'NoneType' object has no attribute 'id'). EXPECTED: 401, or a documented rule.
    assert profile_response.status_code == 500
    assert auser.json() == {"is_none": True}


def test_bug4_request_user_with_no_token_is_none_not_anonymous_user():
    api = BoltAPI()

    @api.get("/profile", auth=[JWTAuthentication(secret=SECRET)])
    async def profile(request: Request):
        return {"user": repr(request.user), "auser": type(await request.auser()).__name__}

    with TestClient(api) as client:
        response = client.get("/profile")
    # docs/src/topics/authentication.md: "request.user = AnonymousUser". ACTUAL: None.
    assert response.json() == {"user": "None", "auser": "AnonymousUser"}


@pytest.mark.django_db(transaction=True)
def test_bug5_a_jwt_of_an_inactive_user_authenticates_and_a_session_does_not():
    api = BoltAPI()

    @api.post("/login", auth=[SessionAuthentication()], guards=[AllowAny()])
    async def login(request: Request, username: str):
        await alogin(request, await User.objects.aget(username=username))
        return {"ok": True}

    @api.get("/me", auth=[JWTAuthentication(secret=SECRET), SessionAuthentication()], guards=[IsAuthenticated()])
    async def me(user: CurrentUser):
        return {"username": user.username, "is_active": user.is_active}

    user = User.objects.create_user(username="repro_leaver", password="pw")
    token = create_jwt_for_user(user, secret=SECRET)
    with TestClient(api) as client:
        client.post("/login?username=repro_leaver", headers=SAME_ORIGIN)
        User.objects.filter(pk=user.pk).update(is_active=False)
        by_session = client.get("/me")
        by_token = client.get("/me", headers={"authorization": "Bearer " + token})
    assert by_session.status_code == 401
    # ACTUAL: 200 with the inactive user. Django's ModelBackend.get_user gives None for an inactive user.
    assert (by_token.status_code, by_token.json()) == (200, {"username": "repro_leaver", "is_active": False})


def test_bug6_the_bearer_scheme_is_case_sensitive():
    api = BoltAPI()

    @api.get("/p", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def p():
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/p", headers={"authorization": "Bearer " + tok()}).status_code == 200
        # RFC 9110 section 11.1: the scheme name is case-insensitive. ACTUAL: 401.
        assert client.get("/p", headers={"authorization": "bearer " + tok()}).status_code == 401
        # A token with no scheme is accepted.
        assert client.get("/p", headers={"authorization": tok()}).status_code == 200


def test_bug7_api_key_configuration_that_has_no_effect():
    api = BoltAPI()

    @api.get(
        "/perm",
        auth=[APIKeyAuthentication(key_permissions={"sk-reader": {"read"}})],
        guards=[Requires("permissions", "read")],
    )
    async def perm():
        return {"ok": True}

    @api.get(
        "/prefixed",
        auth=[APIKeyAuthentication(api_keys={"Bearer abc"}, header="authorization")],
        guards=[IsAuthenticated()],
    )
    async def prefixed():
        return {"ok": True}

    with TestClient(api) as client:
        # key_permissions without api_keys: accepted, and no key authenticates.
        assert client.get("/perm", headers={"x-api-key": "sk-reader"}).status_code == 401
        # A key that starts with "Bearer " or "ApiKey " never authenticates: Rust removes the prefix first.
        assert client.get("/prefixed", headers={"authorization": "Bearer abc"}).status_code == 401
        assert client.get("/prefixed", headers={"authorization": "Bearer Bearer abc"}).status_code == 200
