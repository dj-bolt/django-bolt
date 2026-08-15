---
icon: lucide/file-json
---

# Serializers

Django-Bolt provides a powerful `Serializer` class built on top of `msgspec.Struct`. It offers field validation, computed fields, dynamic field selection, and Django model integration - all with excellent performance.

## Why use Serializers?

In Django REST Framework, you often need multiple serializer classes for different views:

```python
# DRF approach - multiple serializers
class UserListSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username']

class UserDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'created_at']

class UserAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'created_at', 'is_staff']
```

With Django-Bolt's `Serializer`, you define one class and create views dynamically:

```python
from django_bolt.serializers import Serializer

class UserSerializer(Serializer):
    id: int
    username: str
    email: str
    created_at: str
    is_staff: bool = False

    class Config:
        field_sets = {
            "list": ["id", "username"],
            "detail": ["id", "username", "email", "created_at"],
            "admin": ["id", "username", "email", "created_at", "is_staff"],
        }

# Use different views from the same serializer
UserListSerializer = UserSerializer.fields("list")
UserDetailSerializer = UserSerializer.fields("detail")
```

## Basic usage

### Creating a serializer

```python
from django_bolt.serializers import Serializer

class UserSerializer(Serializer):
    username: str
    email: str
```

Create instances just like dataclasses:

```python
user = UserSerializer(username="alice", email="alice@example.com")
print(user.username)  # "alice"
```

### Converting to dict

```python
user = UserSerializer(username="alice", email="alice@example.com")
data = user.to_dict()
# {'username': 'alice', 'email': 'alice@example.com'}
```

### Using dump() for output

The `dump()` method serializes instances and respects configuration:

```python
user = UserSerializer(username="alice", email="alice@example.com")
data = user.dump()
# {'username': 'alice', 'email': 'alice@example.com'}
```

Options for `dump()`:

```python
# Exclude None values
data = user.dump(exclude_none=True)

# Exclude default values
data = user.dump(exclude_defaults=True)
```

### Default values

```python
class UserSerializer(Serializer):
    username: str
    email: str = "no-email@example.com"

user = UserSerializer(username="bob")
print(user.email)  # "no-email@example.com"
```

### Optional fields

```python
class UserSerializer(Serializer):
    username: str
    email: str | None = None

user = UserSerializer(username="alice")
print(user.email)  # None
```

## Field validation

### The field_validator decorator

Use `@field_validator` to validate and transform individual fields:

```python
from django_bolt.serializers import Serializer, field_validator

class UserSerializer(Serializer):
    email: str

    @field_validator("email")
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value
```

Invalid data raises `msgspec.ValidationError`:

```python
UserSerializer(email="invalid")  # Raises ValidationError
```

A validator may also be declared as a `@classmethod`. Type checkers infer the
first parameter of an undecorated method as the instance type, so a plain
`def validate_email(cls, value)` is reported as incompatible with the
decorator's signature; `@classmethod` states the intent and silences it. Put
`@field_validator` on top:

```python
    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value
```

Both forms behave identically at runtime, in either decorator order.
`@staticmethod` is not accepted (a field validator is called with the class),
and `@model_validator` accepts neither — it receives the constructed instance.
Those combinations raise `TypeError` at class creation rather than leaving a
validator that never runs.

### Transforming values

Validators can transform values before storage:

```python
class UserSerializer(Serializer):
    email: str

    @field_validator("email")
    def normalize_email(cls, value):
        return value.lower().strip()

user = UserSerializer(email="  ALICE@EXAMPLE.COM  ")
print(user.email)  # "alice@example.com"
```

### Multiple validators

Apply multiple validators to a single field:

```python
class UserSerializer(Serializer):
    password: str

    @field_validator("password")
    def check_length(cls, value):
        if len(value) < 8:
            raise ValueError("Password too short")
        return value

    @field_validator("password")
    def check_complexity(cls, value):
        if not any(c.isupper() for c in value):
            raise ValueError("Password must have uppercase")
        return value
```

For the same field, validators run in order - if one fails, subsequent validators on that field don't run.

### Multi-error collection

Django-Bolt **collects all validation errors** from `@field_validator` and `@model_validator` across all fields before raising:

