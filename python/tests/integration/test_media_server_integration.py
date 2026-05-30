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

    `handle_file` is shared between static and media; in debug it falls back
    to `find_with_django_finders` only for the static scope, which knows about
    STATICFILES_DIRS. A file that exists only in STATICFILES_DIRS should not be
    reachable under /media/.
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
    """BOLT_MEDIA_MAX_AGE = N sends `Cache-Control: private, max-age=N` on /media/.

    Media must be `private`, not `public`: MEDIA_ROOT typically holds
    per-user uploads where the URL is the only access control. `public`
    would let CDNs / proxies cache one user's media and hand it to another.
    """
    project = _media_project(
        make_server_project,
        extra_settings="BOLT_MEDIA_MAX_AGE = 3600",
    )

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "private, max-age=3600", (
        "Media cache visibility must be `private` to keep shared caches from "
        f"redistributing per-user uploads. Got: {response.headers.get('cache-control')!r}"
    )


def test_media_no_cache_control_when_setting_absent(make_server_project):
    """Without BOLT_MEDIA_MAX_AGE, no Cache-Control header is sent (current default)."""
    project = _media_project(make_server_project)

    with project.start() as server:
        response = server.get("/media/upload.txt")

    assert response.status_code == 200
    assert "cache-control" not in {k.lower() for k in response.headers}


def test_media_cache_control_not_applied_to_404(make_server_project):
    """A 404 must NOT carry Cache-Control even when BOLT_MEDIA_MAX_AGE is set.

    Caching a miss with a long max-age makes a not-yet-uploaded file appear
    permanently missing to the client/CDN for the cache lifetime — even after
    the upload lands. nginx's `expires` only fires on 200; we match that:
    Cache-Control is gated on a success status, while nosniff (a security
    header) stays on every response.
    """
    project = _media_project(
        make_server_project,
        extra_settings="BOLT_MEDIA_MAX_AGE = 31536000",
    )

    with project.start() as server:
        present = server.get("/media/upload.txt")
        missing = server.get("/media/does-not-exist.txt")

    # Sanity: the setting IS active on a real hit.
    assert present.status_code == 200
    assert present.headers.get("cache-control") == "private, max-age=31536000"

    # The fix: the 404 must not be cacheable via our max-age header.
    assert missing.status_code == 404
    assert "cache-control" not in {k.lower() for k in missing.headers}, (
        "404 responses must not carry Cache-Control — a cached miss hides the "
        f"file after it's uploaded. Got: {missing.headers.get('cache-control')!r}"
    )
    # Security header still present on the error path.
    assert missing.headers.get("x-content-type-options") == "nosniff"


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


