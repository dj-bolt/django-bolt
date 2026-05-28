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
