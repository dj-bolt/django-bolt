from __future__ import annotations

import os
import textwrap

import pytest

pytestmark = pytest.mark.server_integration


def test_media_files_served_from_media_root(make_server_project):
    """Files under MEDIA_ROOT are reachable at MEDIA_URL through the Rust pipeline."""
    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "mediafiles/upload.txt": "user uploaded content\n",
        },
    )

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")
    assert response.text == "user uploaded content\n"


def test_static_and_media_coexist(make_server_project):
    """Both static and media routes serve their own files when configured together.

    Regression for the `app_data` collision: two `web::Data<Vec<String>>` instances
    registered at app scope would type-key-collide; the scope refactor isolates them.
    Uses STATIC_ROOT (not STATICFILES_DIRS) and omits django.contrib.staticfiles so
    the Django-finders fallback can't mask a collision by resolving the file anyway.
    """
    project = make_server_project(
        settings_extra="""
        STATIC_URL = "/static/"
        STATIC_ROOT = str(BASE_DIR / "staticroot")
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "staticroot/style.css": "body { color: red; }\n",
            "mediafiles/upload.txt": "media content\n",
        },
    )

    with project.start() as server:
        static_resp = server.get("/static/style.css")
        media_resp = server.get("/media/upload.txt")

    assert static_resp.status_code == 200, f"static failed: {static_resp.status_code}"
    assert "color: red" in static_resp.text
    assert media_resp.status_code == 200, f"media failed: {media_resp.status_code}"
    assert media_resp.text == "media content\n"


def test_static_prefix_requires_path_boundary(make_server_project):
    """`/statictest.css` must not match the static route — only `/static/...` should.

    Regression for the old `format!("{}{{path:.*}}", "/static")` registration that
    matched any path starting with the literal prefix, ignoring `/` boundaries.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        """,
        extra_files={
            "staticassets/test.css": "ok\n",
        },
    )

    with project.start() as server:
        ok = server.get("/static/test.css")
        boundary = server.get("/statictest.css")

    assert ok.status_code == 200
    assert boundary.status_code == 404, (
        f"/statictest.css should NOT match /static scope, got {boundary.status_code}"
    )


def test_media_does_not_fall_through_to_static_finders(make_server_project):
    """A `/media/...` miss must not be resolved via Django staticfiles finders.

    `handle_static_file` is shared between static and media; in debug it falls back
    to `find_with_django_finders`, which only knows about static. A file that
    exists only in STATICFILES_DIRS should not be reachable under /media/.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "staticassets/bait.css": "static-only file\n",
            "mediafiles/.keep": "",
        },
    )

    with project.start() as server:
        static_hit = server.get("/static/bait.css")
        media_leak = server.get("/media/bait.css")

    # Sanity: the file IS reachable through static (so the finder works).
    assert static_hit.status_code == 200
    # The bug: file leaks through /media/ via finder fallback.
    assert media_leak.status_code == 404, (
        f"/media/bait.css should NOT resolve via staticfiles finders, "
        f"got {media_leak.status_code}"
    )


def test_media_response_sets_caching_and_nosniff_headers(make_server_project):
    """Media responses expose conditional-request headers and forbid MIME sniffing."""
    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "mediafiles/photo.bin": b"\x00" * 4096,
        },
    )

    with project.start() as server:
        response = server.get("/media/photo.bin")
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        conditional = server.get("/media/photo.bin", headers={"If-None-Match": etag or ""})

    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert etag, "ETag must be set so clients can do conditional GETs"
    assert last_modified, "Last-Modified must be set"
    assert conditional.status_code == 304, (
        f"If-None-Match with the current ETag must return 304, got {conditional.status_code}"
    )


def test_media_range_request_returns_partial_content(make_server_project):
    """Range requests must serve a slice of the file, not the whole thing."""
    body = b"abcdefghijklmnopqrstuvwxyz" * 64  # 1664 bytes
    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "mediafiles/clip.bin": body,
        },
    )

    with project.start() as server:
        response = server.get("/media/clip.bin", headers={"Range": "bytes=0-15"})

    assert response.status_code == 206
    assert response.content == body[:16]
    assert response.headers.get("content-range", "").startswith("bytes 0-15/")


@pytest.mark.parametrize(
    "attack_path",
    [
        "/media/../etc/passwd",
        "/media/sub/../../etc/passwd",
        "/media/..%2fetc/passwd",
        "/media/%2e%2e/etc/passwd",
    ],
)
def test_media_traversal_blocked(make_server_project, attack_path):
    """Directory-traversal attempts must never escape MEDIA_ROOT."""
    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "mediafiles/keep.txt": "ok\n",
        },
    )

    with project.start() as server:
        response = server.get(attack_path)

    assert response.status_code in (400, 404), (
        f"{attack_path} must not succeed, got {response.status_code}"
    )


