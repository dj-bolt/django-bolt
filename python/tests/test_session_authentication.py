"""SessionAuthentication: Django session auth as a Bolt auth backend.

Django does the session work. Bolt adds Django's SessionMiddleware and
AuthenticationMiddleware to each route that uses the backend, and checks the
guards of the route against ``request.user``.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import alogin
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_bolt import BoltAPI, CurrentUser, Request, Router
from django_bolt.auth import (
    AllowAny,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
    SessionAuthentication,
    create_jwt_for_user,
)
from django_bolt.concurrency import in_lane_mode
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.testing import TestClient, WebSocketTestClient

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def _add_login(api: BoltAPI) -> None:
    """Add a login route that uses the session backend and Django's ``alogin``."""

    @api.post("/login", auth=[SessionAuthentication()], guards=[AllowAny()])
    async def login(request: Request, username: str):
        await alogin(request, await User.objects.aget(username=username))
        return {"ok": True}


def _login(client: TestClient, username: str) -> None:
    response = client.post(f"/login?username={username}", headers=SAME_ORIGIN)
    assert response.status_code == 200, response.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler_is_async", [True, False], ids=["async", "sync"])
def test_session_route_works_without_django_middleware_on_the_api(handler_is_async):
    """The backend adds the Django session and auth middleware to its route."""
    api = BoltAPI()
    _add_login(api)

    if handler_is_async:

        @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
        async def me(user: CurrentUser):
            return {"username": user.username}

    else:

        @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
        def me(user: CurrentUser):
            return {"username": user.username}

    User.objects.create_user(username="session_alice", password="pw-for-tests")
    with TestClient(api) as client:
        assert client.get("/me").status_code == 401
        _login(client, "session_alice")
        response = client.get("/me")

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_alice"}


@pytest.mark.django_db(transaction=True)
def test_a_stale_session_cookie_gets_401():
    """Rust cannot read the session, so Django decides. A bad cookie is anonymous."""
    api = BoltAPI()

    @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def me():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/me", headers={"cookie": "sessionid=not-a-session"})

    assert response.status_code == 401, response.text


@pytest.mark.django_db(transaction=True)
def test_requires_is_staff_checks_the_django_user():
    api = BoltAPI()
    _add_login(api)

    @api.get("/staff", auth=[SessionAuthentication()], guards=[Requires("is_staff", True)])
    async def staff():
        return {"ok": True}

    User.objects.create_user(username="session_member", password="pw-for-tests")
    User.objects.create_user(username="session_staff", password="pw-for-tests", is_staff=True)
    with TestClient(api) as member, TestClient(api) as staff_client:
        _login(member, "session_member")
        _login(staff_client, "session_staff")
        assert member.get("/staff").status_code == 403
        assert staff_client.get("/staff").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_requires_permissions_checks_the_django_user():
    api = BoltAPI()
    _add_login(api)

    @api.get("/users", auth=[SessionAuthentication()], guards=[Requires("permissions", "auth.view_user")])
    async def users():
        return {"ok": True}

    @api.get(
        "/safe",
        auth=[SessionAuthentication()],
        guards=[Requires("permissions", none_of=["auth.delete_user"])],
    )
    async def safe():
        return {"ok": True}

    viewer = User.objects.create_user(username="session_viewer", password="pw-for-tests")
    viewer.user_permissions.add(
        Permission.objects.get(codename="view_user"), Permission.objects.get(codename="delete_user")
    )
    User.objects.create_user(username="session_plain", password="pw-for-tests")
    with TestClient(api) as viewer_client, TestClient(api) as plain_client:
        _login(viewer_client, "session_viewer")
        _login(plain_client, "session_plain")
        assert viewer_client.get("/users").status_code == 200
        assert plain_client.get("/users").status_code == 403
        # Rust has no user for a session request. It must not pass an exclusion guard.
        assert viewer_client.get("/safe").status_code == 403
        assert plain_client.get("/safe").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_login_url_redirects_an_anonymous_request():
    api = BoltAPI()

    @api.get("/page", auth=[SessionAuthentication(login_url="/accounts/login/")], guards=[IsAuthenticated()])
    async def page():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/page?tab=1", follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/accounts/login/?next=/page%3Ftab%3D1"


