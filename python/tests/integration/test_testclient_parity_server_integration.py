"""runbolt answers each parity case as TestClient does.

``test_testclient_production_parity.py`` sends the same cases through
TestClient. If runbolt changes how it reads a setting, this file fails.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .parity_cases import CASES, ParityCase


@pytest.mark.server_integration
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_runbolt_answers_as_testclient(case: ParityCase, make_server_project, tmp_path):
    settings = case.prepare(tmp_path)
    settings_extra = "\n".join(f"{name} = {value!r}" for name, value in settings.items())
    project = make_server_project(api_module=app_module("testclient_parity"), settings_extra=settings_extra)
    with project.start() as server:
        case.check(server.request(case.method, case.path, headers=case.headers, content=case.body))
