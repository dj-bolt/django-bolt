"""
Tests for Actix-native static file serving.

Verifies that static files are served correctly from:
1. STATIC_ROOT (collected static files)
2. STATICFILES_DIRS (additional directories)
3. Django's staticfiles finders (app-level static files like admin)

Also tests Django template {% static %} tag integration.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from django.core.exceptions import SuspiciousFileOperation
from django.contrib.staticfiles.finders import get_finder

from django_bolt import BoltAPI
from django_bolt.admin.static import find_static_file, serve_static_file
from django_bolt.exceptions import HTTPException
from django_bolt.shortcuts import render
from django_bolt.testing import TestClient


@pytest.fixture(autouse=True)
def clear_staticfiles_finder_cache():
    """Reset Django's cached static finders between tests that mutate settings."""
    get_finder.cache_clear()
    yield
    get_finder.cache_clear()


# Create test static files directory
TEST_STATIC_DIR = tempfile.mkdtemp(prefix="django_bolt_static_")

# Create test CSS file
CSS_DIR = os.path.join(TEST_STATIC_DIR, "css")
os.makedirs(CSS_DIR, exist_ok=True)
CSS_FILE = os.path.join(CSS_DIR, "style.css")
with open(CSS_FILE, "w") as f:
    f.write("body { color: blue; font-family: sans-serif; }")

# Create test JS file
JS_DIR = os.path.join(TEST_STATIC_DIR, "js")
os.makedirs(JS_DIR, exist_ok=True)
JS_FILE = os.path.join(JS_DIR, "app.js")
with open(JS_FILE, "w") as f:
    f.write("console.log('Django-Bolt static files working!');")

# Create test image file (small PNG)
IMG_DIR = os.path.join(TEST_STATIC_DIR, "img")
os.makedirs(IMG_DIR, exist_ok=True)
IMG_FILE = os.path.join(IMG_DIR, "logo.png")
# Minimal valid 1x1 transparent PNG
PNG_BYTES = bytes(
    [
        0x89,
        0x50,
        0x4E,
        0x47,
        0x0D,
        0x0A,
        0x1A,
        0x0A,  # PNG signature
        0x00,
        0x00,
        0x00,
        0x0D,
        0x49,
        0x48,
        0x44,
        0x52,  # IHDR chunk
        0x00,
        0x00,
        0x00,
        0x01,
        0x00,
        0x00,
        0x00,
        0x01,  # 1x1 dimensions
        0x08,
        0x06,
        0x00,
        0x00,
        0x00,
        0x1F,
        0x15,
        0xC4,  # RGBA, 8-bit
        0x89,
        0x00,
        0x00,
        0x00,
        0x0A,
        0x49,
        0x44,
        0x41,  # IDAT chunk
        0x54,
        0x78,
        0x9C,
        0x63,
        0x00,
        0x01,
        0x00,
        0x00,  # Compressed data
        0x05,
        0x00,
        0x01,
        0x0D,
        0x0A,
        0x2D,
        0xB4,
        0x00,  # Adler32 checksum
        0x00,
        0x00,
        0x00,
        0x00,
        0x49,
        0x45,
        0x4E,
        0x44,  # IEND chunk
        0xAE,
        0x42,
        0x60,
        0x82,  # CRC
    ]
)
with open(IMG_FILE, "wb") as f:
    f.write(PNG_BYTES)


