"""Tests for request.META property (Django template compatibility)."""

from __future__ import annotations

import logging

import pytest
from django.http import QueryDict
from django.test import RequestFactory, override_settings

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

# Locations that Bolt joined to the host as text. Django parses and joins them.
LOCATIONS = (
    "@evil.example/x",
    ".evil.example/x",
    ":80@evil.example/",
    "//evil.example/x",
    "?page=2",
    "foo",
    "../up",
    "",
    "/x\r\nSet-Cookie: a=b",
    "/café x",
    "https://cdn.example/a b",
)


@pytest.fixture(scope="module")
def api():
    """Create test API with routes that access request.META."""
    api = BoltAPI()

    @api.get("/meta")
    async def get_meta(request):
        """Return all META keys and values."""
        meta = request.META
        return dict(meta)

    @api.get("/meta/method")
    async def get_meta_method(request):
        """Return REQUEST_METHOD from META."""
        return {"method": request.META.get("REQUEST_METHOD")}

    @api.get("/meta/path")
    async def get_meta_path(request):
        """Return PATH_INFO from META."""
        return {"path": request.META.get("PATH_INFO")}

    @api.get("/meta/query")
    async def get_meta_query(request):
        """Return QUERY_STRING from META."""
        return {"query": request.META.get("QUERY_STRING")}

    @api.get("/meta/urls")
    async def get_meta_urls(request):
        """Return the URLs built from the query, and the query as Django parses QUERY_STRING."""
        return {
            "full_path": request.get_full_path(),
            "absolute_uri": request.build_absolute_uri(),
            "reparsed": QueryDict(request.META["QUERY_STRING"]).dict(),
        }

    @api.get("/meta/headers")
    async def get_meta_headers(request):
        """Return HTTP_* headers from META."""
        meta = request.META
        return {
            "host": meta.get("HTTP_HOST"),
            "custom": meta.get("HTTP_X_CUSTOM"),
            "content_type": meta.get("CONTENT_TYPE"),
            "content_length": meta.get("CONTENT_LENGTH"),
        }

    @api.get("/meta/cached")
    async def get_meta_cached(request):
        """Verify META is cached (same object on multiple accesses)."""
        meta1 = request.META
        meta2 = request.META
        return {"cached": meta1 is meta2}

    @api.get("/meta/scheme")
    async def get_meta_scheme(request):
        """Return the values that the request scheme sets."""
        return {"server_port": request.META["SERVER_PORT"], "absolute_uri": request.build_absolute_uri()}

    @api.get("/meta/scheme-attribute")
    async def get_scheme_attribute(request):
        """Return the scheme of the request, and the scheme of its absolute URI."""
        return {
            "scheme": request.scheme,
            "is_secure": request.is_secure(),
            "absolute_uri": request.build_absolute_uri(),
        }

    @api.get("/meta/absolute")
    async def get_meta_absolute(request):
        """Return the absolute URI of the request."""
        return {"absolute_uri": request.build_absolute_uri()}

    @api.get("/meta/location/{index}")
    async def get_meta_location(request, index: int):
        """Return the absolute URI of one of LOCATIONS."""
        return {"absolute_uri": request.build_absolute_uri(LOCATIONS[index])}

    @api.get("/meta/absolute-location")
    async def get_meta_absolute_location(request):
        """Return the absolute URI of an absolute location."""
        return {"absolute_uri": request.build_absolute_uri("https://cdn.example/a")}

    @api.post("/meta/post")
    async def post_meta(request):
        """Return META for POST request."""
        return {"method": request.META.get("REQUEST_METHOD")}

    return api


@pytest.fixture(scope="module")
def client(api):
    """Create test client."""
    return TestClient(api)


