"""
Test sync function support in django-bolt.
Tests that sync and async handlers work correctly with parameters, dependencies, middleware, and auth.
"""

from __future__ import annotations

import inspect  # noqa: PLC0415
import time
from typing import Annotated

import jwt
import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.exceptions import HTTPException
from django_bolt.middleware import cors, rate_limit
from django_bolt.params import Depends, Header, Query
from django_bolt.testing import TestClient

from .test_models import Article

TEST_JWT_SECRET = "test-secret-key-for-sync-functions-012345"

# ========================
# Shared Models
# ========================


class UserCreate(msgspec.Struct):
    """User creation request model."""

    name: str
    email: str


@pytest.fixture(scope="module")
def api():
    """Create test API with sync and async handlers."""
    api = BoltAPI()

    # ========================
    # Basic Handlers
    # ========================

    # Test 1: Async function (existing behavior)
    @api.get("/async")
    async def async_handler():
        return {"type": "async"}

    # Test 2: Sync function with inline=True (default)
    @api.get("/sync-inline")
    def sync_inline_handler():
        return {"type": "sync", "mode": "inline"}

    # Test 3: Sync function without inline (uses default)
    @api.get("/sync-blocking")
    def sync_blocking_handler():
        time.sleep(0.001)  # Simulate slow operation
        return {"type": "sync", "mode": "blocking"}

    # ========================
    # Parameter Handlers
    # ========================

    # Path parameters (sync)
    @api.get("/sync/users/{user_id}")
    def sync_path_param(user_id: int):
        return {"user_id": user_id, "name": f"User {user_id}"}

    # Path and query parameters (sync)
    @api.get("/sync/items/{item_id}")
    def sync_path_query_param(item_id: int, search: str = "default"):
        return {"item_id": item_id, "search": search}

    # Query parameters (sync)
    @api.get("/sync/search")
    def sync_query_param(q: str, limit: int = 10):
        return {"query": q, "limit": limit}

    # Header parameters (sync)
    @api.get("/sync/headers")
    def sync_header_param(x_custom: Annotated[str, Header()]):
        return {"header": x_custom}

    # Query with annotation (sync)
    @api.get("/sync/annotated-query")
    def sync_annotated_query(count: Annotated[int, Query()] = 5):
        return {"count": count}

    # ========================
    # Request Body Handlers
    # ========================

    # POST with JSON body (sync)
    @api.post("/sync/users")
    def sync_post_body(user: UserCreate):
        return {"id": 1, "name": user.name, "email": user.email}

    # ========================
    # Dependency Injection
    # ========================

    async def get_current_user():
        """Dependency that returns current user."""
        return {"user_id": 42, "username": "testuser"}

    async def get_db_connection():
        """Dependency that returns a DB-like connection."""
        return {"connection": "active"}

    # Sync handler with single dependency
    @api.get("/sync/with-dependency")
    def sync_with_dependency(user: dict = Depends(get_current_user)):
        return {"user": user}

    # Sync handler with multiple dependencies
    @api.get("/sync/with-multiple-dependencies")
    def sync_with_multiple_deps(
        user: dict = Depends(get_current_user),
        db: dict = Depends(get_db_connection),
    ):
        return {"user": user, "db": db}

    # ========================
    # Middleware Handlers (CORS)
    # ========================

    @api.get("/sync/cors-enabled")
    @cors(origins=["http://localhost:3000"])
    def sync_cors_handler():
        return {"cors": "enabled"}

    # ========================
    # Middleware Handlers (Rate Limiting)
    # ========================

    @api.get("/sync/rate-limited")
    @rate_limit(rps=100, burst=10)
    def sync_rate_limited():
        return {"status": "ok"}

    # ========================
    # Auth Handlers
    # ========================

    auth_config = JWTAuthentication(secret=TEST_JWT_SECRET, algorithms=["HS256"])

    @api.get("/sync/protected", auth=[auth_config], guards=[IsAuthenticated()])
    def sync_protected_handler():
        return {"message": "Protected sync handler"}

    @api.get("/sync/unprotected")
    def sync_unprotected_handler():
        return {"message": "Public sync handler"}

    # ========================
    # Mixed Async/Sync Handlers with Same Features
    # ========================

    @api.get("/async/users/{user_id}")
    async def async_path_param(user_id: int):
        return {"user_id": user_id, "name": f"User {user_id}", "is_async": True}

    @api.post("/async/users")
    async def async_post_body(user: UserCreate):
        return {"id": 1, "name": user.name, "email": user.email, "is_async": True}

    @api.get("/async/with-dependency")
    async def async_with_dependency(user: dict = Depends(get_current_user)):
        return {"user": user, "is_async": True}

    @api.get("/async/protected", auth=[auth_config], guards=[IsAuthenticated()])
    async def async_protected_handler():
        return {"message": "Protected async handler", "is_async": True}

    # Store handlers for inspection tests
    api._async_handler = async_handler
    api._sync_inline_handler = sync_inline_handler
    api._sync_blocking_handler = sync_blocking_handler
    api._sync_path_param = sync_path_param
    api._auth_config = auth_config

    return api