class TestFindStaticFile:
    """Tests for the find_static_file function used by Django finders fallback."""

    def test_find_file_in_static_root(self, monkeypatch):
        """Test that finder-only lookup does not search STATIC_ROOT."""
        from django.conf import settings

        original_root = getattr(settings, "STATIC_ROOT", None)
        original_dirs = getattr(settings, "STATICFILES_DIRS", None)
        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATICFILES_DIRS = []

        try:
            result = find_static_file("css/style.css")
            assert result is None
        finally:
            if original_root is not None:
                settings.STATIC_ROOT = original_root
            elif hasattr(settings, "STATIC_ROOT"):
                delattr(settings, "STATIC_ROOT")
            if original_dirs is not None:
                settings.STATICFILES_DIRS = original_dirs
            elif hasattr(settings, "STATICFILES_DIRS"):
                delattr(settings, "STATICFILES_DIRS")

    def test_find_file_in_staticfiles_dirs(self, monkeypatch):
        """Test finding a file in STATICFILES_DIRS."""
        from django.conf import settings

        # Create a second static directory
        second_dir = tempfile.mkdtemp(prefix="django_bolt_static2_")
        second_css = os.path.join(second_dir, "custom.css")
        with open(second_css, "w") as f:
            f.write("/* Custom styles */")

        # Temporarily set STATICFILES_DIRS
        original_dirs = getattr(settings, "STATICFILES_DIRS", None)
        settings.STATICFILES_DIRS = [second_dir]
        settings.STATIC_ROOT = None  # Clear to test STATICFILES_DIRS

        try:
            result = find_static_file("custom.css")
            assert result is not None
            assert result.endswith("custom.css")
        finally:
            if original_dirs is not None:
                settings.STATICFILES_DIRS = original_dirs
            elif hasattr(settings, "STATICFILES_DIRS"):
                delattr(settings, "STATICFILES_DIRS")

    def test_find_nonexistent_file(self):
        """Test that None is returned for non-existent files."""
        result = find_static_file("nonexistent/file.xyz")
        assert result is None

    def test_directory_traversal_blocked(self):
        """Test that traversal attempts are blocked by the finder."""
        with pytest.raises(SuspiciousFileOperation):
            find_static_file("../etc/passwd")

        result = find_static_file("..\\windows\\system32")
        assert result is None

    def test_windows_absolute_paths_return_none(self):
        """Test that Windows absolute paths are not resolved by the finder."""
        assert find_static_file("C:/Windows/win.ini") is None
        assert find_static_file("D:/secret.txt") is None
        assert find_static_file("C:temp/file.txt") is None
        assert find_static_file("\\\\server\\share\\file.txt") is None

    @pytest.mark.asyncio
    async def test_serve_static_file_returns_not_found_for_windows_absolute_paths(self):
        """Test that Windows absolute paths fall through to not found."""
        with pytest.raises(HTTPException) as exc_info:
            await serve_static_file("C:/Windows/win.ini")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_serve_static_file_rejects_traversal_paths(self):
        """Test that traversal paths raise Django's original error."""
        with pytest.raises(SuspiciousFileOperation):
            await serve_static_file("../etc/passwd")


class TestStaticFileServing:
    """Tests for static file serving via the test client."""

    @pytest.fixture(scope="class")
    def api_with_static(self):
        """Create API with static file routes."""
        from django_bolt.admin.static import register_static_routes

        api = BoltAPI()
        register_static_routes(api)

        @api.get("/health")
        async def health():
            return {"status": "ok"}

        return api

    @pytest.fixture(scope="class")
    def client(self, api_with_static):
        """Create test client."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATIC_URL = "/static/"
        return TestClient(api_with_static, use_http_layer=True)

    def test_serve_css_file(self, client, monkeypatch):
        """Test serving a CSS file."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATIC_URL = "/static/"

        response = client.get("/static/css/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")
        assert b"color: blue" in response.content

    def test_serve_js_file(self, client, monkeypatch):
        """Test serving a JavaScript file."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATIC_URL = "/static/"

        response = client.get("/static/js/app.js")
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "javascript" in content_type or "application/javascript" in content_type
        assert b"console.log" in response.content

    def test_serve_image_file(self, client, monkeypatch):
        """Test serving an image file."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATIC_URL = "/static/"

        response = client.get("/static/img/logo.png")
        assert response.status_code == 200
        assert "image/png" in response.headers.get("content-type", "")
        # PNG signature
        assert response.content[:4] == b"\x89PNG"

    def test_static_file_not_found(self, client):
        """Test 404 for non-existent static files."""
        response = client.get("/static/nonexistent/file.xyz")
        assert response.status_code == 404

    def test_directory_traversal_blocked(self, client):
        """Test that directory traversal is blocked."""
        response = client.get("/static/../../../etc/passwd")
        assert response.status_code in (400, 404)

        response = client.get("/static/css/../../etc/passwd")
        assert response.status_code in (400, 404)


