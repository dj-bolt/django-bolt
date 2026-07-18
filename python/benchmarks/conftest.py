"""Pytest configuration for micro-benchmarks.

Configures a minimal Django settings module so django_bolt internals
(serialization, injector compilation) import cleanly outside a project.

Run with: just bench-micro
  (uv run --with pytest --with pytest-benchmark pytest python/benchmarks)
"""

from __future__ import annotations

import os


def pytest_configure(config):
    import django  # noqa: PLC0415
    from django.conf import settings  # noqa: PLC0415

    if os.getenv("DJANGO_SETTINGS_MODULE"):
        return

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            SECRET_KEY="bench-secret-key",
            ALLOWED_HOSTS=["*"],
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
            ],
            DATABASES={},
        )
        django.setup()