# --- Security: user-uploaded scripting content is neutralized ---
#
# The threat: an attacker uploads `evil.html`, `evil.svg`, or `evil.js` and
# lures a logged-in victim to `/media/<uid>/evil.<ext>`. With the default
# actix-files Content-Type (text/html, image/svg+xml, application/javascript)
# and Content-Disposition: inline, the browser executes the script in the
# site's origin. X-Content-Type-Options: nosniff doesn't help here — it only
# prevents the browser from *overriding* the declared type, not from honoring
# a declared `text/html`. The handler must rewrite Content-Type to
# `application/octet-stream` and force `Content-Disposition: attachment` for
# any extension that can carry executable script.
@pytest.mark.parametrize(
    ("filename", "body"),
    [
        ("evil.html", b"<script>alert(1)</script>"),
        ("evil.htm", b"<script>alert(1)</script>"),
        ("evil.xhtml", b"<html><script>alert(1)</script></html>"),
        ("evil.shtml", b"<!--#exec cmd=\"x\" --><script>alert(1)</script>"),
        ("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ("evil.svgz", b"\x1f\x8bsvg-bytes-here"),
        ("evil.xml", b"<?xml version='1.0'?><root/>"),
        ("evil.xsl", b"<?xml version='1.0'?><xsl:stylesheet/>"),
        ("evil.xslt", b"<?xml version='1.0'?><xsl:stylesheet/>"),
        ("evil.js", b"alert(1)"),
        ("evil.mjs", b"export default 1;"),
        ("evil.wasm", b"\x00asm\x01\x00\x00\x00"),
    ],
)
def test_media_dangerous_uploads_force_attachment_and_octet_stream(
    make_server_project, filename, body
):
    """User-uploaded scriptable types must be downloaded, not rendered.

    The bug this guards against: stored XSS via a user uploading
    `<script>alert(document.cookie)</script>` as `.html`/`.svg`/`.js` and the
    browser executing it inline at `/media/...` (i.e. in the app's origin).
    """
    project = _media_project(
        make_server_project,
        extra_files={f"mediafiles/{filename}": body},
    )

    with project.start() as server:
        response = server.get(f"/media/{filename}")

    assert response.status_code == 200
    # MUST be the inert type — the browser will not interpret octet-stream.
    assert response.headers.get("content-type") == "application/octet-stream", (
        f"{filename}: content-type leaked as "
        f"{response.headers.get('content-type')!r} — browser may execute as script"
    )
    # MUST force download. `inline` would still execute renderable types.
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("attachment"), (
        f"{filename}: Content-Disposition must be `attachment`, got {disposition!r}"
    )
    # Body must still be the original bytes; we're rewriting headers, not content.
    assert response.content == body, (
        f"{filename}: body should be served unchanged (this is a header-level fix)"
    )


@pytest.mark.parametrize(
    ("filename", "body", "content_type_prefix"),
    [
        ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png"),
        ("avatar.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32, "image/jpeg"),
        ("doc.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"),
        ("readme.txt", b"hello\n", "text/plain"),
        ("data.json", b'{"ok":true}\n', "application/json"),
    ],
)
def test_media_safe_types_keep_their_content_type(
    make_server_project, filename, body, content_type_prefix
):
    """Regression guard for the XSS fix.

    The dangerous-extension rewrite must NOT break legitimate inline serving
    of images and other non-scriptable types. Otherwise the obvious "force
    attachment on everything" fix would silently break user avatars and
    post images.

    We check the *Content-Type* — the disarm sets it to
    `application/octet-stream`, so preserving the native type proves the
    disarm wasn't applied. We can't usefully assert on Content-Disposition
    here because actix-files chooses inline vs. attachment per type (PDFs
    default to attachment with a filename, independent of our security fix).
    """
    project = _media_project(
        make_server_project,
        extra_files={f"mediafiles/{filename}": body},
    )

    with project.start() as server:
        response = server.get(f"/media/{filename}")

    assert response.status_code == 200
    ct = response.headers.get("content-type", "")
    assert ct.startswith(content_type_prefix), (
        f"{filename}: expected Content-Type starting with {content_type_prefix!r}, got {ct!r}"
    )
    assert ct != "application/octet-stream", (
        f"{filename}: Content-Type was nuked to octet-stream — XSS disarm fired on a safe type"
    )
    # nosniff still applied — that's defense-in-depth, not the disarm itself.
    assert response.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize(
    "dotfile_path",
    [
        ".env",
        ".git/config",
        ".htaccess",
        "subdir/.ssh/id_rsa",
        "ok/.hidden",
    ],
)
def test_media_dotfile_paths_return_404(make_server_project, dotfile_path):
    """Hidden / dotfile paths under MEDIA_ROOT must not be served.

    Even though MEDIA_ROOT is meant for user uploads, stray secrets do end up
    there (`.env`, leftover `.git/`, editor backups). nginx and Apache deny
    dotfiles by default for this reason — django-bolt should too.
    """
    project = _media_project(
        make_server_project,
        extra_files={f"mediafiles/{dotfile_path}": "SECRET=do-not-leak\n"},
    )

    with project.start() as server:
        response = server.get(f"/media/{dotfile_path}")

    assert response.status_code == 404, (
        f"/media/{dotfile_path} must not be served, got {response.status_code}"
    )
    assert b"SECRET=do-not-leak" not in response.content


def test_media_url_with_scope_param_chars_is_ignored(make_server_project):
    """MEDIA_URL containing `{` or `:` must not silently become an actix scope param.

    `web::scope("/media/{tenant}")` would compile silently into a parameterized
    scope: the `{tenant}` segment would match anything, and routes mounted
    inside would not see the literal `{tenant}` value as the user intended.
    The Rust side warns and skips serving instead.
    """
    project = make_server_project(
        settings_extra="""
        MEDIA_URL = "/media/{tenant}/"
        MEDIA_ROOT = str(BASE_DIR / "mediafiles")
        """,
        extra_files={"mediafiles/upload.txt": "ok\n"},
    )

    with project.start() as server:
        # The misconfigured prefix must not serve files. Try the literal prefix
        # AND a couple of guesses for how actix might interpret the param.
        literal = server.get("/media/{tenant}/upload.txt")
        guessed = server.get("/media/anything/upload.txt")
        plain = server.get("/media/upload.txt")

    # Whatever actix does with the malformed scope, none of these must succeed.
    for label, response in [("literal", literal), ("guessed", guessed), ("plain", plain)]:
        assert response.status_code == 404, (
            f"{label} request must 404 when MEDIA_URL is malformed, got {response.status_code}"
        )
        assert b"ok" not in response.content