@pytest.mark.django_db(transaction=True)
def test_session_cookie_requests_get_the_csrf_origin_check():
    api = BoltAPI()
    _add_login(api)

    @api.post("/items", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def create_item():
        return {"ok": True}

    User.objects.create_user(username="session_writer", password="pw-for-tests")
    with TestClient(api) as client:
        _login(client, "session_writer")
        assert client.post("/items", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.post("/items", headers=SAME_ORIGIN).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_a_valid_token_wins_over_a_stale_session_cookie():
    """Rust can verify a token. It tries the token backends before the session cookie."""
    api = BoltAPI()

    @api.get(
        "/me",
        auth=[SessionAuthentication(), JWTAuthentication(secret="session-test-secret-of-32-bytes!!")],
        guards=[IsAuthenticated()],
    )
    async def me(user: CurrentUser):
        return {"username": user.username}

    user = User.objects.create_user(username="session_token_user", password="pw-for-tests")
    token = create_jwt_for_user(user, secret="session-test-secret-of-32-bytes!!")
    with TestClient(api) as client:
        response = client.get("/me", headers={"authorization": f"Bearer {token}", "cookie": "sessionid=not-a-session"})

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_token_user"}


@pytest.mark.django_db(transaction=True)
def test_session_route_on_an_api_with_django_middleware():
    """The API stack has the session and auth middleware. Bolt adds no second copy."""
    api = BoltAPI(
        django_middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    )
    _add_login(api)

    @api.get("/staff", auth=[SessionAuthentication()], guards=[Requires("is_staff", True)])
    def staff(user: CurrentUser):
        return {"username": user.username, "lane": in_lane_mode()}

    User.objects.create_user(username="stack_member", password="pw-for-tests")
    User.objects.create_user(username="stack_staff", password="pw-for-tests", is_staff=True)
    with TestClient(api) as member, TestClient(api) as staff_client:
        _login(member, "stack_member")
        _login(staff_client, "stack_staff")
        assert member.get("/staff").status_code == 403
        response = staff_client.get("/staff")

    assert response.status_code == 200, response.text
    # The whole request, with the guard check, runs on the request lane.
    assert response.json() == {"username": "stack_staff", "lane": True}


@pytest.mark.django_db(transaction=True)
def test_session_backend_as_the_global_default():
    with override_settings(
        BOLT_AUTHENTICATION_CLASSES=[SessionAuthentication()],
        BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()],
    ):
        api = BoltAPI()
        _add_login(api)

        @api.get("/me")
        async def me(user: CurrentUser):
            return {"username": user.username}

        User.objects.create_user(username="session_default", password="pw-for-tests")
        with TestClient(api) as client:
            assert client.get("/me").status_code == 401
            _login(client, "session_default")
            response = client.get("/me")

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_default"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("login_url", [None, "/accounts/login/"], ids=["allow-any", "login-url"])
def test_a_duplicate_empty_session_cookie_does_not_skip_the_csrf_check(login_url):
    """Django reads the last ``sessionid`` value. An empty first value must not hide the cookie."""
    api = BoltAPI()
    _add_login(api)
    backend = SessionAuthentication(login_url=login_url)
    guards = [AllowAny()] if login_url is None else [IsAuthenticated()]

    @api.post("/act", auth=[backend], guards=guards)
    async def act(request: Request):
        user = await request.auser()
        return {"username": user.username}

    User.objects.create_user(username="session_victim", password="pw-for-tests")
    with TestClient(api) as client:
        _login(client, "session_victim")
        session_id = client.cookies["sessionid"]
    with TestClient(api) as attacker:
        response = attacker.post(
            "/act",
            headers={"cookie": f"sessionid=; sessionid={session_id}", "sec-fetch-site": "cross-site"},
        )

    assert response.status_code == 403, response.text


@pytest.mark.django_db(transaction=True)
def test_a_login_route_gets_the_csrf_check_with_no_cookie():
    """A cross-site page must not log a browser in to another account."""
    api = BoltAPI()
    _add_login(api)

    User.objects.create_user(username="session_login_target", password="pw-for-tests")
    with TestClient(api) as client:
        cross_site = client.post("/login?username=session_login_target", headers={"sec-fetch-site": "cross-site"})
        same_origin = client.post("/login?username=session_login_target", headers=SAME_ORIGIN)

    assert cross_site.status_code == 403, cross_site.text
    assert same_origin.status_code == 200, same_origin.text


@pytest.mark.django_db(transaction=True)
def test_csrf_false_turns_off_the_origin_check():
    api = BoltAPI()

    @api.post("/hook", auth=[SessionAuthentication(csrf=False)], guards=[AllowAny()])
    async def hook():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.post("/hook", headers={"sec-fetch-site": "cross-site"})

    assert response.status_code == 200, response.text


@pytest.mark.django_db(transaction=True)
def test_session_routes_in_a_router_and_a_mounted_api():
    api = BoltAPI()
    _add_login(api)
    router = Router(prefix="/router")
    sub_api = BoltAPI()

    @router.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def router_me(user: CurrentUser):
        return {"username": user.username}

    @sub_api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def sub_me(user: CurrentUser):
        return {"username": user.username}

    api.include_router(router)
    api.mount("/sub", sub_api)

    User.objects.create_user(username="session_nested", password="pw-for-tests")
    with TestClient(api) as client:
        for path in ("/router/me", "/sub/me"):
            assert client.get(path).status_code == 401, path
        _login(client, "session_nested")
        for path in ("/router/me", "/sub/me"):
            response = client.get(path)
            assert response.status_code == 200, f"{path}: {response.text}"
            assert response.json() == {"username": "session_nested"}


def test_only_one_of_the_two_django_middleware_is_an_error():
    """A second SessionMiddleware would load and save the session a second time."""
    api = BoltAPI(django_middleware=["django.contrib.sessions.middleware.SessionMiddleware"])

    with pytest.raises(ImproperlyConfigured, match="AuthenticationMiddleware"):

        @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
        async def me():
            return {"ok": True}


def test_none_of_on_a_claim_that_a_session_user_lacks_is_an_error():
    """A session user has no ``role`` claim, so ``none_of`` would let every user in."""
    api = BoltAPI()

    with pytest.raises(ImproperlyConfigured, match="none_of"):

        @api.get("/page", auth=[SessionAuthentication()], guards=[Requires("role", none_of=["banned"])])
        async def page():
            return {"ok": True}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_session_default_is_dropped_on_a_websocket_route():
    """A WebSocket route cannot use the session backend. The other default backends stay."""
    user = await User.objects.acreate(username="session_ws_user")
    token = create_jwt_for_user(user, secret="session-test-secret-of-32-bytes!!")
    with override_settings(
        BOLT_AUTHENTICATION_CLASSES=[
            SessionAuthentication(),
            JWTAuthentication(secret="session-test-secret-of-32-bytes!!"),
        ],
        BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()],
    ):
        api = BoltAPI()

        @api.websocket("/ws")
        async def ws(websocket):
            await websocket.accept()
            await websocket.send_text("hello")

        async with WebSocketTestClient(api, "/ws", headers={"authorization": f"Bearer {token}"}) as client:
            assert await client.receive_text() == "hello"
        # A session cookie is no credential on a WebSocket. The guards reject it.
        with pytest.raises(PermissionError, match="Authentication required"):
            async with WebSocketTestClient(api, "/ws", headers={"cookie": "sessionid=abc"}):
                pass


def test_session_backend_on_a_websocket_route_is_an_error():
    api = BoltAPI()

    with pytest.raises(ImproperlyConfigured, match="SessionAuthentication"):

        @api.websocket("/ws", auth=[SessionAuthentication()])
        async def ws(websocket):
            await websocket.accept()


def test_session_route_has_a_cookie_security_scheme_in_the_schema():
    api = BoltAPI()

    @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def me():
        return {"ok": True}

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test", version="1")).generate().to_schema()

    assert schema["components"]["securitySchemes"]["SessionAuth"] == {
        "type": "apiKey",
        "name": "sessionid",
        "in": "cookie",
    }
    assert schema["paths"]["/me"]["get"]["security"] == [{"SessionAuth": []}]
