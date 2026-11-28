"""The ``ENGINE`` of ``test_async_orm_on_loop_postgres.py`` for a database with a custom backend."""

from __future__ import annotations

from django.db.backends.postgresql import base


class DatabaseWrapper(base.DatabaseWrapper):
    """A project can change how this class opens a connection or runs a query."""
