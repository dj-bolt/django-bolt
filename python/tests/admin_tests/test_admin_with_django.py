"""
Tests for Django admin integration that actually use a Django project.

These tests configure Django properly and validate admin via ASGI mounts.
"""

import pytest

from django_bolt.admin.admin_detection import detect_admin_url_prefix, detect_admin_url_prefixes
from django_bolt.api import BoltAPI
from django_bolt.testing import TestClient


@pytest.mark.django_db(transaction=True)
def test_admin_root_redirect():
    """Test /admin/ returns content (redirect or login page) via TestClient."""
    from django_bolt.admin.admin_detection import should_enable_admin  # noqa: PLC0415

    # The test settings enable the admin. A skip here would hide a broken detection.
    assert should_enable_admin()

    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    @api.get("/test")
    async def test_route():
        return {"test": "ok"}

    # Check if admin mount was registered
    assert api._admin_routes_registered

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/admin/")

        print("\n[Admin Root Test]")
        print(f"Status: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print(f"Body length: {len(response.content)}")
        print(f"Body preview: {response.text[:300] if response.text else 'N/A'}")

        # Should return a valid response (redirect or login page)
        assert response.status_code in (200, 301, 302), f"Expected valid response, got {response.status_code}"

        # CRITICAL: Body should NOT be empty
        assert len(response.content) > 0, (
            f"Response body is EMPTY! Got {len(response.content)} bytes. Admin mount path is broken."
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("urlconf", "login_path"),
    [
        ("tests.admin_tests.urls_regex_prefix", "/manage/login/"),
        ("tests.admin_tests.urls_include_before_admin", "/backoffice/login/"),
        ("tests.admin_tests.urls_nested_include", "/portal/internal/staff/login/"),
        ("tests.admin_tests.urls_custom_site", "/office/login/"),
    ],
)
def test_admin_mount_uses_the_prefix_of_the_admin_site(settings, urlconf, login_path):
    """The admin answers at the prefix of its URL pattern, also for a regex pattern or a later pattern."""
    settings.ROOT_URLCONF = urlconf
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    with TestClient(api, use_http_layer=True) as client:
        response = client.get(login_path)

    assert response.status_code == 200
    assert "csrfmiddlewaretoken" in response.text


@pytest.mark.django_db(transaction=True)
def test_admin_at_the_root_path_leaves_the_bolt_routes(settings, capsys):
    """With the admin site at "", Django answers each path that has no Bolt route."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_root_admin"
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(item_id: int):
        return {"item_id": item_id}

    api._register_admin_routes("127.0.0.1", 8000)

    assert "Django answers each path that has no Bolt route" in capsys.readouterr().err
    with TestClient(api, use_http_layer=True) as client:
        assert client.get("/items/7").json() == {"item_id": 7}
        response = client.get("/login/")

    assert response.status_code == 200
    assert "csrfmiddlewaretoken" in response.text


def _use_two_languages(settings, urlconf):
    settings.ROOT_URLCONF = urlconf
    settings.LANGUAGE_CODE = "en"
    settings.LANGUAGES = [("en", "English"), ("fr", "French")]
    settings.MIDDLEWARE = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.locale.LocaleMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ]


@pytest.mark.django_db(transaction=True)
def test_admin_in_i18n_patterns_answers_for_each_language(settings):
    """The admin answers at the prefix of each language, and Django redirects the path with no prefix."""
    _use_two_languages(settings, "tests.admin_tests.urls_i18n")
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    with TestClient(api, use_http_layer=True) as client:
        english = client.get("/en/admin/login/")
        french = client.get("/fr/admin/login/")
        no_prefix = client.get("/admin/login/", follow_redirects=False)

    assert english.status_code == 200
    assert '<html lang="en"' in english.text
    assert french.status_code == 200
    assert '<html lang="fr"' in french.text
    # LocaleMiddleware of Django redirects to the prefix of the default language.
    assert no_prefix.status_code == 302
    assert no_prefix.headers["location"] == "/en/admin/login/"


@pytest.mark.django_db(transaction=True)
def test_admin_in_i18n_patterns_with_no_prefix_for_the_default_language(settings):
    """With prefix_default_language=False, the default language has no prefix."""
    _use_two_languages(settings, "tests.admin_tests.urls_i18n_no_default_prefix")
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    with TestClient(api, use_http_layer=True) as client:
        english = client.get("/admin/login/")
        french = client.get("/fr/admin/login/")

    assert english.status_code == 200
    assert '<html lang="en"' in english.text
    assert french.status_code == 200
    assert '<html lang="fr"' in french.text


@pytest.mark.django_db(transaction=True)
def test_admin_in_i18n_patterns_uses_the_default_language_that_django_serves(settings):
    """With LANGUAGE_CODE "en-us" and no "en-us" in LANGUAGES, Django serves the default language at /en/."""
    _use_two_languages(settings, "tests.admin_tests.urls_i18n")
    settings.LANGUAGE_CODE = "en-us"
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    with TestClient(api, use_http_layer=True) as client:
        # runbolt shows the first prefix in its banner.
        banner = client.get(f"/{detect_admin_url_prefix()}/login/")
        no_prefix = client.get("/admin/login/", follow_redirects=False)
        default_code = client.get("/en-us/admin/login/")

    assert banner.status_code == 200
    assert '<html lang="en"' in banner.text
    assert no_prefix.status_code == 302
    assert no_prefix.headers["location"] == "/en/admin/login/"
    # Django has no page at the prefix of LANGUAGE_CODE.
    assert default_code.status_code == 404
    assert detect_admin_url_prefixes() == ["en/admin", "admin", "fr/admin"]


@pytest.mark.django_db(transaction=True)
def test_admin_in_i18n_patterns_with_no_prefix_keeps_the_path_of_language_code(settings):
    """With prefix_default_language=False, Django serves LANGUAGE_CODE "en-us" at the path with no prefix."""
    _use_two_languages(settings, "tests.admin_tests.urls_i18n_no_default_prefix")
    settings.LANGUAGE_CODE = "en-us"
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    assert detect_admin_url_prefixes() == ["admin", "en/admin", "fr/admin"]
    with TestClient(api, use_http_layer=True) as client:
        banner = client.get(f"/{detect_admin_url_prefix()}/login/")
        english = client.get("/en/admin/login/")

    assert banner.status_code == 200
    assert english.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_admin_behind_a_path_parameter_is_served_with_no_user_action(settings, capsys):
    """A prefix with a path parameter is not static. runbolt gives Django each path with no Bolt route."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_path_parameter"
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(item_id: int):
        return {"item_id": item_id}

    api._register_admin_routes("127.0.0.1", 8000)

    err = capsys.readouterr().err
    assert err.count("[django-bolt] Warning") == 1
    assert "path parameter" in err
    assert "Django answers each path that has no Bolt route" in err
    # The warning does not tell the user to do something that runbolt already did.
    assert "mount_django" not in err
    assert detect_admin_url_prefixes() == [""]
    with TestClient(api, use_http_layer=True) as client:
        acme = client.get("/acme/admin/login/")
        globex = client.get("/globex/admin/")
        bolt_route = client.get("/items/7")
        bolt_validation = client.get("/items/abc")

    assert acme.status_code == 200
    assert acme.text == "admin login of acme"
    assert globex.status_code == 200
    assert globex.text == "admin index of globex"
    # Bolt routes answer first.
    assert bolt_route.json() == {"item_id": 7}
    assert bolt_validation.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_admin_behind_a_path_parameter_leaves_a_bolt_route_at_the_root_path(settings, capsys):
    """A Bolt route at "/" and the root mount of the admin work together. The Bolt route answers "/"."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_path_parameter"
    api = BoltAPI()

    @api.get("/")
    async def home():
        return {"home": True}

    api._register_admin_routes("127.0.0.1", 8000)
    capsys.readouterr()

    with TestClient(api, use_http_layer=True) as client:
        home_response = client.get("/")
        acme = client.get("/acme/admin/login/")

    assert home_response.json() == {"home": True}
    assert acme.status_code == 200
    assert acme.text == "admin login of acme"


def test_admin_at_the_root_path_keeps_the_root_mount_of_the_user(settings):
    """A Django mount at "/" of the user already serves an admin site at the root path."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_root_admin"
    api = BoltAPI()
    api.mount_django("/", clear_root_path=True)

    api._register_admin_routes("127.0.0.1", 8000)

    assert api._admin_routes_registered
    assert [prefix for prefix, _app in api._asgi_mounts] == ["/"]


@pytest.mark.django_db(transaction=True)
def test_admin_behind_a_path_parameter_keeps_the_root_mount_of_the_user(settings, capsys):
    """The workaround of earlier versions, a Django mount at "/", stays valid."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_path_parameter"
    api = BoltAPI()
    api.mount_django("/", clear_root_path=True)

    api._register_admin_routes("127.0.0.1", 8000)

    assert api._admin_routes_registered
    assert [prefix for prefix, _app in api._asgi_mounts] == ["/"]
    assert "[django-bolt] Warning" not in capsys.readouterr().err
    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/acme/admin/login/")

    assert response.text == "admin login of acme"


@pytest.mark.django_db(transaction=True)
def test_admin_login_page():
    """Test /admin/login/ returns HTML page (not empty body) via TestClient."""
    from django_bolt.admin.admin_detection import should_enable_admin  # noqa: PLC0415

    # The test settings enable the admin. A skip here would hide a broken detection.
    assert should_enable_admin()

    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    @api.get("/test")
    async def test_route():
        return {"test": "ok"}

    # Check if admin mount was registered
    assert api._admin_routes_registered

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/admin/login/")

        print("\n[Admin Login Test]")
        print(f"Status: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print(f"Body length: {len(response.content)}")
        print(f"Body preview: {response.text[:300]}")

        # Should return 200 OK
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # CRITICAL: Body should NOT be empty - THIS IS THE BUG
        assert len(response.content) > 0, (
            f"Admin login page body is EMPTY! Got {len(response.content)} bytes. Admin mount path is broken."
        )

        # Should be HTML
        content_type = response.headers.get("content-type", "")
        assert "html" in content_type.lower(), f"Expected HTML, got {content_type}"

        # Should contain login form
        body_text = response.text.lower()
        assert "login" in body_text or "django" in body_text, f"Expected login content, got: {body_text[:200]}"


@pytest.mark.django_db(transaction=True)
def test_admin_mount_serves_only_the_admin_prefix(settings):
    """The admin mount gives Django the admin prefix, not each path of the URLconf."""
    settings.ROOT_URLCONF = "tests.admin_tests.urls_site_page"
    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    with TestClient(api, use_http_layer=True) as client:
        admin_response = client.get("/admin/login/")
        site_response = client.get("/site/")

    assert admin_response.status_code == 200
    assert "csrfmiddlewaretoken" in admin_response.text
    assert site_response.status_code == 404
    assert "a page of Django" not in site_response.text
