"""
Tests for the status_codes module.
"""

from django_bolt import status_codes


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
