"""
Tests for the precompiled user-load fast path.

`load_user_by_pk_sync` replaces `User.objects.get(pk=...)` in the default
user loaders. Django recompiles the SELECT for a `.get()` on every call —
roughly two thirds of the per-request user-load cost measured under load —
so the framework compiles the pk query once per (model, alias) and executes
it directly, materializing the instance with `Model.from_db` exactly like a
QuerySet would.

These tests pin the behavior contract to the ORM path it replaces: same
instance data, same None-for-missing semantics, and end-to-end parity
through request.user.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import models

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.auth.user_loader import load_user_by_pk_sync
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db

User = get_user_model()

SECRET = "user-load-fastpath-test-secret-hs256"

# Mutable state read by FilteredAccountManager.get_queryset — a stand-in for
# any manager whose default filtering is dynamic (time windows, tenancy from
# a contextvar, feature flags). The compiled fast path MUST NOT freeze such
# filters, so these models get the per-call ORM fallback.
_VISIBLE_STATUS = {"value": "active"}


class FilteredAccountManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(status=_VISIBLE_STATUS["value"])


class FilteredAccount(models.Model):
    """User-model stand-in whose default manager filters dynamically."""

    username = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="active")

    objects = FilteredAccountManager()

    class Meta:
        app_label = "django_bolt"


def test_loads_same_user_as_orm_get():
    user = User.objects.create(
        username="fastpath",
        email="fast@example.com",
        first_name="Fast",
        is_staff=True,
    )
    orm_user = User.objects.get(pk=user.pk)

    loaded = load_user_by_pk_sync(User, str(user.pk))

    assert loaded is not None
    assert loaded.pk == orm_user.pk
    assert loaded.username == orm_user.username
    assert loaded.email == orm_user.email
    assert loaded.first_name == orm_user.first_name
    assert loaded.is_staff is True
    # Materialized like a real ORM row: attached to a database, not "adding".
    assert loaded._state.db == orm_user._state.db
    assert loaded._state.adding is False


def test_missing_user_returns_none():
    assert load_user_by_pk_sync(User, "999999999") is None


def test_invalid_pk_returns_none():
    # A stale/garbage token subject must not raise, matching the
    # DoesNotExist -> None contract of the loaders this replaces.
    assert load_user_by_pk_sync(User, "not-a-pk") is None


@pytest.fixture(autouse=True, scope="module")
def _filtered_account_table(django_db_setup, django_db_blocker):
    """Create the FilteredAccount table (test models are not in migrations)."""
    from django.db import connection  # noqa: PLC0415

    with django_db_blocker.unblock():
        if FilteredAccount._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(FilteredAccount)


def test_custom_manager_dynamic_filter_is_not_frozen():
    """A default manager with an overridden get_queryset must be re-evaluated
    per call, exactly like the `objects.get(pk=...)` this loader replaces —
    compiling its WHERE clause once would freeze dynamic filter values."""
    active = FilteredAccount._base_manager.create(username="active_acct", status="active")
    dormant = FilteredAccount._base_manager.create(username="dormant_acct", status="dormant")

    _VISIBLE_STATUS["value"] = "active"
    try:
        loaded = load_user_by_pk_sync(FilteredAccount, str(active.pk))
        assert loaded is not None and loaded.username == "active_acct"
        assert load_user_by_pk_sync(FilteredAccount, str(dormant.pk)) is None

        # Flip the dynamic filter: the same loader must now see the other row.
        _VISIBLE_STATUS["value"] = "dormant"
        loaded = load_user_by_pk_sync(FilteredAccount, str(dormant.pk))
        assert loaded is not None and loaded.username == "dormant_acct"
        assert load_user_by_pk_sync(FilteredAccount, str(active.pk)) is None
    finally:
        _VISIBLE_STATUS["value"] = "active"


def test_default_user_model_takes_compiled_path():
    """Perf tripwire: contrib's UserManager uses stock get_queryset, so the
    default user model must actually get the compiled statement — if this
    ever falls back, authenticated request throughput silently halves."""
    from django.db import DEFAULT_DB_ALIAS, router  # noqa: PLC0415

    from django_bolt.auth.pk_loader import _compiled_pk_query  # noqa: PLC0415

    alias = router.db_for_read(User) or DEFAULT_DB_ALIAS
    assert _compiled_pk_query(User, alias) is not None


def test_custom_get_user_sync_override_is_untouched():
    """Backends overriding get_user_sync (custom user queries) bypass the
    compiled fast path entirely — the override runs verbatim."""

    class CustomQueryAuth(JWTAuthentication):
        def get_user_sync(self, user_id):
            return User.objects.select_related().only("username").get(pk=user_id)

    user = User.objects.create(username="custom_query")
    loaded = CustomQueryAuth(secret=SECRET).get_user_sync(str(user.pk))
    assert loaded.username == "custom_query"
    assert loaded.get_deferred_fields()  # .only() projection survived


@pytest.mark.django_db(transaction=True)
def test_request_user_uses_fast_path_end_to_end():
    api = BoltAPI()

    @api.get("/whoami", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def whoami(request):
        user = request.user
        return {"username": user.username, "is_anonymous": bool(getattr(user, "is_anonymous", False))}

    user = User.objects.create(username="fastpath_e2e")
    token = create_jwt_for_user(user, secret=SECRET)

    with TestClient(api) as client:
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        assert response.json() == {"username": "fastpath_e2e", "is_anonymous": False}
