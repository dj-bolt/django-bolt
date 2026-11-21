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

# Exclude fields that hold msgspec.UNSET
data = user.dump(exclude_unset=True)
```

Use `exclude_unset` for a partial update. Give each optional field the default `msgspec.UNSET`. A field that the request body does not contain keeps that value, and `dump(exclude_unset=True)` leaves it out:

```python
import msgspec

class UserPatch(Serializer):
    username: str | msgspec.UnsetType = msgspec.UNSET
    email: str | None | msgspec.UnsetType = msgspec.UNSET

patch = UserPatch(email=None)
patch.dump(exclude_unset=True)
# {'email': None}
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

### Field aliases

`field(alias=...)` sets the name of a field in JSON. Python code uses the field name:

```python
from django_bolt.serializers import Serializer, field

class AccountSerializer(Serializer):
    user_name: str = field(alias="userName")
    age: int = 0

account = AccountSerializer.model_validate({"userName": "alice"})
print(account.user_name)  # alice
print(account.dump())     # {"userName": "alice", "age": 0}
```

The alias is the key for input and for output: in a request body, in `model_validate()`, in `dump()` and in a response.
The input does not accept the field name, as with `Field(alias=...)` in pydantic.
A field has one name in JSON. Thus the `by_alias` argument of `dump()` has no effect.
The alias wins over the `rename` option of the class.

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

A validator may also be declared as a `@classmethod` — the form Pydantic uses,
and the one that makes the `cls` parameter explicit to type checkers. Both
forms are accepted, in either decorator order:

```python
class UserSerializer(Serializer):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email")
        return value
```

Both forms behave identically at runtime.
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

### Validator modes

`@field_validator` and `@model_validator` take `mode="after"` (the default) or `mode="before"`.

| Validator | Gets | Returns |
|-----------|------|---------|
| `@field_validator(name, mode="before")` | The raw input value, before the type of the field converts it. | The value that the type of the field then validates. Bolt uses the result, also `None`. |
| `@field_validator(name)` | The value that the type of the field accepted. | The new value, or `None` to keep the value. |
| `@model_validator(mode="before")` | The raw input data, usually a `dict`. It is a classmethod. | The data to validate. |
| `@model_validator` | The built instance. | Nothing. |

```python
from django_bolt.serializers import Serializer, field_validator, model_validator

class OrderSerializer(Serializer):
    order_id: int
    tags: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def accept_old_key(cls, data):
        if isinstance(data, dict) and "id" in data:
            data = {**data, "order_id": data["id"]}
            del data["id"]
        return data

    @field_validator("tags", mode="before")
    @classmethod
    def split_tags(cls, value):
        return value.split(",") if isinstance(value, str) else value

order = OrderSerializer.model_validate({"id": 7, "tags": "a,b"})
# OrderSerializer(order_id=7, tags=['a', 'b'])
```

The order does not depend on the order of the definitions:

1. Model validators with `mode="before"`. The last one in the class runs first.
2. Field validators with `mode="before"`. The last one in the class runs first.
3. The type conversion and the constraints of msgspec.
4. Field validators with `mode="after"`, in the order of the class.
5. Model validators with `mode="after"`, in the order of the class.

After validators of a parent class run before those of a subclass. Before validators of a subclass run before those of a parent class. This is the order of pydantic.

A subclass can define a validator again under the same name. Then only the definition of the subclass runs, at the position of the parent definition. A validator with a new name adds to those of the parent.

Notes:

- Before validators get raw values for a JSON request body, `model_validate()` and `model_validate_json()`, also in a nested serializer. A field validator with `mode="before"` runs only if the input has the field. The keys of the raw data are the names in the input (the names after `rename=`).
- For an instance that Python code builds (`OrderSerializer(order_id=7)`), msgspec does no type conversion. The before validators then get the values of the fields.
- If a before validator raises `ValueError` or `TypeError`, Bolt reports the errors of the before validators and stops. The `loc` of an error has the full path, for example `["body", "lines", 0, "qty"]`.
- A serializer with a before validator, at any depth of a body type, decodes the body to builtins first and then converts it. This is slower than the direct decode. A serializer without one keeps the direct decode.
- `mode="wrap"` and `mode="plain"` are not supported. The decorator raises `ValueError`.

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

