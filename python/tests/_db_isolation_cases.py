"""Cases for ``test_db_isolation.py``, which runs them in a child pytest run.

The name does not start with ``test_``, so the main run does not collect this file.
pytest-django sorts the tests. The tests with ``db`` or a ``django_db`` marker run first.
The writer and the reader run last, in the order of this file.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from django_bolt.auth.revocation_db.models import RevokedToken


def test_db_then_transactional_db_is_refused(db, request):
    """The test already runs in one transaction, so it cannot become transactional."""
    request.getfixturevalue("transactional_db")


def test_writer(request):
    request.getfixturevalue("transactional_db")
    expires_at = datetime.now(UTC) + timedelta(days=1)

    # The async ORM call runs on a thread of the ORM pool, which has its own connection.
    asyncio.run(RevokedToken.objects.acreate(jti="row-of-the-writer", expires_at=expires_at))


def test_reader_starts_with_an_empty_table(request):
    request.getfixturevalue("transactional_db")

    assert asyncio.run(RevokedToken.objects.filter(jti="row-of-the-writer").aexists()) is False


@pytest.mark.django_db
def test_django_db_then_transactional_db_is_refused(request):
    request.getfixturevalue("transactional_db")


@pytest.mark.django_db(transaction=True)
def test_django_db_transaction_then_transactional_db_is_accepted(request):
    request.getfixturevalue("transactional_db")