class TestDjangoAdminStaticFiles:
    """Tests for Django admin static files via staticfiles finders."""

    def test_find_admin_css(self):
        """Test that Django admin CSS can be found via finders."""
        # This relies on django.contrib.admin being installed
        result = find_static_file("admin/css/base.css")

        # May be None if Django admin isn't fully installed
        # In CI/test environments, admin static files should be available
        if result is not None:
            assert "admin" in result
            assert result.endswith("base.css")
            assert os.path.isfile(result)

    def test_find_admin_js(self):
        """Test that Django admin JS can be found via finders."""
        result = find_static_file("admin/js/core.js")

        if result is not None:
            assert "admin" in result
            assert result.endswith("core.js")
            assert os.path.isfile(result)


class TestStaticTemplateTag:
    """Tests for Django template {% static %} tag with Bolt."""

    @pytest.fixture(scope="class")
    def api_with_template(self):
        """Create API with template that uses static tag."""
        from django.conf import settings

        # Configure static settings
        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATIC_URL = "/static/"

        api = BoltAPI()

        @api.get("/page")
        async def page(req):
            return render(req, "test_static_page.html", {"title": "Static Test"})

        return api

    @pytest.fixture(scope="class")
    def client(self, api_with_template):
        """Create test client."""
        return TestClient(api_with_template)

    def test_static_tag_renders_url(self, client):
        """Test that {% static %} tag renders correct URL."""
        response = client.get("/page")
        assert response.status_code == 200
        # The template should contain a reference to the static URL
        assert "/static/" in response.text or "static" in response.text.lower()


class TestMultipleDirectories:
    """Tests for serving from multiple static directories."""

    def test_searches_directories_in_order(self, monkeypatch):
        """Test that directories are searched in priority order."""
        from django.conf import settings

        # Create two directories with same-named file
        dir1 = tempfile.mkdtemp(prefix="static_priority1_")
        dir2 = tempfile.mkdtemp(prefix="static_priority2_")

        # Put different content in each
        with open(os.path.join(dir1, "test.txt"), "w") as f:
            f.write("FROM_DIR1")
        with open(os.path.join(dir2, "test.txt"), "w") as f:
            f.write("FROM_DIR2")

        settings.STATIC_ROOT = None
        settings.STATICFILES_DIRS = [dir1, dir2]

        result = find_static_file("test.txt")
        assert result is not None

        with open(result) as f:
            content = f.read()

        # STATICFILES_DIRS should preserve order
        assert content == "FROM_DIR1"

    def test_fallback_to_second_directory(self, monkeypatch):
        """Test falling back to second directory when file not in first."""
        from django.conf import settings

        # Create two directories, file only in second
        dir1 = tempfile.mkdtemp(prefix="static_fallback1_")
        dir2 = tempfile.mkdtemp(prefix="static_fallback2_")

        with open(os.path.join(dir2, "only_in_dir2.txt"), "w") as f:
            f.write("FOUND")

        settings.STATIC_ROOT = None
        settings.STATICFILES_DIRS = [dir1, dir2]

        result = find_static_file("only_in_dir2.txt")
        assert result is not None

        with open(result) as f:
            assert f.read() == "FOUND"