```python
class UserSerializer(Serializer):
    email: str
    password: str

    @field_validator("email")
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value

    @field_validator("password")
    def validate_password(cls, value):
        if len(value) < 8:
            raise ValueError("Password too short")
        return value

# Both email AND password are invalid
try:
    UserSerializer(email="invalid", password="short")
except RequestValidationError as e:
    # Returns ALL errors, not just the first one
    print(e.errors())
    # [
    #     {"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"},
    #     {"loc": ["body", "password"], "msg": "Password too short", "type": "value_error"}
    # ]
```

This matches Pydantic's behavior and provides a better user experience - users can fix all issues at once instead of discovering them one at a time.

A request that fails these validators returns the same structure as `422`, one
entry per field. For nested serializers and list bodies, `loc` includes the
container path (`["body", "items", "1", "email"]`):

```json
{
  "detail": [
    {"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"},
    {"loc": ["body", "password"], "msg": "Password too short", "type": "value_error"}
  ]
}
```

### Understanding validation layers

Django-Bolt Serializer has **two validation layers**:

| Layer | Source | Raw msgspec | With `model_validate()` |
|-------|--------|-------------|-------------------------|
| **Meta constraints** | `Meta(min_length, pattern, ge, le, ...)` | Fail-fast | **Collects all errors** |
| **Custom validators** | `@field_validator`, `@model_validator` | N/A | **Collects all errors** |

**Meta constraints** use `msgspec.Meta` and are validated by msgspec's high-performance C code:

```python
from typing import Annotated
from msgspec import Meta

class UserSerializer(Serializer):
    name: Annotated[str, Meta(min_length=2, max_length=100)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    age: Annotated[int, Meta(ge=0, le=150)]
```

When using raw msgspec (`msgspec.convert()` or direct decoding), Meta constraints are fail-fast. However, when using `model_validate()` or `model_validate_json()`, **all Meta constraint errors are collected**.

**Custom validators** run after msgspec validation and also **collect all errors**:

```python
class UserSerializer(Serializer):
    # Meta constraints (validated by msgspec)
    name: Annotated[str, Meta(min_length=2)]
    email: str
    password: str

    # Custom validators
    @field_validator("email")
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value.lower()

    @field_validator("password")
    def validate_password(cls, value):
        if len(value) < 8:
            raise ValueError("Password too short")
        return value
```

**Validation order:**

1. **msgspec parses and validates** - Type checking and Meta constraints
2. **`@field_validator` runs** - Custom field validation
3. **`@model_validator` runs** - Cross-field validation
4. **All errors raised together** - Single `RequestValidationError` with all Meta and custom validator errors

For best performance, use Meta constraints for simple validations (length, range, pattern) and reserve `@field_validator` for complex logic or value transformations.

### Using msgspec.Meta for constraints

For declarative validation, use `Annotated` with `msgspec.Meta`:

```python
from typing import Annotated
from msgspec import Meta
from django_bolt.serializers import Serializer

class AuthorSerializer(Serializer):
    id: int
    name: Annotated[str, Meta(min_length=2)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
```

Meta constraints are enforced during deserialization via `msgspec.convert()`.

## Model-level validation

### The model_validator decorator

Use `@model_validator` for cross-field validation after all fields are set:

```python
from django_bolt.serializers import Serializer, model_validator

class PasswordSerializer(Serializer):
    password: str
    password_confirm: str

    @model_validator
    def check_passwords_match(self):
        if self.password != self.password_confirm:
            raise ValueError("Passwords don't match")
```

### Execution order

Field validators run first, then model validators:

```python
class TestSerializer(Serializer):
    value: str

    @field_validator("value")
    def field_val(cls, v):
        print("Field validator")
        return v

    @model_validator
    def model_val(self):
        print("Model validator")

TestSerializer(value="test")
# Prints: "Field validator" then "Model validator"
```

## Computed fields

### Basic computed fields

Use `@computed_field` to add derived values to output:

```python
from django_bolt.serializers import Serializer, computed_field

class UserSerializer(Serializer):
    first_name: str
    last_name: str

    @computed_field
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

user = UserSerializer(first_name="John", last_name="Doe")
result = user.dump()
# {'first_name': 'John', 'last_name': 'Doe', 'full_name': 'John Doe'}
```

