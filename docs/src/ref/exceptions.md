---
icon: lucide/alert-triangle
---

# Exceptions Reference

All exceptions are importable from `django_bolt.exceptions`.

```python
from django_bolt.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
    BadRequest,
    NotFound,
    # ... etc
)
```

## HTTP Exceptions

### HTTPException

Base class for all HTTP error responses.

<!-- fragment -->
```python
HTTPException(
    status_code: int = 500,
    detail: str = "",
    headers: dict[str, str] = None,
    extra: dict | list = None,
)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `status_code` | `int` | HTTP status code |
| `detail` | `str` | Error message (defaults to HTTP phrase) |
| `headers` | `dict` | Headers to include in response |
| `extra` | `dict \| list` | Additional data in response |

**Response format:**

```json
{
    "detail": "Error message",
    "extra": { }
}
```

## Client Errors (4xx)

| Exception | Status | Description |
|-----------|--------|-------------|
| `BadRequest` | 400 | Invalid request syntax or parameters |
| `Unauthorized` | 401 | Authentication required or failed |
| `Forbidden` | 403 | Insufficient permissions |
| `NotFound` | 404 | Resource not found |
| `MethodNotAllowed` | 405 | HTTP method not supported |
| `NotAcceptable` | 406 | Cannot produce requested format |
| `Conflict` | 409 | Request conflicts with current state |
| `Gone` | 410 | Resource permanently deleted |
| `UnprocessableEntity` | 422 | Semantic validation error |
| `TooManyRequests` | 429 | Rate limit exceeded |

**Usage:**

```python
from django_bolt.exceptions import NotFound, BadRequest

@api.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id < 1:
        raise BadRequest(detail="Invalid user ID")

    user = await User.objects.filter(id=user_id).afirst()
    if not user:
        raise NotFound(detail="User not found")

    return {"id": user.id}
```

## Server Errors (5xx)

| Exception | Status | Description |
|-----------|--------|-------------|
| `InternalServerError` | 500 | Unexpected server error |
| `NotImplemented` | 501 | Feature not implemented |
| `BadGateway` | 502 | Invalid upstream response |
| `ServiceUnavailable` | 503 | Server temporarily unavailable |
| `GatewayTimeout` | 504 | Upstream server timeout |

## Validation Errors

### RequestValidationError

Raised when request data fails validation. Returns **422 Unprocessable Entity**.

<!-- fragment -->
```python
RequestValidationError(
    errors: list[dict],
    body: Any = None,
)
```

**Multi-error collection:** Django-Bolt collects all `@field_validator` and `@model_validator` errors before raising, so users see all issues at once:

```python
# Access all errors
try:
    serializer = MySerializer(email="bad", password="short")
except RequestValidationError as e:
    all_errors = e.errors()  # Returns list of error dicts
    print(str(e))  # "body.email: Invalid email; body.password: Too short"
```

**Response format:**

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Invalid email",
            "type": "value_error"
        },
        {
            "loc": ["body", "password"],
            "msg": "Password too short",
            "type": "value_error"
        }
    ]
}
```

### ResponseValidationError

Raised when handler return value fails validation. Returns **500 Internal Server Error**.

<!-- fragment -->
```python
ResponseValidationError(
    errors: list[dict],
    body: Any = None,
)
```

## Validation Layers

Django-Bolt's Serializer has two validation layers:

| Layer | Source | Raw msgspec | With `model_validate()` |
|-------|--------|-------------|-------------------------|
| **Meta constraints** | `Meta(min_length, pattern, ge, le, ...)` | Fail-fast | **Collects all errors** |
| **Custom validators** | `@field_validator`, `@model_validator` | N/A | **Collects all errors** |

### Multi-error collection

When using `model_validate()` or `model_validate_json()`, **all errors are collected** before raising `RequestValidationError`:

```python
# Both Meta constraints AND custom validators collect errors
try:
    UserSerializer.model_validate({
        "name": "X",        # Meta: min_length=2 violated
        "email": "bad",     # Meta: pattern violated
        "age": 200          # Meta: le=150 violated
    })
except RequestValidationError as e:
    print(len(e.errors()))  # 3 errors, not just 1
```

