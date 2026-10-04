"""TestClient reads Django settings as `runbolt` reads them.

Each case in ``tests.integration.parity_cases`` sets a setting, sends a
request, and states the answer. This file checks the answers of TestClient.
``test_testclient_parity_server_integration.py`` checks the same answers
from a real `runbolt` server.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient
from tests.integration.apps.testclient_parity import api
from tests.integration.parity_cases import CASES, ParityCase


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_testclient_answers_as_runbolt(case: ParityCase, tmp_path):
    settings = case.prepare(tmp_path)
    with override_settings(**settings), TestClient(api) as client:
        case.check(client.request(case.method, case.path, headers=case.headers, content=case.body))


def test_a_static_prefix_of_slash_does_not_shadow_the_routes(tmp_path):
    """runbolt refuses STATIC_URL "/": it would serve every path as a file."""
    static_files = {"url_prefix": "/", "directories": [str(tmp_path)]}
    with TestClient(api, static_files_config=static_files) as client:
        assert client.get("/plain").json() == {"ok": True}


def _limited_api() -> BoltAPI:
    limited_api = BoltAPI()

    @limited_api.get("/limited")
    @rate_limit(rps=1, burst=1)
    async def limited():
        return {"ok": True}

    return limited_api


def test_two_apis_never_share_a_rate_limit_bucket():
    """Rate-limit buckets belong to the BoltAPI, as they belong to the server.

    Two APIs have the same handler ids and the same client address here. The
    second API starts with an empty bucket. Clients of one API share its buckets.
    """
    first = _limited_api()
    with TestClient(first) as client:
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 429

    with TestClient(_limited_api()) as client:
        assert client.get("/limited").status_code == 200

    with TestClient(first) as client:
        assert client.get("/limited").status_code == 429
