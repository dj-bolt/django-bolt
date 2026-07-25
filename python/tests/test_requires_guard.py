"""
Integration tests for `Requires` — the single permission guard.

`Requires(claim, *values, all_of=...)` is the one way to express a permission
check. Its claim name and expected values are extracted from Python ONCE at
registration and compiled into a native Rust check — request-time guard
evaluation never calls back into Python. These tests cover the allow/deny
semantics, any-of/all-of matching, claim-type handling, the unified
`permissions` claim (JWT and API-key), and the registration-time failures
that must never silently leave a route open.

Tokens are minted with the public `create_jwt_for_user()` API against real
Django users, so the tests exercise the same claim layout applications get.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured

from django_bolt import BoltAPI
from django_bolt.auth import (
    AllowAny,
    APIKeyAuthentication,
    BasePermission,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
    create_jwt_for_user,
)
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db

SECRET = "requires-guard-test-secret-long-enough-for-hs256"

# The one way to define a reusable custom check: name a Requires instance.
IsClient = Requires("role", "client")

JWT_AUTH = [JWTAuthentication(secret=SECRET)]

# One shared API for every fixed-guard route. TestClient boots the full
# Rust/Actix pipeline, so the tests share a single module-scoped client
# instead of paying that startup once per test.
guarded_api = BoltAPI()


def _route(path, guards, auth=JWT_AUTH):
    @guarded_api.get(path, auth=auth, guards=guards)
    async def handler():
        return {"ok": True}


_route("/authed-client", [IsAuthenticated(), IsClient])
_route("/client", [IsClient])
_route("/client-then-allow", [IsClient, AllowAny()])
_route("/role-any", [Requires("role", "client", "vip")])
_route("/tenant-present", [Requires("tenant_id")])
_route("/roles-member", [Requires("roles", "client")])
_route("/roles-all", [Requires("roles", all_of=["a", "b"])])
_route("/level-5", [Requires("level", 5)])
_route("/staff-true", [Requires("is_staff", True)])
_route("/perm-delete", [Requires("permissions", "users.delete")])
_route("/perm-all", [Requires("permissions", all_of=["users.create", "users.delete"])])
_route(
    "/key-perm",
    [Requires("permissions", "can.read")],
    auth=[
        APIKeyAuthentication(
            api_keys={"key-1", "key-2"},
            key_permissions={"key-1": ["can.read"]},
        )
    ],
)
_route("/key-role", [Requires("role", "client")], auth=[APIKeyAuthentication(api_keys={"key-1"})])


@pytest.fixture(scope="module")
def client():
    with TestClient(guarded_api) as c:
        yield c


def bearer(user, **extra_claims) -> dict[str, str]:
    """Authorization header for `user`, with any extra JWT claims."""
    token = create_jwt_for_user(user, secret=SECRET, extra_claims=extra_claims or None)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user():
    return get_user_model().objects.create(username="regular", email="regular@example.com")


def make_api(guards, auth=None):
    """A throwaway API for guards that can't be shared routes
    (registration failures, per-user claim values)."""
    api = BoltAPI()
    backends = auth if auth is not None else JWT_AUTH

    @api.get("/protected", auth=backends, guards=guards)
    async def protected():
        return {"ok": True}

    return api


# --- Enforcement -----------------------------------------------------------


class TestRequiresEnforcement:
    def test_allows_matching_claim(self, client, user):
        resp = client.get("/authed-client", headers=bearer(user, role="client"))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_denies_wrong_claim(self, client, user):
        resp = client.get("/authed-client", headers=bearer(user, role="vendor"))
        assert resp.status_code == 403

    def test_denies_missing_claim(self, client, user):
        resp = client.get("/authed-client", headers=bearer(user))
        assert resp.status_code == 403

    def test_denies_unauthenticated_with_401(self, client):
        """No credential at all → 401, same convention as IsAuthenticated."""
        resp = client.get("/client")
        assert resp.status_code == 401

    def test_earlier_guard_short_circuits(self, client):
        """IsAuthenticated fails first → 401 before the claim check."""
        resp = client.get("/authed-client")
        assert resp.status_code == 401

    def test_allow_any_after_deny_does_not_resurrect(self, client, user):
        """Guards evaluate in order: the denial is reached before AllowAny."""
        resp = client.get("/client-then-allow", headers=bearer(user, role="vendor"))
        assert resp.status_code == 403


class TestMatchingSemantics:
    def test_any_of_values(self, client, user):
        vip = client.get("/role-any", headers=bearer(user, role="vip"))
        vendor = client.get("/role-any", headers=bearer(user, role="vendor"))
        assert vip.status_code == 200
        assert vendor.status_code == 403

    def test_presence_only(self, client, user):
        """Requires with no values means the claim just has to exist."""
        present = client.get("/tenant-present", headers=bearer(user, tenant_id="acme"))
        absent = client.get("/tenant-present", headers=bearer(user))
        assert present.status_code == 200
        assert absent.status_code == 403

    def test_list_claim_matches_on_membership(self, client, user):
        """A list claim (roles: [...]) passes when it contains an expected value."""
        member = client.get("/roles-member", headers=bearer(user, roles=["beta", "client"]))
        non_member = client.get("/roles-member", headers=bearer(user, roles=["beta"]))
        assert member.status_code == 200
        assert non_member.status_code == 403

    def test_all_of_requires_every_value(self, client, user):
        both = client.get("/roles-all", headers=bearer(user, roles=["a", "b", "c"]))
        one = client.get("/roles-all", headers=bearer(user, roles=["a"]))
        assert both.status_code == 200
        assert one.status_code == 403

    def test_int_and_bool_values(self, client, user):
        ok = client.get("/level-5", headers=bearer(user, level=5))
        bad = client.get("/level-5", headers=bearer(user, level=4))
        assert ok.status_code == 200
        assert bad.status_code == 403

        # regular user is not staff → is_staff claim is False
        resp = client.get("/staff-true", headers=bearer(user))
        assert resp.status_code == 403

    def test_standard_claim_is_reachable(self, user):
        """Typed claims (sub, is_staff, ...) are guardable, not just extras."""
        with TestClient(make_api([Requires("sub", str(user.id))])) as client:
            resp = client.get("/protected", headers=bearer(user))
        assert resp.status_code == 200


class TestPermissionsClaim:
    """`permissions` reads the unified permission set — JWT and API keys."""

    def test_jwt_permission(self, client, user):
        ok = client.get("/perm-delete", headers=bearer(user, permissions=["users.delete"]))
        bad = client.get("/perm-delete", headers=bearer(user, permissions=["users.view"]))
        assert ok.status_code == 200
        assert bad.status_code == 403

    def test_jwt_all_of_permissions(self, client, user):
        both = client.get(
            "/perm-all",
            headers=bearer(user, permissions=["users.create", "users.delete", "x"]),
        )
        one = client.get("/perm-all", headers=bearer(user, permissions=["users.create"]))
        assert both.status_code == 200
        assert one.status_code == 403

    def test_api_key_permissions(self, client):
        """key_permissions from APIKeyAuthentication satisfy the same guard."""
        granted = client.get("/key-perm", headers={"X-API-Key": "key-1"})
        denied = client.get("/key-perm", headers={"X-API-Key": "key-2"})
        assert granted.status_code == 200
        assert denied.status_code == 403

    def test_api_key_cannot_satisfy_other_claims(self, client):
        """API keys carry no claims, so non-permission claims never match."""
        resp = client.get("/key-role", headers={"X-API-Key": "key-1"})
        assert resp.status_code == 403


# --- Registration-time failures: never silently leave a route open ---------


class TestRegistrationFailures:
    def test_empty_claim_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="non-empty string"):
            Requires("")

    def test_non_scalar_value_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="str, int, float, or bool"):
            Requires("role", {"nested": "dict"})

    def test_values_and_all_of_are_mutually_exclusive(self):
        with pytest.raises(ImproperlyConfigured, match="not both"):
            Requires("permissions", "a", all_of=["b"])

    def test_empty_all_of_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="must not be empty"):
            Requires("permissions", all_of=[])

    def test_string_all_of_rejected(self):
        """all_of='perm' would iterate characters — reject it."""
        with pytest.raises(ImproperlyConfigured, match="list/tuple"):
            Requires("permissions", all_of="users.delete")

    def test_base_permission_subclass_is_not_a_guard(self):
        """Subclassing is not the way to build checks — Requires is."""

        class HomeMade(BasePermission):
            __slots__ = ()

        with pytest.raises(ImproperlyConfigured, match="Requires"):
            make_api([HomeMade()])

    def test_non_guard_object_fails_loudly(self):
        with pytest.raises(ImproperlyConfigured, match="BasePermission"):
            make_api([object()])
