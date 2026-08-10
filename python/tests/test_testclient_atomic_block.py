"""
TestClient behavior when the test wraps itself in a transaction.

`TestClient` drives requests through the Rust pipeline, so handlers run on
framework threads with their own database connections. Django's `TestCase`
(and pytest-django's plain `django_db`) wrap each test in an uncommitted
transaction on the *calling* thread's connection, so rows created by the test
are invisible to those handler threads — and on SQLite the held write lock
turns the handler's query into `database table is locked`, surfacing as an
opaque 500.

`TransactionTestCase` / `django_db(transaction=True)` commit instead, which is
why every database-touching TestClient test in this suite uses the
transactional variant. Nothing warned about the difference, so the failure
mode was a 500 with no pointer to the cause (issue #276).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from django_bolt import BoltAPI
from django_bolt.testing import TestClient
from django_bolt.testing import client as client_module
from django_bolt.testing.client import BoltTestClientWarning

User = get_user_model()


@pytest.fixture(autouse=True)
def reset_warning_latch(monkeypatch):
    """The advisory fires once per process; these tests each need a fresh latch."""
    monkeypatch.setattr(client_module, "_atomic_block_warning_emitted", False)


def _advisories(recorded) -> list:
    """The BoltTestClientWarnings among everything pytest recorded."""
    return [w for w in recorded.list if issubclass(w.category, BoltTestClientWarning)]


def _make_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/users")
    async def list_users():
        return User.objects.values("username")

    @api.get("/ping")
    async def ping():
        return {"ok": True}

    return api


@pytest.mark.django_db
def test_warns_when_entered_inside_atomic_block():
    """A non-transactional test wraps an atomic block — handlers cannot see it."""
    assert connection.in_atomic_block, "precondition: plain django_db is transactional"

    with pytest.warns(BoltTestClientWarning, match="TransactionTestCase"), TestClient(_make_api()):
        pass


@pytest.mark.django_db(transaction=True)
def test_no_warning_under_transactional_db(recwarn):
    """The supported pattern must stay silent."""
    assert not connection.in_atomic_block

    with TestClient(_make_api()):
        pass

    assert not _advisories(recwarn)


def test_no_warning_without_database_access(recwarn):
    """No database involvement at all — nothing to warn about."""
    with TestClient(_make_api()) as client:
        assert client.get("/ping").status_code == 200

    assert not _advisories(recwarn)


@pytest.mark.django_db
def test_warning_is_emitted_only_once_per_process(recwarn):
    """Repeated clients must not flood a suite that mixes marker styles."""
    with TestClient(_make_api()):
        pass
    assert len(_advisories(recwarn)) == 1

    with TestClient(_make_api()):
        pass
    assert len(_advisories(recwarn)) == 1


@pytest.mark.django_db(transaction=True)
def test_handler_sees_committed_test_data():
    """The behavior the warning steers people toward actually works."""
    User.objects.create(username="visible-to-handler", email="v@example.com")

    with TestClient(_make_api()) as client:
        response = client.get("/users")
        assert response.status_code == 200
        assert [row["username"] for row in response.json()] == ["visible-to-handler"]