def test_media_symlink_outside_root_blocked(make_server_project, tmp_path):
    """A symlink inside MEDIA_ROOT pointing outside it must not be served."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_CONTENT_DO_NOT_LEAK\n")

    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={
            "mediafiles/.keep": "",
        },
    )

    media_root = project.path("mediafiles")
    media_root.mkdir(exist_ok=True)
    symlink = media_root / "escape.txt"
    try:
        os.symlink(secret, symlink)
    except OSError:
        pytest.skip("symlink creation not supported on this platform")

    with project.start() as server:
        response = server.get("/media/escape.txt")

    assert response.status_code == 404, (
        f"Symlink escaping MEDIA_ROOT must 404, got {response.status_code}"
    )
    assert b"SECRET_CONTENT_DO_NOT_LEAK" not in response.content


def _media_project(make_server_project, extra_settings: str = "", **kwargs):
    parts = [
        'MEDIA_URL = "/media/"',
        'MEDIA_ROOT = str(BASE_DIR / "mediafiles")',
    ]
    extra = textwrap.dedent(extra_settings).strip()
    if extra:
        parts.append(extra)
    files = {"mediafiles/upload.txt": "ok\n"}
    files.update(kwargs.pop("extra_files", {}))
    return make_server_project(
        settings_extra="\n".join(parts),
        extra_files=files,
        **kwargs,
    )


def test_media_head_returns_headers_without_body(make_server_project):
    """HEAD must mirror GET's headers (incl. nosniff) but never return a body."""
    project = _media_project(make_server_project)

    with project.start() as server:
        head = server.request("HEAD", "/media/upload.txt")
        get = server.get("/media/upload.txt")

    assert head.status_code == 200
    assert head.content == b"", "HEAD response must have an empty body"
    assert head.headers.get("x-content-type-options") == "nosniff"
    assert head.headers.get("content-length") == get.headers.get("content-length")
    assert head.headers.get("etag") == get.headers.get("etag")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_media_rejects_write_methods(make_server_project, method):
    """Media route is read-only; write methods must return 405.

    Asserting exactly 405 (not 404) so a regression that fails to register the
    route at all — which would 404 everything — can't silently pass this test.
    """
    project = _media_project(make_server_project)

    with project.start() as server:
        response = server.request(method, "/media/upload.txt")

    assert response.status_code == 405, (
        f"{method} /media/upload.txt must return 405 Method Not Allowed, got {response.status_code}"
    )


def test_media_directory_request_does_not_list_or_500(make_server_project):
    """GET on a directory inside MEDIA_ROOT must 404, not list contents or 500."""
    project = _media_project(
        make_server_project,
        extra_files={
            "mediafiles/photos/a.txt": "a\n",
            "mediafiles/photos/b.txt": "b\n",
        },
    )

    with project.start() as server:
        # follow_redirects=True (helpers.py), so a 301 → trailing-slash redirect
        # would be transparently chased; we care about the final response.
        response = server.get("/media/photos")
        trailing = server.get("/media/photos/")

    assert response.status_code == 404, (
        f"/media/photos must 404, got {response.status_code}"
    )
    assert trailing.status_code == 404, (
        f"/media/photos/ must 404 (not list), got {trailing.status_code}"
    )
    for body in (response.content, trailing.content):
        assert b"a.txt" not in body and b"b.txt" not in body, (
            "directory listing must not appear in the response body"
        )


def test_media_root_path_returns_404(make_server_project):
    """GET /media/ (no file) must not list MEDIA_ROOT."""
    project = _media_project(make_server_project)

    with project.start() as server:
        response = server.get("/media/")

    assert response.status_code in (301, 404)
    assert b"upload.txt" not in response.content


@pytest.mark.parametrize(
    "attack_path",
    [
        "/media/%252e%252e/etc/passwd",      # double-encoded ../
        "/media/..\\etc\\passwd",            # backslash traversal
        "/media/sub\\..\\..\\etc\\passwd",   # nested backslash traversal
        "/media//etc/passwd",                # leading-slash absolute path
    ],
)
def test_media_exotic_traversal_blocked(make_server_project, attack_path):
    """Encoded and platform-specific traversal variants must not escape MEDIA_ROOT."""
    project = _media_project(make_server_project)

    with project.start() as server:
        response = server.get(attack_path)

    assert response.status_code in (400, 404), (
        f"{attack_path} must not succeed, got {response.status_code}"
    )
    # Whatever it returns, it must not contain /etc/passwd content
    assert b"root:" not in response.content


def test_media_cache_control_header_from_max_age_setting(make_server_project):
    """BOLT_MEDIA_MAX_AGE = N sends `Cache-Control: public, max-age=N` on /media/."""
    project = _media_project(
        make_server_project,
        extra_settings="BOLT_MEDIA_MAX_AGE = 3600",
    )

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "public, max-age=3600"


def test_media_no_cache_control_when_setting_absent(make_server_project):
    """Without BOLT_MEDIA_MAX_AGE, no Cache-Control header is sent (current default)."""
    project = _media_project(make_server_project)

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert "cache-control" not in {k.lower() for k in response.headers}


@pytest.mark.parametrize("bad_value", ["-1", '"oops"', "True", "False"])
def test_media_invalid_max_age_falls_back_silently(make_server_project, bad_value):
    """Negative / non-int / bool BOLT_MEDIA_MAX_AGE values must not crash startup or emit a header.

    Booleans are a Python subclass of int, so a naive `extract::<i64>` would
    silently turn `True` into `max-age=1`. The Rust side rejects them explicitly.
    """
    project = _media_project(
        make_server_project,
        extra_settings=f"BOLT_MEDIA_MAX_AGE = {bad_value}",
    )

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert "cache-control" not in {k.lower() for k in response.headers}


def test_media_serves_csp_header_when_configured(make_server_project):
    """When SECURE_CSP is set, media responses carry the Content-Security-Policy header.

    CSP on media is a defense-in-depth against XSS via user-uploaded HTML/SVG —
    even if a browser ignores nosniff, the CSP restricts what scripts can run.
    """
    project = _media_project(
        make_server_project,
        extra_settings="""
        SECURE_CSP = {
            "default-src": ["'self'"],
            "script-src": ["'self'"],
        }
        """,
    )

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp, f"CSP not propagated to /media/, got {csp!r}"
    # nosniff must still be present alongside CSP
    assert response.headers.get("x-content-type-options") == "nosniff"