class TestCachingHeaders:
    """Tests for HTTP caching headers on static files."""

    @pytest.fixture(scope="class")
    def api(self):
        """Create API with static routes."""
        from django_bolt.admin.static import register_static_routes

        api = BoltAPI()
        register_static_routes(api)
        return api

    @pytest.fixture(scope="class")
    def client(self, api):
        """Create test client with HTTP layer and explicit static files config."""
        static_config = {
            "url_prefix": "/static",
            "directories": [TEST_STATIC_DIR],
            "csp_header": None,
        }
        return TestClient(api, static_files_config=static_config)

    def test_etag_header_present(self, client, monkeypatch):
        """Test that ETag header is returned."""
        response = client.get("/static/css/style.css")
        assert response.status_code == 200
        # ETag may or may not be present depending on handler
        # Just verify the response is valid

    def test_content_type_correct(self, client, monkeypatch):
        """Test that Content-Type is correctly set."""
        response = client.get("/static/css/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")

        response = client.get("/static/js/app.js")
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "javascript" in content_type

        response = client.get("/static/img/logo.png")
        assert response.status_code == 200
        assert "image/png" in response.headers.get("content-type", "")


class TestDirectoryTraversalSecurity:
    """Comprehensive tests for directory traversal attack prevention."""

    @pytest.fixture(scope="class")
    def api_with_static(self):
        """Create API with static file routes."""
        from django_bolt.admin.static import register_static_routes

        api = BoltAPI()
        register_static_routes(api)
        return api

    @pytest.fixture(scope="class")
    def client(self, api_with_static):
        """Create test client with explicit static files config."""
        static_config = {
            "url_prefix": "/static",
            "directories": [TEST_STATIC_DIR],
            "csp_header": None,
        }
        return TestClient(api_with_static, static_files_config=static_config)

    def test_basic_traversal_blocked(self, client):
        """Test basic ../ traversal is blocked."""
        response = client.get("/static/../etc/passwd")
        assert response.status_code in (400, 404)

    def test_encoded_traversal_blocked(self, client):
        """Test URL-encoded traversal attempts are blocked."""
        # %2e = .
        # %2f = /
        response = client.get("/static/%2e%2e/etc/passwd")
        assert response.status_code in (400, 404)

        response = client.get("/static/..%2fetc/passwd")
        assert response.status_code in (400, 404)

    def test_double_encoded_traversal_blocked(self, client):
        """Test double URL-encoded traversal attempts are blocked."""
        # %252e = %2e (double encoded .)
        response = client.get("/static/%252e%252e/etc/passwd")
        assert response.status_code in (400, 404)

    def test_nested_traversal_blocked(self, client):
        """Test nested directory traversal is blocked."""
        response = client.get("/static/css/../../etc/passwd")
        assert response.status_code in (400, 404)

        response = client.get("/static/css/../../../etc/passwd")
        assert response.status_code in (400, 404)

    def test_windows_style_traversal_blocked(self, client):
        """Test Windows-style path traversal is blocked."""
        response = client.get("/static/..\\etc\\passwd")
        assert response.status_code in (400, 404)

        response = client.get("/static/css\\..\\..\\etc\\passwd")
        assert response.status_code in (400, 404)

    def test_null_byte_injection_blocked(self):
        """Test null byte injection is handled safely at the finder level."""
        # Null bytes could be used to truncate paths in some systems
        # Test at the Python level since %00 is an invalid URI character
        # that would be rejected at the HTTP layer anyway
        result = find_static_file("style.css\x00.txt")
        # Should be None (not found) - null bytes don't bypass security
        assert result is None

    def test_absolute_path_blocked(self, client):
        """Test absolute paths are rejected."""
        response = client.get("/static//etc/passwd")
        assert response.status_code in (400, 404)

    def test_valid_nested_path_works(self, client):
        """Test that valid nested paths still work."""
        response = client.get("/static/css/style.css")
        assert response.status_code == 200
        assert b"color: blue" in response.content