@pytest.fixture
def client(api):
    """Test client for making HTTP requests."""
    with TestClient(api) as client:
        yield client


class TestMetadataDetection:
    """Test that handlers have correct metadata."""

    def test_async_handler_metadata(self, api):
        """Async handlers should be marked as async."""
        # Find the route with the async handler
        handler_id = None
        for _method, _path, hid, handler in api._routes:
            if handler == api._async_handler:
                handler_id = hid
                break
        meta = api._handler_meta[handler_id]
        assert meta["is_async"] is True, "Async handler should be marked as async"

    def test_sync_inline_handler_metadata(self, api):
        """Sync inline handlers should be marked as sync."""
        # Find the route with the sync inline handler
        handler_id = None
        for _method, _path, hid, handler in api._routes:
            if handler == api._sync_inline_handler:
                handler_id = hid
                break
        meta = api._handler_meta[handler_id]
        assert meta["is_async"] is False, "Sync handler should be marked as sync"

    def test_sync_blocking_handler_metadata(self, api):
        """Sync blocking handlers should be marked as sync."""
        # Find the route with the sync blocking handler
        handler_id = None
        for _method, _path, hid, handler in api._routes:
            if handler == api._sync_blocking_handler:
                handler_id = hid
                break
        meta = api._handler_meta[handler_id]
        assert meta["is_async"] is False, "Sync handler should be marked as sync"


class TestHandlerInspection:
    """Test Python's inspect functions on handlers."""

    def test_async_handler_is_coroutine_function(self, api):
        """Async handlers should be coroutine functions."""
        assert inspect.iscoroutinefunction(api._async_handler), (
            "Async handler should be recognized as coroutine function"
        )

    def test_sync_inline_handler_is_not_coroutine(self, api):
        """Sync inline handlers should NOT be coroutine functions."""
        assert not inspect.iscoroutinefunction(api._sync_inline_handler), (
            "Sync handler should not be recognized as coroutine function"
        )

    def test_sync_blocking_handler_is_not_coroutine(self, api):
        """Sync blocking handlers should NOT be coroutine functions."""
        assert not inspect.iscoroutinefunction(api._sync_blocking_handler), (
            "Sync handler should not be recognized as coroutine function"
        )


class TestAsyncHandlerExecution:
    """Test async handler execution via HTTP."""

    def test_async_handler_returns_correct_response(self, client):
        """Async handler should return expected response."""
        response = client.get("/async")
        assert response.status_code == 200, "Async handler should return 200"
        data = response.json()
        assert data == {"type": "async"}, "Async handler should return correct data"

    def test_async_handler_response_type(self, client):
        """Async handler response should be proper JSON."""
        response = client.get("/async")
        assert response.headers["content-type"] == "application/json"


class TestSyncInlineHandlerExecution:
    """Test sync inline handler execution via HTTP."""

    def test_sync_inline_handler_returns_correct_response(self, client):
        """Sync inline handler should return expected response."""
        response = client.get("/sync-inline")
        assert response.status_code == 200, "Sync inline handler should return 200"
        data = response.json()
        assert data == {
            "type": "sync",
            "mode": "inline",
        }, "Sync inline handler should return correct data"

    def test_sync_inline_handler_response_type(self, client):
        """Sync inline handler response should be proper JSON."""
        response = client.get("/sync-inline")
        assert response.headers["content-type"] == "application/json"

    def test_sync_inline_handler_is_actually_sync(self, api):
        """Verify handler is sync function."""
        assert not inspect.iscoroutinefunction(api._sync_inline_handler), "Handler should be sync"


class TestSyncBlockingHandlerExecution:
    """Test sync blocking handler execution via HTTP."""

    def test_sync_blocking_handler_returns_correct_response(self, client):
        """Sync blocking handler should return expected response."""
        response = client.get("/sync-blocking")
        assert response.status_code == 200, "Sync blocking handler should return 200"
        data = response.json()
        assert data == {
            "type": "sync",
            "mode": "blocking",
        }, "Sync blocking handler should return correct data"

    def test_sync_blocking_handler_response_type(self, client):
        """Sync blocking handler response should be proper JSON."""
        response = client.get("/sync-blocking")
        assert response.headers["content-type"] == "application/json"

    def test_sync_blocking_handler_is_actually_sync(self, api):
        """Verify handler is sync function."""
        assert not inspect.iscoroutinefunction(api._sync_blocking_handler), "Handler should be sync"


