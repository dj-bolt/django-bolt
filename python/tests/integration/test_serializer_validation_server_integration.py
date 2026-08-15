"""Serializer validator 422 shape, in-process and over a real runbolt process (issue #280)."""

from __future__ import annotations

import pytest

from django_bolt.testing import TestClient

from .apps import app_module, serializer_validation

BAD = {"collage_code": "bad", "phone_number": "bad"}
GOOD = {"collage_code": "ok", "phone_number": "ok"}
COLLAGE_ERR = {"loc": ["body", "collage_code"], "msg": "collage Code is Not Valid", "type": "value_error"}
PHONE_ERR = {"loc": ["body", "phone_number"], "msg": "The phone number entered is not valid.", "type": "value_error"}


def _under(prefix, *errs):
    return [{**e, "loc": ["body", *prefix, *e["loc"][1:]]} for e in errs]


def _exercise(post, put, patch):
    """Run every variant through the given request callables; return {name: (status, detail)}."""
    responses = {
        "create": post("/register", BAD),
        "create_ok": post("/register", GOOD),
        "update": put("/register/7", BAD),
        "update_ok": put("/register/7", GOOD),
        "patch_one_bad": patch("/register/7", {"phone_number": "bad"}),
        "patch_ok": patch("/register/7", {"collage_code": "ok"}),
        "bulk": post("/register/bulk", [GOOD, BAD]),
        "bulk_ok": post("/register/bulk", [GOOD, GOOD]),
        "nested": post("/profile", {"register": BAD}),
        "nested_type_error": post("/profile", {"register": {"collage_code": 1, "phone_number": "ok"}}),
    }
    return {k: (r.status_code, r.json().get("detail")) for k, r in responses.items()}


def _assert_shape(results):
    assert results["create"] == (422, [COLLAGE_ERR, PHONE_ERR])
    assert results["create_ok"] == (200, None)
    assert results["update"] == (422, [COLLAGE_ERR, PHONE_ERR])
    assert results["update_ok"] == (200, None)
    assert results["patch_one_bad"] == (422, [PHONE_ERR])
    assert results["patch_ok"] == (200, None)
    assert results["bulk"] == (422, _under(["1"], COLLAGE_ERR, PHONE_ERR))
    assert results["bulk_ok"] == (200, None)
    assert results["nested"] == (422, _under(["register"], COLLAGE_ERR, PHONE_ERR))
    assert results["nested_type_error"] == (
        422,
        [{"loc": ["body", "register", "collage_code"], "msg": "Expected `str`, got `int`", "type": "validation_error"}],
    )


def test_validator_errors_in_process():
    with TestClient(serializer_validation.api) as client:
        results = _exercise(
            lambda p, b: client.post(p, json=b),
            lambda p, b: client.put(p, json=b),
            lambda p, b: client.patch(p, json=b),
        )
    _assert_shape(results)


@pytest.mark.server_integration
def test_validator_errors_over_real_server(make_server_project):
    project = make_server_project(api_module=app_module("serializer_validation"))

    with project.start() as server:
        c = server.client
        results = _exercise(
            lambda p, b: c.post(server.url(p), json=b),
            lambda p, b: c.put(server.url(p), json=b),
            lambda p, b: c.patch(server.url(p), json=b),
        )
    _assert_shape(results)