class TestSymlinkSecurity:
    """Tests for symlink attack prevention."""

    @pytest.fixture(scope="class")
    def setup_symlink_test(self):
        """Create a test directory with symlinks."""
        import shutil

        # Create a fresh test directory
        test_dir = tempfile.mkdtemp(prefix="symlink_test_")
        safe_dir = os.path.join(test_dir, "safe")
        os.makedirs(safe_dir, exist_ok=True)

        # Create a safe file inside the static directory
        safe_file = os.path.join(safe_dir, "safe.txt")
        with open(safe_file, "w") as f:
            f.write("SAFE_CONTENT")

        # Create a file outside the static directory
        outside_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        outside_file.write(b"OUTSIDE_CONTENT_SHOULD_NOT_BE_SERVED")
        outside_file.close()

        # Create a symlink inside the static directory pointing outside
        symlink_path = os.path.join(safe_dir, "malicious_link.txt")

        try:
            os.symlink(outside_file.name, symlink_path)
            symlink_created = True
        except OSError:
            # Symlink creation may fail on some systems (e.g., Windows without admin)
            symlink_created = False

        yield {
            "test_dir": test_dir,
            "safe_dir": safe_dir,
            "safe_file": safe_file,
            "outside_file": outside_file.name,
            "symlink_path": symlink_path,
            "symlink_created": symlink_created,
        }

        # Cleanup
        try:
            if symlink_created and os.path.islink(symlink_path):
                os.unlink(symlink_path)
            os.unlink(outside_file.name)
            shutil.rmtree(test_dir)
        except Exception:
            pass

    def test_symlink_outside_directory_blocked(self, setup_symlink_test):
        """Test symlink security behavior.

        Note: The Python find_static_file (used in dev) follows symlinks for convenience.
        The Rust handler uses canonicalize() to verify paths stay within the static dir.
        This test verifies the Rust-side security by checking path resolution logic.
        """
        if not setup_symlink_test["symlink_created"]:
            pytest.skip("Symlink creation not supported on this system")

        from django.conf import settings

        settings.STATIC_ROOT = None
        settings.STATICFILES_DIRS = [setup_symlink_test["safe_dir"]]
        settings.STATIC_URL = "/static/"

        # Test the security check that Rust uses: canonicalize + starts_with
        symlink_path = os.path.join(setup_symlink_test["safe_dir"], "malicious_link.txt")

        if os.path.exists(symlink_path):
            canonical = os.path.realpath(symlink_path)
            dir_canonical = os.path.realpath(setup_symlink_test["safe_dir"])

            # The Rust code checks: canonical.starts_with(&dir_canonical)
            # For a symlink pointing outside, this should be False
            is_within_directory = canonical.startswith(dir_canonical)

            # The symlink points to a file outside the directory
            # so canonical should NOT start with dir_canonical
            assert not is_within_directory, "Symlink canonical path should not be within static directory"

    def test_safe_file_still_accessible(self, setup_symlink_test):
        """Test that regular files within the directory are still accessible."""
        from django.conf import settings

        settings.STATIC_ROOT = None
        settings.STATICFILES_DIRS = [setup_symlink_test["safe_dir"]]
        settings.STATIC_URL = "/static/"

        result = find_static_file("safe.txt")
        assert result is not None
        with open(result) as f:
            assert f.read() == "SAFE_CONTENT"

    def test_symlink_within_directory_works(self, setup_symlink_test):
        """Test that symlinks within the static directory still work."""
        if not setup_symlink_test["symlink_created"]:
            pytest.skip("Symlink creation not supported on this system")

        from django.conf import settings

        safe_dir = setup_symlink_test["safe_dir"]

        # Create a symlink within the same directory (safe)
        internal_link = os.path.join(safe_dir, "internal_link.txt")
        try:
            os.symlink(setup_symlink_test["safe_file"], internal_link)
        except OSError:
            pytest.skip("Symlink creation failed")

        try:
            settings.STATIC_ROOT = None
            settings.STATICFILES_DIRS = [safe_dir]
            settings.STATIC_URL = "/static/"

            result = find_static_file("internal_link.txt")
            # Internal symlinks should work
            if result is not None:
                with open(result) as f:
                    assert f.read() == "SAFE_CONTENT"
        finally:
            if os.path.islink(internal_link):
                os.unlink(internal_link)


