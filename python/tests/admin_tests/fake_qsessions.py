"""Stand-in for django-qsessions used by admin detection tests.

django-qsessions replaces ``django.contrib.sessions`` in ``INSTALLED_APPS`` with
its own ``qsessions`` app, and swaps ``SessionMiddleware`` for a subclass in
``MIDDLEWARE``. This module mirrors that middleware subclassing so tests can
exercise admin detection without installing the real package.
"""

from __future__ import annotations

from django.contrib.sessions.middleware import SessionMiddleware as DjangoSessionMiddleware


class SessionMiddleware(DjangoSessionMiddleware):
    """A SessionMiddleware subclass, like ``qsessions.middleware.SessionMiddleware``."""