### Computed fields with aliases

```python
@computed_field(alias="displayName")
def display_name(self) -> str:
    return f"{self.first_name} {self.last_name}".upper()
```

### Chaining computed fields

Computed fields can use other computed fields:

```python
class ProductSerializer(Serializer):
    price: float
    quantity: int

    @computed_field
    def total(self) -> float:
        return self.price * self.quantity

    @computed_field
    def formatted_total(self) -> str:
        return f"${self.total():.2f}"  # Call as method
```

## Dynamic field selection

One of the most powerful features: create different views from a single serializer.

### Using only()

Select specific fields:

```python
class UserSerializer(Serializer):
    id: int
    name: str
    email: str
    created_at: str

user = UserSerializer(id=1, name="John", email="john@example.com", created_at="2024-01-01")

# Get only id and name
result = UserSerializer.only("id", "name").dump(user)
# {'id': 1, 'name': 'John'}
```

### Using exclude()

Exclude specific fields:

```python
result = UserSerializer.exclude("password", "secret_key").dump(user)
```

### Using field_sets with use()

Define reusable field sets in Config:

```python
class UserSerializer(Serializer):
    id: int
    name: str
    email: str
    password: str
    created_at: str
    updated_at: str

    class Config:
        field_sets = {
            "list": ["id", "name", "email"],
            "detail": ["id", "name", "email", "created_at", "updated_at"],
            "minimal": ["id", "name"],
        }

# Use predefined field sets
list_result = UserSerializer.use("list").dump(user)
detail_result = UserSerializer.use("detail").dump(user)
```

### Chaining field selection

```python
view = UserSerializer.only("id", "name", "email").exclude("email")
result = view.dump(user)
# {'id': 1, 'name': 'John'}
```

### Field selection with computed fields

Computed fields work with field selection:

```python
class UserSerializer(Serializer):
    first_name: str
    last_name: str

    @computed_field
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

user = UserSerializer(first_name="John", last_name="Doe")

# Include computed field
result = UserSerializer.only("first_name", "full_name").dump(user)
# {'first_name': 'John', 'full_name': 'John Doe'}
```

## Creating type-safe serializer subsets

### The subset() method

Create actual subclasses with only specific fields:

```python
class UserSerializer(Serializer):
    id: int
    name: str
    email: str
    password: str

# Create a type-safe subset
UserMiniSerializer = UserSerializer.subset("id", "name")

# UserMiniSerializer is a proper class
user = UserMiniSerializer(id=1, name="John")
```

### The fields() method

Create subsets from field_sets:

```python
class UserSerializer(Serializer):
    id: int
    name: str
    email: str

    class Config:
        field_sets = {
            "list": ["id", "name"],
            "detail": ["id", "name", "email"],
        }

UserListSerializer = UserSerializer.fields("list")
UserDetailSerializer = UserSerializer.fields("detail")

# These are proper subclasses with type annotations
assert issubclass(UserListSerializer, Serializer)
```

### Converting from parent to subset

```python
UserMini = UserSerializer.subset("id", "name")

# Create full instance
full_user = UserSerializer(id=1, name="John", email="john@example.com", password="secret")

# Convert to mini
mini_user = UserMini.from_parent(full_user)
print(mini_user.dump())  # {'id': 1, 'name': 'John'}
```

## Write-only fields

Hide sensitive fields from output:

```python
class UserCreateSerializer(Serializer):
    email: str
    password: str

    class Config:
        write_only = {"password"}

user = UserCreateSerializer(email="test@example.com", password="secret123")
result = user.dump()
# {'email': 'test@example.com'}  # password excluded
```

## Nested serializers

### Basic nesting

```python
class AddressSerializer(Serializer):
    street: str
    city: str
    zip_code: str

class UserSerializer(Serializer):
    id: int
    name: str
    address: AddressSerializer

address = AddressSerializer(street="123 Main St", city="NYC", zip_code="10001")
user = UserSerializer(id=1, name="John", address=address)

result = user.dump()
# {
#     'id': 1,
#     'name': 'John',
#     'address': {'street': '123 Main St', 'city': 'NYC', 'zip_code': '10001'}
# }
```