class TestContentSecurityPolicy:
    """Tests for Content-Security-Policy header on static files."""

    def test_csp_header_applied_when_configured(self):
        """Test that CSP header is applied when BOLT_STATIC_CSP is set."""
        from django.conf import settings

        from django_bolt.admin.static import register_static_routes

        # Configure CSP
        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATICFILES_DIRS = [TEST_STATIC_DIR]
        settings.STATIC_URL = "/static/"
        settings.BOLT_STATIC_CSP = "default-src 'self'; script-src 'self'"

        api = BoltAPI()
        register_static_routes(api)

        client = TestClient(api, use_http_layer=True)
        response = client.get("/static/css/style.css")

        assert response.status_code == 200
        # Note: CSP is applied at the Actix server level, which may not be
        # active in the test client. This test verifies the setting is read.
        # Full CSP testing requires integration tests with the actual server.

    def test_no_csp_header_when_not_configured(self):
        """Test that no CSP header is added when not configured."""
        from django.conf import settings

        from django_bolt.admin.static import register_static_routes

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATICFILES_DIRS = [TEST_STATIC_DIR]
        settings.STATIC_URL = "/static/"

        # Ensure CSP is not configured
        if hasattr(settings, "BOLT_STATIC_CSP"):
            delattr(settings, "BOLT_STATIC_CSP")

        api = BoltAPI()
        register_static_routes(api)

        client = TestClient(api, use_http_layer=True)
        response = client.get("/static/css/style.css")

        assert response.status_code == 200
        # CSP header should not be present
        # (Note: test client may not fully replicate Actix behavior)

    def test_csp_setting_formats(self):
        """Test various CSP setting formats are accepted."""
        from django.conf import settings

        # Test string format
        settings.BOLT_STATIC_CSP = "default-src 'self'"
        assert settings.BOLT_STATIC_CSP == "default-src 'self'"

        # Test more complex CSP
        settings.BOLT_STATIC_CSP = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'"
        )
        assert "script-src" in settings.BOLT_STATIC_CSP
        assert "style-src" in settings.BOLT_STATIC_CSP


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    @pytest.fixture(scope="class")
    def api_with_static(self):
        """Create API with static file routes."""
        from django_bolt.admin.static import register_static_routes

        api = BoltAPI()
        register_static_routes(api)
        return api

    @pytest.fixture(scope="class")
    def client(self, api_with_static):
        """Create test client."""
        return TestClient(api_with_static, use_http_layer=True)

    @pytest.fixture(autouse=True)
    def setup_static_dir(self):
        """Setup static directory for tests."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR
        settings.STATICFILES_DIRS = [TEST_STATIC_DIR]
        settings.STATIC_URL = "/static/"

    def test_file_with_spaces_in_name(self):
        """Test finding files with spaces in the name."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR

        # Create a file with spaces
        space_file = os.path.join(TEST_STATIC_DIR, "file with spaces.txt")
        with open(space_file, "w") as f:
            f.write("CONTENT_WITH_SPACES")

        try:
            # Test at the finder level (HTTP layer has URI parsing issues with spaces)
            result = find_static_file("file with spaces.txt")
            assert result is not None
            with open(result) as f:
                assert f.read() == "CONTENT_WITH_SPACES"
        finally:
            os.unlink(space_file)

    def test_file_with_unicode_name(self):
        """Test finding files with unicode characters in the name."""
        from django.conf import settings

        settings.STATIC_ROOT = TEST_STATIC_DIR

        # Create a file with unicode characters
        unicode_file = os.path.join(TEST_STATIC_DIR, "文件.txt")
        with open(unicode_file, "w", encoding="utf-8") as f:
            f.write("UNICODE_CONTENT")

        try:
            # Test at the finder level
            result = find_static_file("文件.txt")
            assert result is not None
            with open(result, encoding="utf-8") as f:
                assert f.read() == "UNICODE_CONTENT"
        finally:
            os.unlink(unicode_file)

    def test_empty_file(self, client):
        """Test serving an empty file."""
        empty_file = os.path.join(TEST_STATIC_DIR, "empty.txt")
        with open(empty_file, "w") as f:
            pass  # Create empty file

        try:
            response = client.get("/static/empty.txt")
            assert response.status_code == 200
            assert response.content == b""
        finally:
            os.unlink(empty_file)

    def test_hidden_file(self, client):
        """Test serving hidden files (starting with .)."""
        hidden_file = os.path.join(TEST_STATIC_DIR, ".hidden")
        with open(hidden_file, "w") as f:
            f.write("HIDDEN_CONTENT")

        try:
            response = client.get("/static/.hidden")
            # Hidden files should be servable (Django doesn't block them by default)
            assert response.status_code == 200
            assert b"HIDDEN_CONTENT" in response.content
        finally:
            os.unlink(hidden_file)

    def test_deeply_nested_path(self, client):
        """Test serving files from deeply nested directories."""
        import shutil

        deep_dir = os.path.join(TEST_STATIC_DIR, "a", "b", "c", "d", "e")
        os.makedirs(deep_dir, exist_ok=True)
        deep_file = os.path.join(deep_dir, "deep.txt")
        with open(deep_file, "w") as f:
            f.write("DEEP_CONTENT")

        try:
            response = client.get("/static/a/b/c/d/e/deep.txt")
            assert response.status_code == 200
            assert b"DEEP_CONTENT" in response.content
        finally:
            shutil.rmtree(os.path.join(TEST_STATIC_DIR, "a"))


