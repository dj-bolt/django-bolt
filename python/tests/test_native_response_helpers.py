"""Native helpers preserve datetime, filesystem, and Django cookie behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie

import pytest

from django_bolt import _core
from django_bolt.exceptions import ResponseFileNotAllowed, ResponseFileNotFound


@pytest.mark.parametrize("tz", [None, UTC, timezone(timedelta(hours=5))])
def test_native_cookie_date_preserves_wall_time(tz):
    value = datetime(2030, 6, 9, 10, 11, 12, tzinfo=tz)
    assert _core.format_cookie_date(value) == "Sun, 09 Jun 2030 10:11:12 GMT"


def test_native_file_resolution(tmp_path):
    target = tmp_path / "payload"
    target.write_text("content")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert _core.resolve_response_file(link, [tmp_path]) == str(target)
    with pytest.raises(ResponseFileNotFound):
        _core.resolve_response_file(tmp_path / "missing", None)
    # A path that names no regular file is a 404, as a missing file is.
    with pytest.raises(ResponseFileNotFound):
        _core.resolve_response_file(tmp_path, None)
    with pytest.raises(ResponseFileNotAllowed):
        _core.resolve_response_file(target, [tmp_path / "other"])


def test_native_file_resolution_checks_symlink_targets(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    link = allowed / "link"
    link.symlink_to(outside)
    with pytest.raises(ResponseFileNotAllowed):
        _core.resolve_response_file(link, [allowed])
    cycle = tmp_path / "cycle"
    cycle.symlink_to(cycle)
    with pytest.raises(ResponseFileNotFound):
        _core.resolve_response_file(cycle, None)
    with pytest.raises(ResponseFileNotFound):
        _core.resolve_response_file("bad\0path", None)


def test_native_cookie_population_preserves_mutable_morsels():
    cookies = SimpleCookie()
    rows = [("session", 'a;b"c', "/", 42, "expiry", "example.org", True, True, "Lax")]
    _core.populate_response_cookies(cookies, rows)
    morsel = cookies["session"]
    assert morsel.value == 'a;b"c'
    assert morsel.coded_value == '"a\\073b\\"c"'
    assert morsel["max-age"] == 42
    assert morsel["expires"] == "expiry"
    assert morsel["domain"] == "example.org"
    assert morsel["secure"] is True
    morsel["path"] = "/changed"
    assert "Path=/changed" in cookies.output()


@pytest.mark.parametrize("index", [0, 1, 2, 4, 5, 8])
def test_native_cookie_population_rejects_controls(index):
    row = ["session", "value", "/", None, None, None, False, False, "Lax"]
    row[index] = "bad\r\nvalue"
    with pytest.raises(CookieError):
        _core.populate_response_cookies(SimpleCookie(), [tuple(row)])


def test_native_cookie_population_keeps_duplicate_attribute_semantics():
    cookies = SimpleCookie()
    _core.populate_response_cookies(
        cookies,
        [
            ("a", "first", "/", None, None, None, True, False, "Lax"),
            ("a", "second", "", None, None, None, False, False, None),
        ],
    )
    assert cookies["a"].value == "second"
    assert cookies["a"]["secure"] is True
    assert cookies["a"]["path"] == "/"