### Lists of nested serializers

```python
class TagSerializer(Serializer):
    id: int
    name: str

class PostSerializer(Serializer):
    id: int
    title: str
    tags: list[TagSerializer]

tags = [TagSerializer(id=1, name="python"), TagSerializer(id=2, name="django")]
post = PostSerializer(id=1, title="Hello World", tags=tags)
```

### Nested serializer fields

Nested fields are inferred directly from the type annotation:

```python
from typing import Annotated
from django_bolt.serializers import Serializer, Nested

class AuthorSerializer(Serializer):
    id: int
    name: str
    email: str

class BlogPostSerializer(Serializer):
    id: int
    title: str
    author: AuthorSerializer
    tags: list[TagSerializer]
```

You only need `Nested(...)` when you want extra nested-field metadata, such as a custom list limit:

```python
class BlogPostSerializer(Serializer):
    id: int
    title: str
    tags: Annotated[list[TagSerializer], Nested(max_items=200)]
```

## Django model integration

### From model to serializer

Use `from_model()` to create serializer instances from Django models when you already have the data you need loaded:

```python
class ArticleSerializer(Serializer):
    id: int
    title: str
    content: str

# From a Django model without extra relation loading
article = Article.objects.get(id=1)
serializer = ArticleSerializer.from_model(article)
```

Use `afrom_model()` in async handlers when a serializer may need to read unloaded relations:

```python
article = await Article.objects.aget(id=1)
serializer = await ArticleSerializer.afrom_model(article)
```

Quick rule of thumb:

- `from_model()` is sync and non-querying
- `afrom_model()` is async-safe and may lazy-load missing relations
- in async handlers, default to `await afrom_model(...)` unless you intentionally want strict non-querying behavior

### Relation loading with from_model()

`from_model()` is non-querying. It only serializes relations that are already loaded on the model instance.

Use `select_related()` for single relations and `prefetch_related()` for reverse or many relations:

```python
# Sync from_model() expects loaded relations
post = await BlogPost.objects.select_related("author").prefetch_related("tags").aget(id=1)
serializer = BlogPostSerializer.from_model(post)
```

If a nested relation is unloaded:

- fields with defaults like `[]` or `None` keep their default value
- required nested fields raise `SerializationError` with a preload hint

For async handlers where relations may need to be loaded lazily, use `afrom_model()`:

```python
post = await BlogPost.objects.aget(id=1)
serializer = await BlogPostSerializer.afrom_model(post)
```

This also applies to reverse relations:

```python
class UserSerializer(Serializer):
    id: int
    addresses: list[AddressSerializer] = []

user = await User.objects.prefetch_related("addresses").aget(id=1)
serializer = UserSerializer.from_model(user)
```

### Bulk serialization

```python
# Serialize multiple instances
users = [
    UserSerializer(id=1, name="John"),
    UserSerializer(id=2, name="Jane"),
]

result = UserSerializer.only("id", "name").dump_many(users)
# [{'id': 1, 'name': 'John'}, {'id': 2, 'name': 'Jane'}]
```

## Serializer inheritance

Serializers support inheritance:

```python
class BaseUserSerializer(Serializer):
    username: str
    email: str

    @field_validator("email")
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value

class AdminSerializer(BaseUserSerializer):
    is_admin: bool = False

# Child inherits validators
admin = AdminSerializer(username="alice", email="alice@example.com", is_admin=True)
```

## Built-in type aliases

Django-Bolt provides pre-defined type aliases for common patterns:

```python
from django_bolt.serializers import (
    # String lengths
    Char50, Char100, Char255,

    # Validated strings
    Email, URL, Slug, UUID,

    # Integers
    PositiveInt, NonNegativeInt,

    # Network
    IPv4, IPv6, Port,

    # Auth
    Username, Password,

    # And more...
)

class UserSerializer(Serializer):
    username: Username      # max 150 chars, validated pattern
    email: Email           # validated email format
    website: URL | None    # validated URL
```

## Using with API handlers