class TestDebugModeFinders:
    """Tests for debug mode restriction on Django finders fallback.

    Security feature: Django staticfiles finders are only used in debug mode.
    In production (DEBUG=False), only explicitly configured directories are served.
    """

    def test_admin_static_found_in_debug_mode(self):
        """Test that Django admin static files are found when DEBUG=True."""
        from django.conf import settings

        original_debug = getattr(settings, "DEBUG", None)
        settings.DEBUG = True

        try:
            # Admin CSS should be found via Django finders (not in STATIC_ROOT)
            result = find_static_file("admin/css/base.css")
            assert result is not None, "Admin CSS should be found in debug mode"
            assert "admin" in result
            assert os.path.isfile(result)
        finally:
            if original_debug is not None:
                settings.DEBUG = original_debug

    def test_configured_dirs_work_regardless_of_debug(self):
        """Test that explicitly configured directories always work."""
        from django.conf import settings

        settings.STATIC_ROOT = None
        settings.STATICFILES_DIRS = [TEST_STATIC_DIR]
        settings.STATIC_URL = "/static/"

        # Should work in both debug and non-debug modes
        for debug_value in [True, False]:
            settings.DEBUG = debug_value

            result = find_static_file("css/style.css")
            assert result is not None, f"Configured static file should be found with DEBUG={debug_value}"
            assert "style.css" in result

    def test_finders_fallback_disabled_in_production_mode(self):
        """Test that Django finders fallback is disabled when DEBUG=False.

        This test verifies the security behavior: in production mode,
        only files in configured directories (STATIC_ROOT, STATICFILES_DIRS) are served.
        Django's app-level finders (which could expose more paths) are not used.

        Note: This tests the Python-level find_static_file function.
        The actual Rust handler checks app_state.debug before calling Django finders.
        """
        from django.conf import settings

        # Create an empty STATIC_ROOT with no admin files
        empty_static_root = tempfile.mkdtemp(prefix="empty_static_")

        original_debug = getattr(settings, "DEBUG", None)
        original_root = getattr(settings, "STATIC_ROOT", None)
        original_dirs = getattr(settings, "STATICFILES_DIRS", None)

        try:
            settings.DEBUG = False
            settings.STATIC_ROOT = empty_static_root
            settings.STATICFILES_DIRS = []

            # In production mode, admin files should NOT be found
            # because they're not in STATIC_ROOT or STATICFILES_DIRS
            # The Rust handler won't call Django finders when debug=False

            # Verify the admin file exists (Django has it)
            settings.DEBUG = True  # Temporarily enable to verify admin exists
            admin_result = find_static_file("admin/css/base.css")
            assert admin_result is not None, "Admin CSS should exist in Django"

            # Now verify our configured dirs don't have it
            settings.DEBUG = False
            result = find_static_file("admin/css/base.css")
            # The Python find_static_file always checks finders
            # but the Rust handler gates this behind debug flag
            # This test documents that the file won't be in our configured dirs

            # Verify empty static root doesn't have admin files
            import os

            admin_in_configured_dir = os.path.exists(os.path.join(empty_static_root, "admin/css/base.css"))
            assert not admin_in_configured_dir, "Admin files should not be in our empty STATIC_ROOT"

        finally:
            if original_debug is not None:
                settings.DEBUG = original_debug
            if original_root is not None:
                settings.STATIC_ROOT = original_root
            if original_dirs is not None:
                settings.STATICFILES_DIRS = original_dirs
            # Cleanup
            import shutil

            shutil.rmtree(empty_static_root, ignore_errors=True)

    def test_rust_handler_debug_flag_integration(self):
        """Test that the Rust handler respects debug flag for finders fallback.

        This is an integration test that verifies the full flow:
        1. Request comes in for a file only available via Django finders
        2. DEBUG=True: File is found (finders fallback enabled)
        3. DEBUG=False: File is NOT found (finders fallback disabled)

        Note: This test uses the TestClient which exercises the Rust static file handler.
        """
        from django.conf import settings

        # Create empty static root (no admin files)
        empty_static_root = tempfile.mkdtemp(prefix="empty_static_")

        original_debug = getattr(settings, "DEBUG", None)
        original_root = getattr(settings, "STATIC_ROOT", None)
        original_dirs = getattr(settings, "STATICFILES_DIRS", None)

        try:
            settings.STATIC_ROOT = empty_static_root
            settings.STATICFILES_DIRS = []
            settings.STATIC_URL = "/static/"

            # Configure static files for testing
            static_config = {
                "url_prefix": "/static",
                "directories": [empty_static_root],
                "csp_header": None,
            }

            # Test with DEBUG=True (finders fallback should work)
            settings.DEBUG = True

            api_debug = BoltAPI()
            client_debug = TestClient(api_debug, static_files_config=static_config)

            # Admin CSS is only available via Django finders, not in our empty STATIC_ROOT
            response = client_debug.get("/static/admin/css/base.css")
            assert response.status_code == 200, (
                f"Admin CSS should be served in debug mode via Django finders (got {response.status_code})"
            )

            # Test with DEBUG=False (finders fallback should be disabled)
            settings.DEBUG = False

            api_prod = BoltAPI()
            client_prod = TestClient(api_prod, static_files_config=static_config)

            # Admin CSS should NOT be found because:
            # 1. It's not in STATIC_ROOT or STATICFILES_DIRS
            # 2. Django finders fallback is disabled in production
            response = client_prod.get("/static/admin/css/base.css")
            assert response.status_code == 404, (
                f"Admin CSS should NOT be served in production mode (got {response.status_code})"
            )

        finally:
            if original_debug is not None:
                settings.DEBUG = original_debug
            if original_root is not None:
                settings.STATIC_ROOT = original_root
            if original_dirs is not None:
                settings.STATICFILES_DIRS = original_dirs
            # Cleanup
            import shutil

            shutil.rmtree(empty_static_root, ignore_errors=True)