### Computed fields in the OpenAPI document

The response schema of a serializer has a property for each computed field. The property is required and has `readOnly: true`. Its type is the return type of the method. A request body schema has no computed field.

```python
class UserSerializer(Serializer):
    first_name: str
    last_name: str

    @computed_field(title="Full name", examples=["John Doe"])
    def full_name(self) -> str:
        """The first name and the last name."""
        return f"{self.first_name} {self.last_name}"

    @computed_field(include_in_schema=False)
    def debug_key(self) -> str:
        return f"user:{self.first_name}"
```

| Option | Effect on the property schema |
|--------|-------------------------------|
| `description` | Sets `description`. The default is the docstring of the method. |
| `title` | Sets `title`. |
| `examples` | Sets `examples`, a list of example values. |
| `deprecated=True` | Sets `deprecated: true`. |
| `include_in_schema=False` | Leaves the property out of the schema. `dump()` still returns the field. |

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

The subset keeps the field validators of its fields.
It keeps a model validator when it has each field that the validator names, such as `self.name`.
A model validator that names a field outside the subset does not run for the subset.
A model validator that names a method or a class variable of the parent does not run for the subset either.

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

## Field options for the OpenAPI document

`field()` takes options that describe a field in the OpenAPI schema of the serializer. They do not change validation or `dump()`.

```python
from django_bolt.serializers import Serializer, field

class ProductSerializer(Serializer):
    sku: str = field(description="Stock unit", title="SKU", examples=["AB-1"])
    old_code: str = field(default="", deprecated=True)
    trace_id: str = field(default="", include_in_schema=False)
```

| Option | Effect on the property schema |
|--------|-------------------------------|
| `description` | Sets `description`. |
| `title` | Sets `title`. |
| `examples` | Sets `examples`, a list of example values. |
| `deprecated=True` | Sets `deprecated: true`. |
| `include_in_schema=False` | Leaves the property out of the schema, also out of `required`. |

The options apply to the request body schema and to the response schema. A field with `include_in_schema=False` still validates input and is still in the output of `dump()`.

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

In the OpenAPI document, the response schema has no write-only field. The request body schema marks the field `writeOnly: true`. The same applies to a field with `field(exclude=True)`, without the mark.

## Read-only fields

A read-only field holds a value that the server sets, such as `id` or `created_at`. Mark it with `field(read_only=True)` or with `Config.read_only`. Give it a default.

```python
from django_bolt.serializers import Serializer, field

class UserSerializer(Serializer):
    id: int = field(read_only=True, default=0)
    name: str

# Input cannot set the field. The field gets its default.
user = UserSerializer.model_validate({"id": 99, "name": "alice"})
# UserSerializer(id=0, name='alice')

# Python code sets the field.
user = UserSerializer(id=5, name="alice")
user.dump()
# {'id': 5, 'name': 'alice'}
```

Input is a JSON request body, `model_validate()` and `model_validate_json()`. A serializer that is a `Query()`, `Header()`, `Cookie()` or `Form()` model is input too. The rule also applies to a nested serializer in that input. `from_model()` and direct construction set the field.

- Bolt ignores the input value. With `forbid_unknown_fields=True` on the serializer, the input value is an error, as an unknown field is.
- A read-only field must have a default. Without one, route registration and `model_validate()` raise `ImproperlyConfigured`. A serializer that is only a response needs no default.
- In the OpenAPI document, the request body schema has no read-only field. The response schema marks the field `readOnly: true`. If the API uses the serializer for a request body and for a response, the request body schema has the name `UserSerializerInput`.

`Config.read_only` and `field(read_only=True, default=...)` give the same result. With `forbid_unknown_fields=True`, each input value for the field is an error, also a value equal to the default.

There is one exception. A `Config.read_only` name can refer to a field of a base class, or to a field with a `msgspec.field()` default. With `forbid_unknown_fields=True`, such a field accepts an input value equal to its default. Declare the field in the class that has the `Config`, or use `field(read_only=True, default=...)`.

### Write-only fields in a response

