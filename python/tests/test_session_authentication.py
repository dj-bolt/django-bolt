"""SessionAuthentication: Django session auth as a Bolt auth backend.

Django does the session work. Bolt adds Django's SessionMiddleware and
AuthenticationMiddleware to each route that uses the backend, and checks the
guards of the route against ``request.user``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import alogin
from django.contrib.auth.models import Permission, User
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from django_bolt import BoltAPI, CurrentUser, Request, Router
from django_bolt.auth import (
    AllowAny,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
    SessionAuthentication,
    create_jwt_for_user,
)
from django_bolt.auth.session import auth_without_session
from django_bolt.concurrency import in_lane_mode
from django_bolt.error_handlers import problem_details
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.testing import TestClient, WebSocketTestClient

from .test_models import User as UserWithoutSuperuser

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
TOKEN_SECRET = "session-test-secret-of-32-bytes!!"


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
@pytest.mark.parametrize("error_formatter", [None, problem_details], ids=["default", "problem-details"])
def test_a_session_rejection_is_the_same_response_as_a_rejection_in_rust(error_formatter):
    """Python rejects a request with a session cookie. A client gets the response that Rust sends."""
    api = BoltAPI(error_formatter=error_formatter)
    _add_login(api)

    @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def me():
        return {"ok": True}

    # Rust checks the guards of a token request, and Python those of a session request.
    @api.get(
        "/staff",
        auth=[JWTAuthentication(), SessionAuthentication()],
        guards=[Requires("is_staff", True, message="Staff only")],
    )
    async def staff():
        return {"ok": True}

    user = User.objects.create_user(username="session_member", password="pw-for-tests")
    with TestClient(api) as client:
        rust_401 = client.get("/me")
        python_401 = client.get("/me", headers={"cookie": "sessionid=not-a-session"})
        token = create_jwt_for_user(user)
        rust_403 = client.get("/staff", headers={"authorization": f"Bearer {token}"})
        _login(client, "session_member")
        python_403 = client.get("/staff")

    def seen(response):
        return (
            response.status_code,
            response.headers["content-type"],
            response.json(),
            response.headers.get("www-authenticate"),
        )

    assert seen(python_401) == seen(rust_401)
    assert seen(python_403) == seen(rust_403)
    if error_formatter is None:
        assert seen(python_401) == (401, "application/json", {"detail": "Authentication required"}, "Bearer")
        assert seen(python_403) == (403, "application/json", {"detail": "Staff only"}, None)
    else:
        assert python_401.headers["content-type"] == "application/problem+json"
        assert python_403.json()["detail"] == "Staff only"


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
def test_requires_reads_is_superuser_and_sub_of_the_django_user_and_no_other_claim():
    api = BoltAPI()
    _add_login(api)
    User.objects.create_user(username="session_only_staff", password="pw-for-tests", is_staff=True)
    superuser = User.objects.create_superuser(username="session_root", password="pw-for-tests")

    @api.get("/root", auth=[SessionAuthentication()], guards=[Requires("is_superuser", True)])
    async def root():
        return {"ok": True}

    # ``sub`` is the primary key of the user, as in a token.
    @api.get("/owner", auth=[SessionAuthentication()], guards=[Requires("sub", str(superuser.pk))])
    async def owner():
        return {"ok": True}

    @api.get(
        "/tenant",
        auth=[JWTAuthentication(secret=TOKEN_SECRET), SessionAuthentication()],
        guards=[Requires("tenant_id", message="Use a tenant token")],
    )
    async def tenant():
        return {"ok": True}

    token = create_jwt_for_user(superuser, secret=TOKEN_SECRET, extra_claims={"tenant_id": "t1"})
    with TestClient(api) as staff_client, TestClient(api) as root_client, TestClient(api) as token_client:
        _login(staff_client, "session_only_staff")
        _login(root_client, "session_root")
        staff_root = staff_client.get("/root")
        assert staff_client.get("/owner").status_code == 403
        assert root_client.get("/root").status_code == 200
        assert root_client.get("/owner").status_code == 200
        # A session user has no ``tenant_id`` claim, also a superuser.
        root_tenant = root_client.get("/tenant")
        # Rust checked the claim of the token. Python does not check the token user as a session user.
        assert token_client.get("/tenant", headers={"authorization": f"Bearer {token}"}).status_code == 200

    assert (staff_root.status_code, staff_root.json()) == (403, {"detail": "Permission denied"})
    assert (root_tenant.status_code, root_tenant.json()) == (403, {"detail": "Use a tenant token"})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["inactive", "deleted", "expired"])
def test_a_session_that_django_accepts_no_more_gets_401(change):
    """Django loads the user for each request. An old cookie gives no access."""
    api = BoltAPI()
    _add_login(api)

    # The handler reads no user. Only the guard check can reject the request.
    @api.get("/page", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def page():
        return {"ok": True}

    user = User.objects.create_user(username="session_leaver", password="pw-for-tests")
    with TestClient(api) as client:
        _login(client, "session_leaver")
        assert client.get("/page").status_code == 200
        if change == "inactive":
            User.objects.filter(pk=user.pk).update(is_active=False)
        elif change == "deleted":
            user.delete()
        else:
            Session.objects.filter(session_key=client.cookies["sessionid"]).update(
                expire_date=timezone.now() - timedelta(seconds=1)
            )
        response = client.get("/page")

    assert (response.status_code, response.json()) == (401, {"detail": "Authentication required"})


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
def test_login_url_redirects_only_a_request_with_no_user():
    """A user that a guard rejects gets 403. The login page cannot help that user."""
    api = BoltAPI()
    _add_login(api)

    # The session backend is the second backend. Rust reports its position.
    @api.get(
        "/staff",
        auth=[
            JWTAuthentication(secret=TOKEN_SECRET),
            SessionAuthentication(login_url="/accounts/login/"),
        ],
        guards=[Requires("is_staff", True)],
    )
    async def staff():
        return {"ok": True}

    User.objects.create_user(username="session_no_staff", password="pw-for-tests")
    with TestClient(api) as client:
        stale = client.get("/staff", headers={"cookie": "sessionid=not-a-session"}, follow_redirects=False)
        _login(client, "session_no_staff")
        rejected = client.get("/staff", follow_redirects=False)

    assert stale.status_code == 302, stale.text
    assert stale.headers["location"] == "/accounts/login/?next=/staff"
    # The redirect has no body, so it has no content type.
    assert stale.content == b""
    assert "content-type" not in stale.headers
    assert (rejected.status_code, rejected.json()) == (403, {"detail": "Permission denied"})


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


@pytest.mark.parametrize(
    ("middleware", "have", "need"),
    [
        ("django.contrib.sessions.middleware.SessionMiddleware", "SessionMiddleware", "AuthenticationMiddleware"),
        ("django.contrib.auth.middleware.AuthenticationMiddleware", "AuthenticationMiddleware", "SessionMiddleware"),
    ],
    ids=["session-only", "auth-only"],
)
def test_the_error_for_one_of_the_two_django_middleware_names_the_missing_one(middleware, have, need):
    api = BoltAPI(django_middleware=[middleware])

    with pytest.raises(ImproperlyConfigured) as raised:

        @api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
        async def me():
            return {"ok": True}

    assert str(raised.value) == (
        "SessionAuthentication on GET /me needs Django's SessionMiddleware and AuthenticationMiddleware. "
        f"The middleware of the route has {have} but not {need}. Add {need} to the same middleware list."
    )


def test_a_websocket_route_keeps_the_defaults_when_they_have_no_session_backend():
    with override_settings(BOLT_AUTHENTICATION_CLASSES=[JWTAuthentication(secret="session-test-secret-of-32-bytes!!")]):
        assert auth_without_session(None, "WebSocket route /ws") is None


def test_a_websocket_route_uses_its_own_auth_without_a_change():
    backends = [JWTAuthentication(secret="session-test-secret-of-32-bytes!!")]

    assert auth_without_session(backends, "WebSocket route /ws") is backends


def test_none_of_on_a_claim_that_a_session_user_lacks_is_an_error():
    """A session user has no ``role`` claim, so ``none_of`` would let every user in."""
    api = BoltAPI()

    with pytest.raises(ImproperlyConfigured, match="none_of"):

        @api.get("/page", auth=[SessionAuthentication()], guards=[Requires("role", none_of=["banned"])])
        async def page():
            return {"ok": True}


def test_a_permissions_guard_needs_the_sync_permissions_method_of_the_user_model(monkeypatch):
    """The guard check calls ``user.get_all_permissions()``, so startup checks that method."""

    class SyncPermissionsUser:
        is_authenticated = True

        def get_all_permissions(self):
            return {"reports.view"}

    class AsyncPermissionsUser:
        is_authenticated = True

        async def aget_all_permissions(self):
            return {"reports.view"}

    def add_route(user_model):
        monkeypatch.setattr("django_bolt.auth.session.get_user_model", lambda: user_model)
        api = BoltAPI()

        @api.get("/reports", auth=[SessionAuthentication()], guards=[Requires("permissions", "reports.view")])
        async def reports():
            return {"ok": True}

    add_route(SyncPermissionsUser)
    with pytest.raises(ImproperlyConfigured) as error:
        add_route(AsyncPermissionsUser)

    assert str(error.value) == (
        "A guard on GET /reports reads permissions of the session user, "
        "but the user model AsyncPermissionsUser has no get_all_permissions."
    )


@pytest.mark.parametrize(
    ("guard", "message"),
    [
        (Requires("is_superuser", True), "reads is_superuser"),
        (Requires("permissions", "auth.view_user"), "reads permissions"),
        (Requires("is_staff", True), None),
    ],
    ids=["is_superuser", "permissions", "is_staff"],
)
def test_a_guard_that_reads_a_field_that_the_user_model_lacks_is_an_error(guard, message):
    """The user model of the test has ``is_staff`` only. Startup fails for the other fields."""
    api = BoltAPI()

    def add_route():
        @api.get("/page", auth=[SessionAuthentication()], guards=[guard])
        async def page():
            return {"ok": True}

    with override_settings(AUTH_USER_MODEL=UserWithoutSuperuser._meta.label):
        if message is None:
            add_route()
        else:
            with pytest.raises(ImproperlyConfigured, match=message):
                add_route()


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
