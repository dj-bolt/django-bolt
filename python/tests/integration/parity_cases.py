"""Requests that must get the same answer from TestClient and from runbolt.

``test_testclient_production_parity.py`` sends each case through TestClient.
``test_testclient_parity_server_integration.py`` sends it to a real runbolt
server. Both check the same expectations, so a difference on either side fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ParityCase:
    id: str
    settings: dict[str, Any]
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # Files to make under the case directory, as relative path -> text.
    files: dict[str, str] = field(default_factory=dict)
    status: int = 200
    expect_headers: dict[str, str] = field(default_factory=dict)
    absent_headers: tuple[str, ...] = ()
    expect_json: Any = None

    def prepare(self, directory: Path) -> dict[str, Any]:
        """Make the files of the case. Return its settings, with "{dir}" filled in."""
        for relative, text in self.files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return {key: _fill(value, str(directory)) for key, value in self.settings.items()}

    def check(self, response: httpx.Response) -> None:
        assert response.status_code == self.status, response.text
        for name, value in self.expect_headers.items():
            assert response.headers.get(name) == value, name
        for name in self.absent_headers:
            assert name not in response.headers, name
        if self.expect_json is not None:
            assert response.json() == self.expect_json


def _fill(value: Any, directory: str) -> Any:
    if isinstance(value, str):
        return value.replace("{dir}", directory)
    if isinstance(value, list):
        return [_fill(item, directory) for item in value]
    return value


PREFLIGHT = {"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}

CASES = [
    ParityCase(
        id="upload-limit-defaults-to-one-megabyte",
        settings={},
        method="POST",
        path="/echo-len",
        body=b"x" * (2 * 1024 * 1024),
        status=413,
    ),
    ParityCase(
        id="max-header-size-applies",
        settings={"BOLT_MAX_HEADER_SIZE": 200},
        method="GET",
        path="/headers",
        headers={"X-Big": "y" * 300},
        status=400,
    ),
    ParityCase(
        id="cors-preflight-server-defaults",
        settings={"CORS_ALLOWED_ORIGINS": ["https://a.example"]},
        method="OPTIONS",
        path="/plain",
        headers=PREFLIGHT,
        status=204,
        expect_headers={
            "access-control-allow-origin": "https://a.example",
            "access-control-allow-headers": "Content-Type, Authorization",
            "access-control-max-age": "3600",
        },
    ),
    ParityCase(
        id="cors-origin-regexes-alone",
        settings={"CORS_ALLOWED_ORIGIN_REGEXES": [r"^https://.*\.example$"]},
        method="GET",
        path="/plain",
        headers={"Origin": "https://b.example"},
        expect_headers={"access-control-allow-origin": "https://b.example"},
    ),
    ParityCase(
        # Only CORS_ALLOW_ALL_ORIGINS allows every origin.
        id="cors-star-origin-is-not-a-wildcard",
        settings={"CORS_ALLOWED_ORIGINS": ["*"]},
        method="GET",
        path="/plain",
        headers={"Origin": "https://evil.test"},
        absent_headers=("access-control-allow-origin",),
    ),
    ParityCase(
        id="cors-empty-origins-unknown-route-preflight",
        settings={"CORS_ALLOWED_ORIGINS": []},
        method="OPTIONS",
        path="/nope",
        headers=PREFLIGHT,
        status=404,
    ),
    ParityCase(
        id="static-max-age-sets-cache-control",
        settings={"STATIC_URL": "/static/", "STATICFILES_DIRS": ["{dir}/static"], "BOLT_STATIC_MAX_AGE": 60},
        files={"static/app.css": "body {}\n"},
        method="GET",
        path="/static/app.css",
        expect_headers={"cache-control": "public, max-age=60"},
    ),
    ParityCase(
        id="static-scope-matches-at-a-segment-boundary",
        settings={"STATIC_URL": "/static/", "STATICFILES_DIRS": ["{dir}/static"]},
        files={"static/app.css": "body {}\n", "static/x/app.css": "body {}\n"},
        method="GET",
        path="/staticx/app.css",
        status=404,
    ),
    ParityCase(
        id="media-files-are-served",
        settings={"MEDIA_URL": "/media/", "MEDIA_ROOT": "{dir}/media", "BOLT_MEDIA_MAX_AGE": 30},
        files={"media/a.txt": "hello\n"},
        method="GET",
        path="/media/a.txt",
        expect_headers={"cache-control": "private, max-age=30"},
    ),
    ParityCase(
        id="trusted-proxy-forwards-the-client-address",
        settings={"BOLT_TRUSTED_PROXIES": ["127.0.0.1"]},
        method="GET",
        path="/remote",
        headers={"X-Forwarded-For": "203.0.113.9"},
        expect_json={"remote": "203.0.113.9"},
    ),
    ParityCase(
        id="untrusted-peer-is-the-client-address",
        settings={},
        method="GET",
        path="/remote",
        headers={"X-Forwarded-For": "203.0.113.9"},
        expect_json={"remote": "127.0.0.1"},
    ),
]