A response gives the `dump()` of each Serializer at a position of the declared response type. The declared type is the return annotation or `response_model`. A position can be a field of a `Struct`, a dataclass or a `TypedDict`. It can also be an item of a list or a tuple, a value of a dict, or a member of a union:

```python
import msgspec


class LoginResult(msgspec.Struct):
    user: UserCreateSerializer
    token: str


@api.post("/login")
async def login(request) -> LoginResult:
    return LoginResult(user=user, token="...")
    # {"user": {"email": "test@example.com"}, "token": "..."}
```

`dump()` does the same for the fields of a Serializer. A Serializer in a Struct, dict or tuple field of a Serializer also gives its `dump()`.

A value with no declared type goes out as msgspec encodes it. msgspec encodes a Serializer with all of its fields, also the write-only fields. Thus declare the type, or call `dump()`:

```python
@api.post("/login")
async def login(request) -> dict:
    return {"user": user.dump(), "token": "..."}  # dump() hides the password
```

Bolt makes the plan of each response type once, when the route registers. A type with no Serializer gets no plan and costs nothing.

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
from django_bolt.serializers import Serializer

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

### Loading a QuerySet with from_models()

A serializer knows which relations it reads. Thus it can prepare the QuerySet.
`from_models()` makes a loading plan from the serializer fields. It applies the
plan to the QuerySet before it reads the rows:

- ForeignKey and OneToOne fields use `select_related`. Dotted sources such as `field(source="author.email")` also use `select_related`.
- ManyToMany and reverse relations use `prefetch_related`. A nested serializer adds its own plan to the `Prefetch`.
- `Config.annotations` uses `annotate`.
- Django 6.1 and later: `fetch_mode(FETCH_PEERS)` applies to all other fields.

```python
class PostSerializer(Serializer):
    class Config:
        annotations = {"comment_count": Count("comments")}

    id: int
    title: str
    author: AuthorSerializer          # select_related("author")
    tags: list[TagSerializer]         # prefetch_related("tags")
    comment_count: int                # annotate(...)

posts = PostSerializer.from_models(BlogPost.objects.filter(published=True))   # sync
posts = await PostSerializer.afrom_models(BlogPost.objects.all())            # async (async ORM iteration)
```

The plan only adds loads. Bolt keeps the `select_related`, `prefetch_related`
and `annotate` calls that you applied. Bolt does not apply them a second time.
If you give a list of model instances, Bolt uses the list as it is.
Use `loading_plan(Model)` to see the plan.

A route with the annotation `list[PostSerializer]` can return a QuerySet. This
also applies to `@paginate` routes. Bolt applies the plan to that QuerySet. You
do not write more code. There is no N+1.

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

### From serializer to model

`to_model()` makes an unsaved model instance, and `update_instance()` sets the fields of an existing one.
A `ForeignKey` or `OneToOneField` field can hold the primary key of the related row:

```python
class PostCreate(Serializer):
    title: str
    author: int  # The primary key of the author

post = PostCreate(title="Hello", author=3).to_model(BlogPost)
# post.author_id == 3
await post.asave()
```

Bolt writes the primary key to `author_id`. A model instance in the field goes to `author`, as before.
`from_model()` reads the primary key from `author_id`, with no query.

### Serializers from a model

`create_serializer()` makes a Serializer class from the fields of a model:

```python
from django_bolt.serializers import create_serializer

PostSerializer = create_serializer(BlogPost, fields=["id", "title", "author"])
```

A `ForeignKey` and a `OneToOneField` get the type `int`, the primary key.
A `DecimalField` gets the type `Decimal`. The JSON output is a string, such as `"19.99"`, so the value keeps each digit. The input can be a JSON string or a JSON number.

## Config options

The inner `Config` class of a serializer has these options:

| Option | Effect |
|--------|--------|
| `write_only` | A set of field names that `dump()` leaves out. |
| `read_only` | A set of field names that the server sets. |
| `field_sets` | Named lists of fields for `use()` and `fields()`. |
| `annotations` | Query annotations that `from_models()` applies to a QuerySet. |
| `model` | The Django model of the serializer. `create_serializer()` sets it. Bolt does not read it. |

`Config` has no `validators` option. A `Config` that sets `validators` raises `TypeError` when the class is created. Declare validators with `@field_validator` and `@model_validator`.

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