This approach is inspired by [Litestar](https://litestar.dev/) - when validation fails, each field is validated individually to collect all errors.

### When to expect which exception

| How you validate | Exception | Multi-error? |
|------------------|-----------|--------------|
| `Serializer.model_validate(dict)` | `RequestValidationError` | Yes |
| `Serializer.model_validate_json(json)` | `RequestValidationError` | Yes |
| `Serializer(field=value)` (direct) | `RequestValidationError` | Yes (custom only) |
| `msgspec.convert(data, Serializer)` | `msgspec.ValidationError` | No (fail-fast) |
| `msgspec.json.decode(json, type=Serializer)` | `msgspec.ValidationError` | No (fail-fast) |

**Note**: Direct instantiation (`Serializer(field=value)`) runs custom validators but **bypasses Meta constraints** - this is msgspec's design for performance.

## Validation Error Types

When validation fails, Django-Bolt returns structured errors with these types:

| Type | Description | Example Message |
|------|-------------|-----------------|
| `missing_field` | Required field not provided | `Object missing required field 'price'` |
| `validation_error` | Type mismatch or Meta constraint violation | `Expected 'int', got 'str'` |
| `json_invalid` | Malformed JSON | `Invalid JSON at line 2, column 5` |
| `value_error` | Custom `@field_validator` or `@model_validator` failure | `Invalid email format` |

### Error Location (`loc`)

The `loc` field indicates where the error occurred:

| Location | Format | Example |
|----------|--------|---------|
| Request body | `["body", "field"]` | `["body", "email"]` |
| Nested field | `["body", "user", "name"]` | `["body", "address", "city"]` |
| Array item | `["body", 0, "field"]` | `["body", 0, "id"]` |
| Query param | `["query", "param"]` | `["query", "page"]` |
| Path param | `["path", "param"]` | `["path", "user_id"]` |
| Header | `["header", "name"]` | `["header", "authorization"]` |

### Example Validation Errors

**Missing required field:**

```json
{
    "detail": [
        {
            "loc": ["body", "price"],
            "msg": "Object missing required field `price`",
            "type": "missing_field"
        }
    ]
}
```

**Type mismatch:**

```json
{
    "detail": [
        {
            "loc": ["body", "age"],
            "msg": "Expected `int`, got `str` - at `$.age`",
            "type": "validation_error"
        }
    ]
}
```

**Invalid JSON:**

```json
{
    "detail": [
        {
            "loc": ["body", 2, 15],
            "msg": "Invalid JSON at line 2, column 15: unexpected character",
            "type": "json_invalid"
        }
    ]
}
```

**Multiple errors:**

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Expected `str`, got `int`",
            "type": "validation_error"
        },
        {
            "loc": ["body", "age"],
            "msg": "Object missing required field `age`",
            "type": "missing_field"
        }
    ]
}
```

## Adding Custom Headers

```python
raise Unauthorized(
    detail="Token expired",
    headers={"WWW-Authenticate": "Bearer"}
)
```

## Adding Extra Data

```python
raise BadRequest(
    detail="Validation failed",
    extra={
        "field": "email",
        "value": "invalid@",
        "suggestion": "Enter a valid email address"
    }
)
```

**Response:**

```json
{
    "detail": "Validation failed",
    "extra": {
        "field": "email",
        "value": "invalid@",
        "suggestion": "Enter a valid email address"
    }
}
```

## Status Code Summary

| Code | Exception | When Raised |
|------|-----------|-------------|
| 400 | `BadRequest` | Explicit raise only |
| 401 | `Unauthorized` | Authentication failed |
| 403 | `Forbidden` | Guard/permission denied |
| 404 | `NotFound` | Resource not found, `FileNotFoundError` |
| 405 | `MethodNotAllowed` | Wrong HTTP method |
| 406 | `NotAcceptable` | Cannot produce requested format |
| 409 | `Conflict` | State conflict |
| 410 | `Gone` | Resource deleted |
| 422 | `UnprocessableEntity` | **Validation errors**, missing required params (automatic) |
| 429 | `TooManyRequests` | Rate limit exceeded |
| 500 | `InternalServerError` | Unhandled exceptions, response validation |
| 501 | `NotImplemented` | Feature not implemented |
| 502 | `BadGateway` | Upstream error |
| 503 | `ServiceUnavailable` | Server unavailable |
| 504 | `GatewayTimeout` | Upstream timeout |
