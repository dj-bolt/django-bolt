from __future__ import annotations

import pytest

pytestmark = pytest.mark.server_integration


def test_static_files_include_csp_header(make_server_project):
    project = make_server_project(
        installed_apps=[
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
        ],
        middleware=[
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
            "django.middleware.csp.ContentSecurityPolicyMiddleware",
        ],
        templates=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                    ]
                },
            }
        ],
        urls_content="""
        from django.contrib import admin
        from django.urls import path

        urlpatterns = [path("admin/", admin.site.urls)]
        """,
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        SECURE_CSP = {
            "default-src": ["'self'"],
            "script-src": ["'self'"],
        }
        """,
        extra_files={
            "staticassets/test.css": "body { color: blue; }\n",
        },
    )

    with project.start() as server:
        response = server.get("/static/test.css")

    assert response.status_code == 200
    assert "text/css" in response.headers.get("content-type", "")
    assert "color: blue" in response.text
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp


def test_static_cache_control_header_from_max_age_setting(make_server_project):
    """BOLT_STATIC_MAX_AGE = N sends `Cache-Control: public, max-age=N` on /static/."""
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        BOLT_STATIC_MAX_AGE = 31536000
        """,
        extra_files={
            "staticassets/app.js": "console.log(1);\n",
        },
    )

    with project.start() as server:
        response = server.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "public, max-age=31536000"


def test_static_dirs_as_path_objects_are_served(make_server_project):
    """STATICFILES_DIRS entries given as Path objects must register and serve.

    Idiomatic Django is `STATICFILES_DIRS = [BASE_DIR / "static"]` — Path
    objects, not strings. The Rust reader previously did
    `extract::<Vec<String>>()`, which silently failed on a list of Paths, so
    the static scope never registered and every /static/* request 404'd. The
    reader now converts each entry via str().

    Note: no `str(...)` wrapper here — that's the whole point of the test.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [BASE_DIR / "staticassets"]
        """,
        extra_files={
            "staticassets/app.css": "body { color: green; }\n",
        },
    )

    with project.start() as server:
        response = server.get("/static/app.css")

    assert response.status_code == 200, (
        "Path-object STATICFILES_DIRS must register the static scope, "
        f"got {response.status_code}"
    )
    assert "color: green" in response.text
    # Confirms it's the native handler (it adds nosniff), not a fallback.
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_static_supports_head_requests(make_server_project):
    """HEAD on static must return GET's headers with an empty body."""
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        """,
        extra_files={
            "staticassets/test.css": "body { color: blue; }\n",
        },
    )

    with project.start() as server:
        head = server.request("HEAD", "/static/test.css")
        get = server.get("/static/test.css")

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers.get("content-length") == get.headers.get("content-length")
    assert head.headers.get("etag") == get.headers.get("etag")


def test_static_response_includes_nosniff(make_server_project):
    """Static responses must carry X-Content-Type-Options: nosniff.

    Cheap defense-in-depth: even if STATIC_ROOT ever ends up with
    user-influenced content (compiled themes, plugin assets, etc.), browsers
    won't sniff a different type and execute it as something more dangerous.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        """,
        extra_files={"staticassets/app.js": "console.log(1);\n"},
    )

    with project.start() as server:
        response = server.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff", (
        "nosniff must be applied to static responses, not just media"
    )


@pytest.mark.parametrize(
    "dotfile_path",
    [".env", ".git/config", ".htaccess", "css/.hidden"],
)
def test_static_dotfile_paths_return_404(make_server_project, dotfile_path):
    """Dotfile paths under STATIC_ROOT/STATICFILES_DIRS must not be served.

    STATIC_ROOT is the bundled-asset target for `collectstatic`. If anything
    sensitive leaked in (`.env`, `.git/`), it must not be reachable via
    /static/. nginx and Apache deny dotfiles by default for this reason.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        """,
        extra_files={f"staticassets/{dotfile_path}": "SECRET=do-not-leak\n"},
    )

    with project.start() as server:
        response = server.get(f"/static/{dotfile_path}")

    assert response.status_code == 404, (
        f"/static/{dotfile_path} must not be served, got {response.status_code}"
    )
    assert b"SECRET=do-not-leak" not in response.content


def test_static_cache_control_stays_public(make_server_project):
    """Regression: static keeps `public, max-age=N` (only media flips to `private`).

    Static assets are admin-curated, content-hashed, and identical for every
    user — `public` caching is correct and desirable for CDN behavior. This
    test guards against accidentally extending the media `private` fix to
    static.
    """
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        BOLT_STATIC_MAX_AGE = 31536000
        """,
        extra_files={"staticassets/app.js": "console.log(1);\n"},
    )

    with project.start() as server:
        response = server.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "public, max-age=31536000"


def test_static_url_with_scope_param_chars_is_ignored(make_server_project):
    """STATIC_URL with `{` or `:` must not silently become an actix scope param."""
    project = make_server_project(
        installed_apps=["django.contrib.staticfiles"],
        settings_extra="""
        STATIC_URL = "/static/{tenant}/"
        STATICFILES_DIRS = [str(BASE_DIR / "staticassets")]
        """,
        extra_files={"staticassets/app.css": "body { color: red; }\n"},
    )

    with project.start() as server:
        literal = server.get("/static/{tenant}/app.css")
        guessed = server.get("/static/anything/app.css")
        plain = server.get("/static/app.css")

    for label, response in [("literal", literal), ("guessed", guessed), ("plain", plain)]:
        assert response.status_code == 404, (
            f"{label} request must 404 when STATIC_URL is malformed, got {response.status_code}"
        )
        assert b"color: red" not in response.content