class TestRequestMETA:
    """Test request.META property for Django compatibility."""

    def test_meta_request_method(self, client):
        """META contains REQUEST_METHOD."""
        response = client.get("/meta/method")
        assert response.status_code == 200
        assert response.json()["method"] == "GET"

    def test_meta_request_method_post(self, client):
        """META contains REQUEST_METHOD for POST."""
        response = client.post("/meta/post")
        assert response.status_code == 200
        assert response.json()["method"] == "POST"

    def test_meta_path_info(self, client):
        """META contains PATH_INFO."""
        response = client.get("/meta/path")
        assert response.status_code == 200
        assert response.json()["path"] == "/meta/path"

    def test_meta_query_string_empty(self, client):
        """META contains empty QUERY_STRING when no query params."""
        response = client.get("/meta/query")
        assert response.status_code == 200
        assert response.json()["query"] == ""

    def test_meta_query_string_with_params(self, client):
        """META contains QUERY_STRING with query params."""
        response = client.get("/meta/query?foo=bar&baz=123")
        assert response.status_code == 200
        query = response.json()["query"]
        # Query string should contain both params (order may vary)
        assert "foo=bar" in query
        assert "baz=123" in query

    def test_meta_http_headers(self, client):
        """META contains HTTP_* headers."""
        response = client.get(
            "/meta/headers",
            headers={
                "Host": "example.com",
                "X-Custom": "test-value",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "example.com"
        assert data["custom"] == "test-value"

    def test_meta_content_type_header(self, client):
        """META contains CONTENT_TYPE (without HTTP_ prefix)."""
        response = client.get(
            "/meta/headers",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["content_type"] == "application/json"

    def test_meta_content_length_header(self, client):
        """META contains CONTENT_LENGTH (without HTTP_ prefix)."""
        response = client.get(
            "/meta/headers",
            headers={"Content-Length": "42"},
        )
        assert response.status_code == 200
        assert response.json()["content_length"] == "42"

    def test_meta_is_cached(self, client):
        """META dict is cached on first access."""
        response = client.get("/meta/cached")
        assert response.status_code == 200
        assert response.json()["cached"] is True

    def test_meta_all_keys(self, client):
        """META contains all expected keys."""
        response = client.get(
            "/meta?test=1",
            headers={
                "Host": "localhost",
                "Accept": "application/json",
            },
        )
        assert response.status_code == 200
        meta = response.json()

        # Standard META keys
        assert "REQUEST_METHOD" in meta
        assert "PATH_INFO" in meta
        assert "QUERY_STRING" in meta

        # Headers as HTTP_*
        assert "HTTP_HOST" in meta
        assert "HTTP_ACCEPT" in meta

        # Verify values
        assert meta["REQUEST_METHOD"] == "GET"
        assert meta["PATH_INFO"] == "/meta"
        assert "test=1" in meta["QUERY_STRING"]

    def test_meta_server_info(self, client):
        """META contains SERVER_NAME, SERVER_PORT, and SERVER_PROTOCOL."""
        response = client.get(
            "/meta",
            headers={"Host": "example.com:8080"},
        )
        assert response.status_code == 200
        meta = response.json()

        # Server info keys
        assert "SERVER_NAME" in meta
        assert "SERVER_PORT" in meta
        assert "SERVER_PROTOCOL" in meta

        # Verify values (parsed from Host header)
        assert meta["SERVER_NAME"] == "example.com"
        assert meta["SERVER_PORT"] == "8080"
        assert meta["SERVER_PROTOCOL"] == "HTTP/1.1"
        # Bolt does not end TLS, and this client is not a trusted proxy.
        assert meta["wsgi.url_scheme"] == "http"

    def test_meta_server_info_default_port(self, client):
        """META parses Host header without port correctly."""
        response = client.get(
            "/meta",
            headers={"Host": "example.com"},
        )
        assert response.status_code == 200
        meta = response.json()

        assert meta["SERVER_NAME"] == "example.com"
        assert meta["SERVER_PORT"] == "80"  # Default port when not specified

    @pytest.mark.parametrize(
        ("trusted_proxies", "forwarded_proto", "scheme", "port"),
        [
            # A client that is not a declared proxy cannot choose the scheme.
            ([], "https", "http", "80"),
            # A declared proxy can. The test client connects from 127.0.0.1.
            (["127.0.0.1"], "https", "https", "443"),
            # The proxy closest to Bolt adds the rightmost value.
            (["127.0.0.1"], "https, http", "http", "80"),
            (["127.0.0.1"], "ftp", "http", "80"),
        ],
    )
    def test_x_forwarded_proto_sets_the_scheme_only_from_a_trusted_proxy(
        self, api, trusted_proxies, forwarded_proto, scheme, port
    ):
        """SERVER_PORT and build_absolute_uri() follow X-Forwarded-Proto only from BOLT_TRUSTED_PROXIES."""
        with override_settings(BOLT_TRUSTED_PROXIES=trusted_proxies), TestClient(api) as client:
            response = client.get(
                "/meta/scheme",
                headers={"Host": "example.com", "X-Forwarded-Proto": forwarded_proto},
            )
        assert response.status_code == 200
        assert response.json() == {
            "server_port": port,
            "absolute_uri": f"{scheme}://example.com/meta/scheme",
        }

    @pytest.mark.parametrize(
        ("headers", "extra_settings", "host"),
        [
            ({"Host": "example.com"}, {}, "example.com"),
            # X-Forwarded-Host counts only with USE_X_FORWARDED_HOST, as in Django.
            ({"Host": "example.com", "X-Forwarded-Host": "evil.example"}, {}, "example.com"),
            ({"Host": "internal", "X-Forwarded-Host": "example.com"}, {"USE_X_FORWARDED_HOST": True}, "example.com"),
        ],
    )
    def test_build_absolute_uri_gets_the_host_as_django_does(self, api, headers, extra_settings, host):
        with override_settings(ALLOWED_HOSTS=["example.com"], **extra_settings), TestClient(api) as client:
            response = client.get("/meta/absolute", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"absolute_uri": f"http://{host}/meta/absolute"}

    @pytest.mark.parametrize("index", range(len(LOCATIONS)))
    def test_build_absolute_uri_joins_a_location_as_django_does(self, api, index):
        """Django parses the location and joins it to the request URL. Text glued to the host can change the host."""
        path = f"/meta/location/{index}"
        with override_settings(ALLOWED_HOSTS=["example.com"]), TestClient(api) as client:
            response = client.get(path, headers={"Host": "example.com"})
            expected = RequestFactory().get(path, HTTP_HOST="example.com").build_absolute_uri(LOCATIONS[index])
        assert response.status_code == 200
        assert response.json() == {"absolute_uri": expected}

    def test_build_absolute_uri_follows_secure_proxy_ssl_header(self, api):
        """Django's build_absolute_uri() applies SECURE_PROXY_SSL_HEADER. Bolt's SERVER_PORT does not."""
        with (
            override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")),
            TestClient(api) as client,
        ):
            response = client.get("/meta/scheme", headers={"Host": "example.com", "X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        assert response.json() == {"server_port": "80", "absolute_uri": "https://example.com/meta/scheme"}

    @pytest.mark.parametrize(
        ("extra_settings", "forwarded_proto", "scheme"),
        [
            ({}, None, "http"),
            # X-Forwarded-Proto counts only from a peer in BOLT_TRUSTED_PROXIES.
            ({}, "https", "http"),
            ({"BOLT_TRUSTED_PROXIES": ["127.0.0.1"]}, "https", "https"),
            ({"BOLT_TRUSTED_PROXIES": ["127.0.0.1"]}, "ftp", "http"),
            # SECURE_PROXY_SSL_HEADER applies on top, as in Django.
            ({"SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https")}, "https", "https"),
            (
                {"BOLT_TRUSTED_PROXIES": ["127.0.0.1"], "SECURE_PROXY_SSL_HEADER": ("HTTP_X_SCHEME", "https")},
                "https",
                "http",
            ),
        ],
    )
    def test_scheme_and_is_secure_are_the_scheme_of_build_absolute_uri(
        self, api, extra_settings, forwarded_proto, scheme
    ):
        headers = {"Host": "example.com", "X-Scheme": "http"}
        if forwarded_proto is not None:
            headers["X-Forwarded-Proto"] = forwarded_proto
        with override_settings(**extra_settings), TestClient(api) as client:
            response = client.get("/meta/scheme-attribute", headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "scheme": scheme,
            "is_secure": scheme == "https",
            "absolute_uri": f"{scheme}://example.com/meta/scheme-attribute",
        }

    def test_build_absolute_uri_allows_the_host_of_the_default_test_client(self, api):
        """Django's test runner adds `testserver` to ALLOWED_HOSTS. TestClient sends that host."""
        with override_settings(ALLOWED_HOSTS=["testserver"]), TestClient(api) as client:
            response = client.get("/meta/absolute")
        assert response.status_code == 200
        assert response.json() == {"absolute_uri": "http://testserver/meta/absolute"}

    def test_build_absolute_uri_returns_an_absolute_location_without_the_host(self, api):
        """Django does not read the host for an absolute location, so the host is not checked."""
        with override_settings(ALLOWED_HOSTS=["example.com"]), TestClient(api) as client:
            response = client.get("/meta/absolute-location", headers={"Host": "evil.example"})
        assert response.status_code == 200
        assert response.json() == {"absolute_uri": "https://cdn.example/a"}

    @pytest.mark.parametrize(("debug", "detail"), [(True, "ALLOWED_HOSTS"), (False, "Bad Request")])
    def test_build_absolute_uri_refuses_a_host_outside_allowed_hosts(self, api, caplog, debug, detail):
        """Django raises DisallowedHost. Bolt answers 400 and logs it, as Django does."""
        with (
            override_settings(ALLOWED_HOSTS=["example.com"], DEBUG=debug),
            caplog.at_level(logging.ERROR),
            TestClient(api) as client,
        ):
            response = client.get("/meta/absolute", headers={"Host": "evil.example"})
        assert response.status_code == 400
        assert detail in response.json()["detail"]
        # One record, as in Django. A client can send a bad host on each request.
        assert [record.name for record in caplog.records if record.levelno >= logging.ERROR] == [
            "django.security.DisallowedHost"
        ]

    def test_meta_remote_addr(self, client):
        """META contains REMOTE_ADDR and REMOTE_HOST."""
        response = client.get("/meta")
        assert response.status_code == 200
        meta = response.json()

        # Client info keys
        assert "REMOTE_ADDR" in meta
        assert "REMOTE_HOST" in meta

        # Both should have a valid IP address (or "127.0.0.1" for localhost)
        assert meta["REMOTE_ADDR"]  # Non-empty
        assert meta["REMOTE_HOST"]  # Non-empty

    def test_meta_script_name(self, client):
        """META contains SCRIPT_NAME (usually empty for Django apps)."""
        response = client.get("/meta")
        assert response.status_code == 200
        meta = response.json()

        assert "SCRIPT_NAME" in meta
        assert meta["SCRIPT_NAME"] == ""

    def test_meta_query_string_preserves_encoding(self, client):
        """META QUERY_STRING is the query string as the client sent it, as in Django."""
        response = client.get("/meta/query?foo=hello%20world&bar=a%2Bb&q=a+b")
        assert response.status_code == 200
        assert response.json()["query"] == "foo=hello%20world&bar=a%2Bb&q=a+b"

    def test_urls_keep_the_query_as_sent(self, client):
        """get_full_path() and build_absolute_uri() keep the encoded query, as in Django."""
        query = "q=hello+world&tag=a%2Bb&text=a%26b%3Dc"
        response = client.get(f"/meta/urls?{query}")
        assert response.status_code == 200
        data = response.json()
        assert data["full_path"] == f"/meta/urls?{query}"
        assert data["absolute_uri"].endswith(f"/meta/urls?{query}")
        # Django middleware parses QUERY_STRING again. It must get the values that the handler gets.
        assert data["reparsed"] == {"q": "hello world", "tag": "a+b", "text": "a&b=c"}
