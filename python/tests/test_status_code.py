"""
Tests for the status_codes module.
"""

from django_bolt import status
from django_bolt import status_codes


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