class TestComparisonBetweenSyncModes:
    """Test the differences between sync inline and blocking modes."""

    def test_both_sync_modes_return_same_structure(self, client):
        """Both sync modes should return similar response structure."""
        inline_response = client.get("/sync-inline")
        blocking_response = client.get("/sync-blocking")

        inline_data = inline_response.json()
        blocking_data = blocking_response.json()

        assert inline_data["type"] == blocking_data["type"] == "sync"
        assert "mode" in inline_data and "mode" in blocking_data

    def test_sync_handlers_all_work(self, client):
        """All sync handlers should be accessible and working."""
        async_response = client.get("/async")
        inline_response = client.get("/sync-inline")
        blocking_response = client.get("/sync-blocking")

        assert async_response.status_code == 200
        assert inline_response.status_code == 200
        assert blocking_response.status_code == 200

    def test_responses_are_all_json(self, client):
        """All responses should be valid JSON."""
        endpoints = ["/async", "/sync-inline", "/sync-blocking"]
        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 200
            # This should not raise if JSON is valid
            data = response.json()
            assert isinstance(data, dict), f"Response from {endpoint} should be a dict"


# ========================
# PARAMETER EXTRACTION TESTS
# ========================


class TestSyncPathParameters:
    """Test sync handlers with path parameters."""

    def test_sync_path_parameter(self, client):
        """Sync handler should extract path parameter."""
        response = client.get("/sync/users/123")
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == 123
        assert data["name"] == "User 123"

    def test_sync_path_parameter_invalid_type(self, client):
        """Sync handler should fail with invalid path parameter type."""
        response = client.get("/sync/users/invalid")
        assert response.status_code == 422
        assert response.json()["detail"].startswith("Path parameter 'user_id': Invalid integer 'invalid'")


class TestSyncPathAndQueryParameters:
    """Test sync handlers with path and query parameters."""

    def test_sync_path_and_query_with_default(self, client):
        """Sync handler should extract path and use query default."""
        response = client.get("/sync/items/456")
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == 456
        assert data["search"] == "default"

    def test_sync_path_and_query_with_value(self, client):
        """Sync handler should extract path and query parameter."""
        response = client.get("/sync/items/456?search=custom")
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == 456
        assert data["search"] == "custom"


class TestSyncQueryParameters:
    """Test sync handlers with query parameters."""

    def test_sync_query_parameter_required(self, client):
        """Sync handler should extract required query parameter."""
        response = client.get("/sync/search?q=test")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "test"
        assert data["limit"] == 10

    def test_sync_query_parameter_with_limit(self, client):
        """Sync handler should extract query parameters with values."""
        response = client.get("/sync/search?q=test&limit=20")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "test"
        assert data["limit"] == 20

    def test_sync_query_parameter_missing_required(self, client):
        """Sync handler should fail without required query parameter."""
        response = client.get("/sync/search")
        assert response.status_code == 422
        assert response.json() == {"detail": "Missing required query parameter: q"}


class TestSyncHeaderParameters:
    """Test sync handlers with header parameters."""

    def test_sync_header_parameter(self, client):
        """Sync handler should extract header parameter."""
        response = client.get("/sync/headers", headers={"X-Custom": "test-value"})
        assert response.status_code == 200
        data = response.json()
        assert data["header"] == "test-value"


class TestSyncAnnotatedQueryParameters:
    """Test sync handlers with annotated query parameters."""

    def test_sync_annotated_query_default(self, client):
        """Sync handler should use default value for annotated query."""
        response = client.get("/sync/annotated-query")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 5

    def test_sync_annotated_query_custom_value(self, client):
        """Sync handler should extract annotated query parameter."""
        response = client.get("/sync/annotated-query?count=15")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 15


# ========================
# REQUEST BODY TESTS
# ========================


