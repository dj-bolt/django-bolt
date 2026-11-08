"""
Tests for the status_codes module, and for the body of a 204 response.
"""

import asyncio

import pytest

from django_bolt import BoltAPI, status, status_codes
from django_bolt.middleware import middleware
from django_bolt.testing import TestClient


def test_status_alias_import():
    """Test that the status alias re-export works."""
    assert status.HTTP_200_OK == 200
    assert status.is_success(200)


def test_status_classification_helpers():
    """Test that the status_code module provides classification helpers for status codes."""
    assert status_codes.is_informational(101)
    assert status_codes.is_success(200)
    assert status_codes.is_redirect(307)
    assert status_codes.is_client_error(422)
    assert status_codes.is_server_error(503)
    assert status_codes.is_error(404)
    assert status_codes.is_error(500)
    assert not status_codes.is_error(201)
    assert not status_codes.is_informational(200)
    assert not status_codes.is_success(100)


def test_status_classification_boundaries():
    """Test boundary values for classification helpers."""
    # informational: 100-199
    assert not status_codes.is_informational(99)
    assert status_codes.is_informational(100)
    assert status_codes.is_informational(199)
    assert not status_codes.is_informational(200)

    # success: 200-299
    assert not status_codes.is_success(199)
    assert status_codes.is_success(200)
    assert status_codes.is_success(299)
    assert not status_codes.is_success(300)

    # redirect: 300-399
    assert not status_codes.is_redirect(299)
    assert status_codes.is_redirect(300)
    assert status_codes.is_redirect(399)
    assert not status_codes.is_redirect(400)

    # client error: 400-499
    assert not status_codes.is_client_error(399)
    assert status_codes.is_client_error(400)
    assert status_codes.is_client_error(499)
    assert not status_codes.is_client_error(500)

    # server error: 500-599
    assert not status_codes.is_server_error(499)
    assert status_codes.is_server_error(500)
    assert status_codes.is_server_error(599)
    assert not status_codes.is_server_error(600)

    # is_error: 400-599
    assert not status_codes.is_error(399)
    assert status_codes.is_error(400)
    assert status_codes.is_error(599)
    assert not status_codes.is_error(600)


@middleware
async def body_length(request, call_next):
    """Send the length of the handler body. Rust drops a 204 body, so only a middleware sees it."""
    response = await call_next(request)
    response.headers["x-body-length"] = str(len(response.body))
    return response


@pytest.mark.parametrize("path", ["/sync", "/async"])
def test_204_with_none_has_an_empty_body(path):
    """A 204 handler that returns None gives an empty body, also to a middleware."""
    api = BoltAPI()

    @api.delete("/sync", status_code=204)
    @body_length
    def sync_delete():
        return None

    @api.delete("/async", status_code=204)
    @body_length
    async def async_delete():
        await asyncio.sleep(0)
        return None

    with TestClient(api) as client:
        response = client.delete(path)

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["x-body-length"] == "0"
