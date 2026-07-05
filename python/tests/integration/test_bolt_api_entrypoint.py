from __future__ import annotations

import pytest
from django.core.management import CommandError
from django.test import override_settings

from django_bolt.management.commands.runbolt import Command


def test_bolt_api_entrypoint_missing_module_raises_command_error():
    with (
        override_settings(BOLT_API=["tests.integration.apps.does_not_exist:api"]),
        pytest.raises(CommandError, match="does_not_exist"),
    ):
        Command().autodiscover_apis()