class TestSyncRequestBodyValidation:
    """Test sync handlers with request body validation."""

    def test_sync_post_with_valid_body(self, client):
        """Sync handler should accept valid request body."""
        response = client.post(
            "/sync/users",
            json={"name": "John", "email": "john@example.com"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "John"
        assert data["email"] == "john@example.com"

    def test_sync_post_with_invalid_body(self, client):
        """Sync handler should reject invalid request body."""
        response = client.post(
            "/sync/users",
            json={"name": "John"},  # Missing email
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "email"]
        assert response.json()["detail"][0]["type"] == "missing_field"


# ========================
# DEPENDENCY INJECTION TESTS
# ========================


class TestSyncDependencyInjection:
    """Test sync handlers with dependency injection."""

    def test_sync_single_dependency(self, client):
        """Sync handler should resolve single dependency."""
        response = client.get("/sync/with-dependency")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert data["user"]["user_id"] == 42
        assert data["user"]["username"] == "testuser"

    def test_sync_multiple_dependencies(self, client):
        """Sync handler should resolve multiple dependencies."""
        response = client.get("/sync/with-multiple-dependencies")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "db" in data
        assert data["user"]["user_id"] == 42
        assert data["db"]["connection"] == "active"


# ========================
# MIDDLEWARE TESTS
# ========================


class TestSyncMiddlewareCORS:
    """Test sync handlers with CORS middleware."""

    def test_sync_cors_handler_http_layer(self):
        """Sync handler should include CORS headers with HTTP layer."""
        test_api = BoltAPI()

        @test_api.get("/sync/cors-test")
        @cors(origins=["http://localhost:3000"])
        def cors_handler():
            return {"cors": "enabled"}

        with TestClient(test_api, use_http_layer=True) as client:
            response = client.get(
                "/sync/cors-test",
                headers={"Origin": "http://localhost:3000"},
            )
            assert response.status_code == 200
            assert "access-control-allow-origin" in response.headers


class TestSyncMiddlewareRateLimit:
    """Test sync handlers with rate limiting."""

    def test_sync_rate_limit_handler_http_layer(self):
        """Sync handler should be rate limited with HTTP layer."""
        test_api = BoltAPI()

        @test_api.get("/sync/rate-test")
        @rate_limit(rps=100, burst=3)
        def rate_limited():
            return {"status": "ok"}

        with TestClient(test_api, use_http_layer=True) as client:
            # First 3 requests should succeed (burst)
            for i in range(3):
                response = client.get("/sync/rate-test")
                assert response.status_code == 200, f"Request {i + 1} should succeed"

            # 4th request might be rate limited
            response = client.get("/sync/rate-test")
            # Just verify it returns a valid status
            assert response.status_code in [200, 429]


# ========================
# AUTHENTICATION TESTS
# ========================


class TestSyncAuthentication:
    """Test sync handlers with authentication."""

    def test_sync_unprotected_endpoint(self, client):
        """Sync handler without auth should be accessible."""
        response = client.get("/sync/unprotected")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Public sync handler"

    def test_sync_protected_endpoint_without_token(self, client):
        """Sync protected handler should reject requests without token."""
        response = client.get("/sync/protected")
        assert response.status_code == 401, "Should reject without token"

    def test_sync_protected_endpoint_with_valid_token(self, client):
        """Sync protected handler should accept valid token."""
        token = jwt.encode(
            {"sub": "user123", "exp": int(time.time()) + 3600},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        response = client.get(
            "/sync/protected",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Protected sync handler"


# ========================
# ASYNC VS SYNC COMPARISON TESTS
# ========================


class TestAsyncSyncParityPathParameters:
    """Compare async and sync handlers with path parameters."""

    def test_async_vs_sync_path_parameters(self, client):
        """Async and sync handlers should both extract path parameters."""
        async_response = client.get("/async/users/789")
        sync_response = client.get("/sync/users/789")

        assert async_response.status_code == 200
        assert sync_response.status_code == 200

        async_data = async_response.json()
        sync_data = sync_response.json()

        assert async_data["user_id"] == sync_data["user_id"] == 789
        assert async_data["is_async"] is True
        assert "is_async" not in sync_data or not sync_data.get("is_async")


class TestAsyncSyncParityRequestBody:
    """Compare async and sync handlers with request body."""

    def test_async_vs_sync_post_body(self, client):
        """Async and sync handlers should both validate request bodies."""
        payload = {"name": "Jane", "email": "jane@example.com"}

        async_response = client.post("/async/users", json=payload)
        sync_response = client.post("/sync/users", json=payload)

        assert async_response.status_code == 200
        assert sync_response.status_code == 200

        async_data = async_response.json()
        sync_data = sync_response.json()

        assert async_data["name"] == sync_data["name"] == "Jane"
        assert async_data["email"] == sync_data["email"] == "jane@example.com"


class TestAsyncSyncParityDependencies:
    """Compare async and sync handlers with dependency injection."""

    def test_async_vs_sync_dependencies(self, client):
        """Async and sync handlers should both resolve dependencies."""
        async_response = client.get("/async/with-dependency")
        sync_response = client.get("/sync/with-dependency")

        assert async_response.status_code == 200
        assert sync_response.status_code == 200

        async_data = async_response.json()
        sync_data = sync_response.json()

        assert async_data["user"]["user_id"] == sync_data["user"]["user_id"] == 42


class TestAsyncSyncParityAuthentication:
    """Compare async and sync handlers with authentication."""

    def test_async_vs_sync_protected_endpoints(self, client):
        """Async and sync protected handlers should both reject without token."""
        async_response = client.get("/async/protected")
        sync_response = client.get("/sync/protected")

        assert async_response.status_code == 401
        assert sync_response.status_code == 401

    def test_async_vs_sync_protected_with_token(self, client):
        """Async and sync protected handlers should both accept valid token."""
        token = jwt.encode(
            {"sub": "user123", "exp": int(time.time()) + 3600},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        headers = {"Authorization": f"Bearer {token}"}

        async_response = client.get("/async/protected", headers=headers)
        sync_response = client.get("/sync/protected", headers=headers)

        assert async_response.status_code == 200
        assert sync_response.status_code == 200

        async_data = async_response.json()
        sync_data = sync_response.json()

        assert async_data["is_async"] is True
        assert "Protected" in async_data["message"]
        assert "Protected" in sync_data["message"]


# ========================
# DJANGO ORM TESTS
# ========================


class ArticleResponse(msgspec.Struct):
    """Article response model."""

    id: int
    title: str
    content: str
    status: str
    author: str
    is_published: bool


class ArticleCreate(msgspec.Struct):
    """Article creation request model."""

    title: str
    content: str
    author: str
    status: str = "draft"


class ArticleUpdate(msgspec.Struct):
    """Article update request model."""

    title: str | None = None
    content: str | None = None
    author: str | None = None
    status: str | None = None
    is_published: bool | None = None


@pytest.fixture
def orm_api():
    """Create test API with Django ORM handlers."""
    api = BoltAPI()

    # ========================
    # Sync ORM Handlers
    # ========================

    @api.get("/sync/articles")
    def sync_list_articles(status: str | None = None, limit: int = 10):
        """Sync handler to list articles with optional filtering."""
        queryset = Article.objects.all()
        if status:
            queryset = queryset.filter(status=status)
        articles = list(queryset[:limit].values("id", "title", "content", "status", "author", "is_published"))
        return {"articles": articles, "count": len(articles)}

    @api.get("/sync/articles/raw-values")
    def sync_list_articles_raw_values():
        """Sync handler returning an unevaluated ValuesQuerySet."""
        return Article.objects.order_by("id").values("id", "title", "status")

    @api.get("/sync/articles/{article_id}")
    def sync_get_article(article_id: int):
        """Sync handler to get a single article by ID."""
        try:
            article = Article.objects.get(id=article_id)
            return {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "status": article.status,
                "author": article.author,
                "is_published": article.is_published,
            }
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    @api.post("/sync/articles")
    def sync_create_article(article: ArticleCreate):
        """Sync handler to create a new article."""
        new_article = Article.objects.create(
            title=article.title,
            content=article.content,
            author=article.author,
            status=article.status,
        )
        return {
            "id": new_article.id,
            "title": new_article.title,
            "content": new_article.content,
            "status": new_article.status,
            "author": new_article.author,
            "is_published": new_article.is_published,
        }

    @api.put("/sync/articles/{article_id}")
    def sync_update_article(article_id: int, article: ArticleUpdate):
        """Sync handler to update an article."""
        try:
            db_article = Article.objects.get(id=article_id)

            # Update fields if provided
            if article.title is not None:
                db_article.title = article.title
            if article.content is not None:
                db_article.content = article.content
            if article.author is not None:
                db_article.author = article.author
            if article.status is not None:
                db_article.status = article.status
            if article.is_published is not None:
                db_article.is_published = article.is_published

            db_article.save()

            return {
                "id": db_article.id,
                "title": db_article.title,
                "content": db_article.content,
                "status": db_article.status,
                "author": db_article.author,
                "is_published": db_article.is_published,
            }
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    @api.delete("/sync/articles/{article_id}")
    def sync_delete_article(article_id: int):
        """Sync handler to delete an article."""
        try:
            article = Article.objects.get(id=article_id)
            article.delete()
            return {"message": f"Article {article_id} deleted successfully"}
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    # ========================
    # Async ORM Handlers
    # ========================

    @api.get("/async/articles")
    async def async_list_articles(status: str | None = None, limit: int = 10):
        """Async handler to list articles with optional filtering."""
        queryset = Article.objects.all()
        if status:
            queryset = queryset.filter(status=status)
        articles = [
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "status": article.status,
                "author": article.author,
                "is_published": article.is_published,
            }
            async for article in queryset[:limit]
        ]
        return {"articles": articles, "count": len(articles), "is_async": True}

    @api.get("/async/articles/{article_id}")
    async def async_get_article(article_id: int):
        """Async handler to get a single article by ID."""
        try:
            article = await Article.objects.aget(id=article_id)
            return {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "status": article.status,
                "author": article.author,
                "is_published": article.is_published,
                "is_async": True,
            }
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    @api.post("/async/articles")
    async def async_create_article(article: ArticleCreate):
        """Async handler to create a new article."""
        new_article = await Article.objects.acreate(
            title=article.title,
            content=article.content,
            author=article.author,
            status=article.status,
        )
        return {
            "id": new_article.id,
            "title": new_article.title,
            "content": new_article.content,
            "status": new_article.status,
            "author": new_article.author,
            "is_published": new_article.is_published,
            "is_async": True,
        }

    @api.put("/async/articles/{article_id}")
    async def async_update_article(article_id: int, article: ArticleUpdate):
        """Async handler to update an article."""
        try:
            db_article = await Article.objects.aget(id=article_id)

            # Update fields if provided
            if article.title is not None:
                db_article.title = article.title
            if article.content is not None:
                db_article.content = article.content
            if article.author is not None:
                db_article.author = article.author
            if article.status is not None:
                db_article.status = article.status
            if article.is_published is not None:
                db_article.is_published = article.is_published

            await db_article.asave()

            return {
                "id": db_article.id,
                "title": db_article.title,
                "content": db_article.content,
                "status": db_article.status,
                "author": db_article.author,
                "is_published": db_article.is_published,
                "is_async": True,
            }
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    @api.delete("/async/articles/{article_id}")
    async def async_delete_article(article_id: int):
        """Async handler to delete an article."""
        try:
            article = await Article.objects.aget(id=article_id)
            await article.adelete()
            return {"message": f"Article {article_id} deleted successfully", "is_async": True}
        except Article.DoesNotExist:
            raise HTTPException(status_code=404, detail="Article not found") from None

    # ========================
    # Complex Query Handlers
    # ========================

    @api.get("/sync/articles/search")
    def sync_search_articles(q: str, author: str | None = None):
        """Sync handler with complex query."""
        queryset = Article.objects.filter(title__icontains=q)
        if author:
            queryset = queryset.filter(author=author)

        articles = list(queryset.values("id", "title", "author", "status"))
        return {"results": articles, "count": len(articles)}

    @api.get("/async/articles/search")
    async def async_search_articles(q: str, author: str | None = None):
        """Async handler with complex query."""
        queryset = Article.objects.filter(title__icontains=q)
        if author:
            queryset = queryset.filter(author=author)

        articles = [{"id": a.id, "title": a.title, "author": a.author, "status": a.status} async for a in queryset]
        return {"results": articles, "count": len(articles), "is_async": True}

    @api.get("/sync/articles/count")
    def sync_count_articles(status: str | None = None):
        """Sync handler for aggregation."""
        queryset = Article.objects.all()
        if status:
            queryset = queryset.filter(status=status)

        total = queryset.count()
        published = queryset.filter(is_published=True).count()

        return {
            "total": total,
            "published": published,
            "draft": total - published,
        }

    @api.get("/async/articles/count")
    async def async_count_articles(status: str | None = None):
        """Async handler for aggregation."""
        queryset = Article.objects.all()
        if status:
            queryset = queryset.filter(status=status)

        total = await queryset.acount()
        published = await queryset.filter(is_published=True).acount()

        return {
            "total": total,
            "published": published,
            "draft": total - published,
            "is_async": True,
        }

    return api


@pytest.fixture
def orm_client(orm_api):
    """Test client for Django ORM tests."""
    with TestClient(orm_api) as client:
        yield client


@pytest.mark.django_db(transaction=True)
class TestSyncORMOperations:
    """Test sync handlers with Django ORM operations."""

    def test_sync_list_articles_empty(self, orm_client):
        """Sync handler should return empty list when no articles exist."""
        response = orm_client.get("/sync/articles")
        assert response.status_code == 200
        data = response.json()
        assert "articles" in data
        assert data["count"] == 0
        assert len(data["articles"]) == 0

    def test_sync_create_article(self, orm_client):
        """Sync handler should create a new article."""
        response = orm_client.post(
            "/sync/articles",
            json={
                "title": "Test Article",
                "content": "This is a test article",
                "author": "John Doe",
                "status": "draft",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Article"
        assert data["content"] == "This is a test article"
        assert data["author"] == "John Doe"
        assert data["status"] == "draft"
        assert data["is_published"] is False
        assert "id" in data

    def test_sync_get_article_by_id(self, orm_client):
        """Sync handler should retrieve article by ID."""
        # Create an article first
        create_response = orm_client.post(
            "/sync/articles",
            json={
                "title": "Get Test Article",
                "content": "Content for get test",
                "author": "Jane Doe",
            },
        )
        created_id = create_response.json()["id"]

        # Get the article
        response = orm_client.get(f"/sync/articles/{created_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == created_id
        assert data["title"] == "Get Test Article"
        assert data["author"] == "Jane Doe"

    def test_sync_get_article_not_found(self, orm_client):
        """Sync handler should return 404 for non-existent article."""
        response = orm_client.get("/sync/articles/99999")
        assert response.status_code == 404

    def test_sync_update_article(self, orm_client):
        """Sync handler should update article fields."""
        # Create an article
        create_response = orm_client.post(
            "/sync/articles",
            json={
                "title": "Original Title",
                "content": "Original content",
                "author": "Original Author",
            },
        )
        article_id = create_response.json()["id"]

        # Update the article
        response = orm_client.put(
            f"/sync/articles/{article_id}",
            json={
                "title": "Updated Title",
                "is_published": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["is_published"] is True
        assert data["content"] == "Original content"  # Unchanged
        assert data["author"] == "Original Author"  # Unchanged

    def test_sync_update_article_not_found(self, orm_client):
        """Sync handler should return 404 when updating non-existent article."""
        response = orm_client.put(
            "/sync/articles/99999",
            json={"title": "Updated Title"},
        )
        assert response.status_code == 404

    def test_sync_delete_article(self, orm_client):
        """Sync handler should delete an article."""
        # Create an article
        create_response = orm_client.post(
            "/sync/articles",
            json={
                "title": "Delete Me",
                "content": "This will be deleted",
                "author": "Test Author",
            },
        )
        article_id = create_response.json()["id"]

        # Delete the article
        response = orm_client.delete(f"/sync/articles/{article_id}")
        assert response.status_code == 200
        data = response.json()
        assert "deleted successfully" in data["message"]

        # Verify it's gone
        get_response = orm_client.get(f"/sync/articles/{article_id}")
        assert get_response.status_code == 404

    def test_sync_delete_article_not_found(self, orm_client):
        """Sync handler should return 404 when deleting non-existent article."""
        response = orm_client.delete("/sync/articles/99999")
        assert response.status_code == 404

    def test_sync_list_articles_with_filtering(self, orm_client):
        """Sync handler should filter articles by status."""
        # Create draft and published articles
        orm_client.post(
            "/sync/articles",
            json={"title": "Draft 1", "content": "Content", "author": "Author", "status": "draft"},
        )
        orm_client.post(
            "/sync/articles",
            json={"title": "Published 1", "content": "Content", "author": "Author", "status": "published"},
        )

        # Filter by status
        response = orm_client.get("/sync/articles?status=draft")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["articles"][0]["status"] == "draft"

    def test_sync_list_articles_with_limit(self, orm_client):
        """Sync handler should respect limit parameter."""
        # Create multiple articles
        for i in range(5):
            orm_client.post(
                "/sync/articles",
                json={"title": f"Article {i}", "content": "Content", "author": "Author"},
            )

        # Request with limit
        response = orm_client.get("/sync/articles?limit=3")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3

    def test_sync_raw_values_queryset_response(self, orm_client):
        """Sync handler returning a raw ValuesQuerySet should serialize successfully."""
        orm_client.post(
            "/sync/articles",
            json={"title": "Raw One", "content": "Raw content one", "author": "Raw", "status": "draft"},
        )
        orm_client.post(
            "/sync/articles",
            json={"title": "Raw Two", "content": "Raw content two", "author": "Raw", "status": "published"},
        )

        response = orm_client.get("/sync/articles/raw-values")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 2
        assert {"id", "title", "status"}.issubset(data[0].keys())


@pytest.mark.django_db(transaction=True)
class TestAsyncORMOperations:
    """Test async handlers with Django ORM operations."""

    def test_async_list_articles_empty(self, orm_client):
        """Async handler should return empty list when no articles exist."""
        response = orm_client.get("/async/articles")
        assert response.status_code == 200
        data = response.json()
        assert "articles" in data
        assert data["count"] == 0
        assert data["is_async"] is True

    def test_async_create_article(self, orm_client):
        """Async handler should create a new article."""
        response = orm_client.post(
            "/async/articles",
            json={
                "title": "Async Test Article",
                "content": "This is an async test article",
                "author": "Async Author",
                "status": "published",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Async Test Article"
        assert data["author"] == "Async Author"
        assert data["status"] == "published"
        assert data["is_async"] is True

    def test_async_get_article_by_id(self, orm_client):
        """Async handler should retrieve article by ID."""
        # Create an article
        create_response = orm_client.post(
            "/async/articles",
            json={
                "title": "Async Get Test",
                "content": "Content",
                "author": "Author",
            },
        )
        created_id = create_response.json()["id"]

        # Get the article
        response = orm_client.get(f"/async/articles/{created_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == created_id
        assert data["title"] == "Async Get Test"
        assert data["is_async"] is True

    def test_async_get_article_not_found(self, orm_client):
        """Async handler should return 404 for non-existent article."""
        response = orm_client.get("/async/articles/99999")
        assert response.status_code == 404

    def test_async_update_article(self, orm_client):
        """Async handler should update article fields."""
        # Create an article
        create_response = orm_client.post(
            "/async/articles",
            json={
                "title": "Async Original",
                "content": "Original content",
                "author": "Author",
            },
        )
        article_id = create_response.json()["id"]

        # Update the article
        response = orm_client.put(
            f"/async/articles/{article_id}",
            json={
                "title": "Async Updated",
                "status": "published",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Async Updated"
        assert data["status"] == "published"
        assert data["is_async"] is True

    def test_async_delete_article(self, orm_client):
        """Async handler should delete an article."""
        # Create an article
        create_response = orm_client.post(
            "/async/articles",
            json={
                "title": "Async Delete Me",
                "content": "Content",
                "author": "Author",
            },
        )
        article_id = create_response.json()["id"]

        # Delete the article
        response = orm_client.delete(f"/async/articles/{article_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["is_async"] is True

        # Verify it's gone
        get_response = orm_client.get(f"/async/articles/{article_id}")
        assert get_response.status_code == 404


@pytest.mark.django_db(transaction=True)
class TestSyncComplexQueries:
    """Test sync handlers with complex Django queries."""

    def test_sync_search_articles(self, orm_client):
        """Sync handler should search articles by title."""
        # Create articles
        orm_client.post(
            "/sync/articles",
            json={"title": "Python Tutorial", "content": "Content", "author": "John"},
        )
        orm_client.post(
            "/sync/articles",
            json={"title": "Django Guide", "content": "Content", "author": "Jane"},
        )
        orm_client.post(
            "/sync/articles",
            json={"title": "Python Advanced", "content": "Content", "author": "John"},
        )

        # Search for Python
        response = orm_client.get("/sync/articles/search?q=Python")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert all("Python" in r["title"] for r in data["results"])

    def test_sync_search_with_author_filter(self, orm_client):
        """Sync handler should filter search results by author."""
        # Create articles
        orm_client.post(
            "/sync/articles",
            json={"title": "Python Tutorial", "content": "Content", "author": "John"},
        )
        orm_client.post(
            "/sync/articles",
            json={"title": "Python Advanced", "content": "Content", "author": "Jane"},
        )

        # Search for Python by John
        response = orm_client.get("/sync/articles/search?q=Python&author=John")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["author"] == "John"

    def test_sync_count_articles(self, orm_client):
        """Sync handler should count articles correctly."""
        # Create articles
        orm_client.post(
            "/sync/articles",
            json={"title": "Article 1", "content": "Content", "author": "Author"},
        )
        create_response = orm_client.post(
            "/sync/articles",
            json={"title": "Article 2", "content": "Content", "author": "Author"},
        )

        # Mark one as published
        article_id = create_response.json()["id"]
        orm_client.put(
            f"/sync/articles/{article_id}",
            json={"is_published": True},
        )

        # Count articles
        response = orm_client.get("/sync/articles/count")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["published"] == 1
        assert data["draft"] == 1


@pytest.mark.django_db(transaction=True)
class TestAsyncComplexQueries:
    """Test async handlers with complex Django queries."""

    def test_async_search_articles(self, orm_client):
        """Async handler should search articles by title."""
        # Create articles
        orm_client.post(
            "/async/articles",
            json={"title": "Rust Tutorial", "content": "Content", "author": "Alice"},
        )
        orm_client.post(
            "/async/articles",
            json={"title": "Go Guide", "content": "Content", "author": "Bob"},
        )

        # Search
        response = orm_client.get("/async/articles/search?q=Rust")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["is_async"] is True
        assert "Rust" in data["results"][0]["title"]

    def test_async_count_articles(self, orm_client):
        """Async handler should count articles correctly."""
        # Create articles
        orm_client.post(
            "/async/articles",
            json={"title": "Article 1", "content": "Content", "author": "Author"},
        )
        orm_client.post(
            "/async/articles",
            json={"title": "Article 2", "content": "Content", "author": "Author"},
        )

        # Count
        response = orm_client.get("/async/articles/count")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["is_async"] is True


@pytest.mark.django_db(transaction=True)
class TestSyncAsyncORMParity:
    """Compare sync and async ORM handlers for consistency."""

    def test_create_parity(self, orm_client):
        """Both sync and async create should produce same results."""
        sync_response = orm_client.post(
            "/sync/articles",
            json={"title": "Sync Article", "content": "Content", "author": "Author"},
        )
        async_response = orm_client.post(
            "/async/articles",
            json={"title": "Async Article", "content": "Content", "author": "Author"},
        )

        assert sync_response.status_code == 200
        assert async_response.status_code == 200

        sync_data = sync_response.json()
        async_data = async_response.json()

        # Both should have same structure
        assert set(sync_data.keys()) - {"is_async"} == set(async_data.keys()) - {"is_async"}
        assert async_data["is_async"] is True

    def test_search_parity(self, orm_client):
        """Both sync and async search should return same results."""
        # Create test data
        orm_client.post(
            "/sync/articles",
            json={"title": "Search Test", "content": "Content", "author": "Author"},
        )

        sync_response = orm_client.get("/sync/articles/search?q=Search")
        async_response = orm_client.get("/async/articles/search?q=Search")

        assert sync_response.status_code == 200
        assert async_response.status_code == 200

        sync_data = sync_response.json()
        async_data = async_response.json()

        assert sync_data["count"] == async_data["count"]

    def test_count_parity(self, orm_client):
        """Both sync and async count should return same results."""
        # Create test data
        orm_client.post(
            "/sync/articles",
            json={"title": "Count Test", "content": "Content", "author": "Author"},
        )

        sync_response = orm_client.get("/sync/articles/count")
        async_response = orm_client.get("/async/articles/count")

        sync_data = sync_response.json()
        async_data = async_response.json()

        assert sync_data["total"] == async_data["total"]
        assert sync_data["published"] == async_data["published"]
