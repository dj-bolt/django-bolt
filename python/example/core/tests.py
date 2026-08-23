"""Executable proof for the "Using Django's test runner" section of the docs.

The docs make three claims about `manage.py test`. This module runs all three
against the real example project, with the real Rust pipeline.
"""

from __future__ import annotations

import warnings

from django.db import connection
from django.test import TestCase, TransactionTestCase

from core.api import api
from core.models import Blog
from django_bolt.testing import BoltTestClientWarning, TestClient
from django_bolt.testing import client as testing_client


class DjangoTestClientDoesNotFindBoltRoutes(TestCase):
    """Claim 1: `self.client` looks in ROOT_URLCONF, thus a Bolt route gives 404."""

    def test_bolt_route_is_not_in_root_urlconf(self):
        response = self.client.get("/blogs/")
        assert response.status_code == 404


class TestClientWorksUnderPlainTestCase(TestCase):
    """Claim 2: TestClient lends its connection, thus `TestCase` needs no marker."""

    def test_handler_reads_the_uncommitted_row(self):
        blog = Blog.objects.create(name="uncommitted", description="d", status="published")

        with TestClient(api) as client:
            response = client.get("/blogs/")

        assert response.status_code == 200
        assert [row["name"] for row in response.json()] == [blog.name]


class SharingCanBeSwitchedOff(TestCase):
    """Claim 2b: with `share_db_connection=False` the old condition returns."""

    def test_advisory_and_failure_without_sharing(self):
        # The advisory fires one time in each process, thus this proof needs a
        # fresh latch when it runs after another test that already caused it.
        testing_client._atomic_block_warning_emitted.clear()

        Blog.objects.create(name="invisible", description="d", status="published")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with TestClient(api, share_db_connection=False) as client:
                response = client.get("/blogs/")

        advisories = [w for w in recorded if issubclass(w.category, BoltTestClientWarning)]
        assert len(advisories) == 1, [str(w.message) for w in recorded]
        assert "TransactionTestCase" in str(advisories[0].message)

        # On SQLite the write lock of the test stops the query of the handler, thus
        # the unclear 500 the docs describe. Other backends give an empty result,
        # because the row of the test is not committed.
        if connection.vendor == "sqlite":
            assert response.status_code == 500
        else:
            assert response.status_code == 200
            assert response.json() == []


class TestClientUnderTransactionTestCaseWorks(TransactionTestCase):
    """Claim 3: the committing pattern keeps working, and shares nothing."""

    def test_handler_reads_the_committed_row(self):
        blog = Blog.objects.create(name="visible", description="d", status="published")

        with TestClient(api) as client:
            response = client.get("/blogs/")

        assert response.status_code == 200
        assert [row["name"] for row in response.json()] == [blog.name]