```python
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer

api = BoltAPI()

class UserSerializer(Serializer):
    id: int
    username: str
    email: str

    class Config:
        field_sets = {
            "list": ["id", "username"],
            "detail": ["id", "username", "email"],
        }

UserListSerializer = UserSerializer.fields("list")
UserDetailSerializer = UserSerializer.fields("detail")

@api.get("/users")
async def list_users() -> list[UserListSerializer]:
    users = []
    async for user in User.objects.all()[:20]:
        users.append(await UserListSerializer.afrom_model(user))
    return users

@api.get("/users/{user_id}")
async def get_user(user_id: int) -> UserDetailSerializer:
    user = await User.objects.aget(id=user_id)
    return await UserDetailSerializer.afrom_model(user)
```

## Serializer vs raw msgspec.Struct

Django-Bolt's `Serializer` extends `msgspec.Struct` with important enhancements, particularly around error handling:

### Error handling differences

| Feature | `msgspec.Struct` | Django-Bolt `Serializer` |
|---------|------------------|--------------------------|
| Meta constraint errors | Fail-fast (first error only) | **Collects all errors** via `model_validate()`/`model_validate_json()` |
| Custom validators | Not supported | `@field_validator`, `@model_validator` with multi-error collection |
| Error format | `msgspec.ValidationError` (string) | `RequestValidationError` with structured `errors()` list |
| Direct instantiation | No validation | Runs custom validators (Meta constraints bypassed) |

### Multi-error collection with model_validate()

When using `model_validate()` or `model_validate_json()`, the Serializer collects **all** validation errors before raising:

```python
from typing import Annotated
from msgspec import Meta
from django_bolt.serializers import Serializer
from django_bolt.exceptions import RequestValidationError

class UserSerializer(Serializer):
    name: Annotated[str, Meta(min_length=2)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+$")]
    age: Annotated[int, Meta(ge=0, le=150)]

# All three fields are invalid
try:
    UserSerializer.model_validate({
        "name": "X",        # Too short
        "email": "invalid", # No @ symbol
        "age": 200          # Over 150
    })
except RequestValidationError as e:
    # Returns ALL errors, not just the first
    for err in e.errors():
        print(f"{err['loc']}: {err['msg']}")
    # ('body', 'name'): Expected `str` of length >= 2
    # ('body', 'email'): Expected `str` matching regex...
    # ('body', 'age'): Expected `int` <= 150
```

This is inspired by [Litestar's approach](https://litestar.dev/) to validation - validating each field individually when the initial validation fails, then collecting all errors.

### When errors are collected vs fail-fast

| Method | Behavior |
|--------|----------|
| `Serializer(field=value)` | Runs custom validators (multi-error), **bypasses** Meta constraints |
| `model_validate(dict)` | Collects all Meta + custom validator errors |
| `model_validate_json(json)` | Collects all Meta + custom validator errors |
| `msgspec.convert(data, Serializer)` | Fail-fast (raw msgspec behavior) |
| `msgspec.json.decode(json, type=Serializer)` | Fail-fast (raw msgspec behavior) |

**Recommendation**: Always use `model_validate()` or `model_validate_json()` for user input to get comprehensive error messages.

### Why direct instantiation bypasses Meta constraints

This is msgspec's design - Meta constraints (`min_length`, `pattern`, `ge`, etc.) are for **parsing external data**, not for constructing objects in Python:

```python
class User(Serializer):
    age: Annotated[int, Meta(ge=0)]

# Direct instantiation - Meta constraint NOT checked
user = User(age=-5)  # Works! No error raised

# Parsing - Meta constraint IS checked
User.model_validate({"age": -5})  # Raises RequestValidationError
```

Custom validators (`@field_validator`, `@model_validator`) always run, regardless of how the instance is created.

## Performance tips

1. **Use field selection for lists**: Only include fields you need in list views.

2. **Use select_related with from_model()**: Prevent N+1 queries when serializing relationships.

3. **Use subset() for type safety**: Creates actual classes that editors can type-check.

4. **Use write_only for sensitive data**: Password fields should never appear in output.

5. **Use Meta constraints for simple validation**: They're validated by msgspec's C code - much faster than Python validators.
