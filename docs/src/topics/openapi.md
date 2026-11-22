---
description: Generate an OpenAPI schema from your handlers, serve it with five docs UIs at /docs, protect or turn off the docs, and generate client code.
---

# OpenAPI

Django-Bolt makes an OpenAPI 3.1 schema from your routes, type hints and docstrings. `runbolt` serves the schema and five docs UIs at `/docs`. You do not write the schema by hand.

## Open the docs

Each `BoltAPI` has docs by default. Pass an `OpenAPIConfig` to set the title, the version and the description:

```python title="articles/api.py"
import msgspec

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig

openapi_config = OpenAPIConfig(
    title="Publishing API",
    version="1.0.0",
    description="Read and write articles.",
)
api = BoltAPI(openapi_config=openapi_config)


class Article(msgspec.Struct):
    id: int
    title: str
    published: bool


class ArticleIn(msgspec.Struct):
    title: str
    body: str


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> Article:
    """Get one article.

    Return the article with this ID, also when it is a draft.
    """
    return Article(id=article_id, title="Hello", published=True)


@api.post("/articles", status_code=201)
async def create_article(data: ArticleIn) -> Article:
    """Create a draft article."""
    return Article(id=1, title=data.title, published=False)
```

Start the server and open `http://localhost:8000/docs`:

```bash
python manage.py runbolt
```

| URL | Content |
| --- | --- |
| `/docs` | Swagger UI |
| `/docs/redoc` | ReDoc |
| `/docs/scalar` | Scalar |
| `/docs/rapidoc` | RapiDoc |
| `/docs/stoplight` | Stoplight Elements |
| `/docs/openapi.json` | The schema as JSON, with the media type `application/vnd.oai.openapi+json` |
| `/docs/openapi.yaml`, `/docs/openapi.yml` | The schema as YAML |

The schema has the routes, the parameters, the request bodies and the responses:

```bash
curl -s localhost:8000/docs/openapi.json | jq '.paths["/articles/{article_id}"].get | {tags, summary, description, operationId}'
```

```json
{
  "tags": [
    "Articles"
  ],
  "summary": "Get one article.",
  "description": "Return the article with this ID, also when it is a draft.",
  "operationId": "get_get_article"
}
```

`runbolt` adds the docs routes when it starts. `TestClient` does not serve them. The docs paths do not get the `prefix` of the `BoltAPI`.

When `runbolt` finds more than one `BoltAPI`, one schema covers the routes of all of them. It uses the `OpenAPIConfig` of the first API that it finds. That is the API of the `BOLT_API` setting, or else the API of the project package, or else the first app in `INSTALLED_APPS`.

## Configure the document

`OpenAPIConfig` is a dataclass. `title` and `version` are required.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `title` | `str` | required | The `info.title` of the document. |
| `version` | `str` | required | The `info.version` of the document. |
| `description` | `str \| None` | `None` | The `info.description`. |
| `summary` | `str \| None` | `None` | The `info.summary`. |
| `contact` | `Contact \| None` | `None` | The `info.contact`. |
| `license` | `License \| None` | `None` | The `info.license`. |
| `terms_of_service` | `str \| None` | `None` | A URL, the `info.termsOfService`. |
| `external_docs` | `ExternalDocumentation \| None` | `None` | A link to more documentation. |
| `servers` | `list[Server]` | `[Server(url="/")]` | The servers that the UIs send requests to. |
| `tags` | `list[Tag] \| None` | `None` | Tags with a description. See [Group operations with tags](#group-operations-with-tags). |
| `security` | `list[SecurityRequirement] \| None` | `None` | The security requirements of the whole document. |
| `components` | `Components` | `Components()` | Components to add, for example your own security schemes. |
| `webhooks` | `dict[str, PathItem \| Reference] \| None` | `None` | The webhooks of the document. |
| `use_handler_docstrings` | `bool` | `True` | Make the summary and the description from the docstring of the handler. |
| `path` | `str` | `"/docs"` | The base path of the docs routes. |
| `render_plugins` | `list[OpenAPIRenderPlugin]` | the five UIs | The docs UIs. See [Choose the docs UIs](#choose-the-docs-uis). |
| `exclude_paths` | `list[str]` | `["/admin", "/static"]` | Path prefixes to leave out of the schema. |
| `include_error_responses` | `bool` | `True` | Add the 422, 401, 403 and 302 responses that Bolt can send. |
| `strict` | `bool` | `False` | Raise an error for a schema that client generators cannot type. See [Check the schema in CI](#check-the-schema-in-ci). |
| `enabled` | `bool` | `True` | Serve the docs routes. |
| `auth` | `list[BaseAuthentication] \| None` | `None` | The authentication backends of the docs routes. |
| `guards` | `list[Guard] \| None` | `None` | The guards of the docs routes. |

Import `Contact`, `License`, `ExternalDocumentation`, `Server`, `Tag` and `Components` from `django_bolt.openapi.spec`:

```python title="articles/api.py"
from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.spec import Contact, License, Server

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Publishing API",
        version="2.1.0",
        contact=Contact(name="API team", email="api@example.com"),
        license=License(name="MIT", identifier="MIT"),
        servers=[Server(url="https://api.example.com", description="Production")],
    )
)
```

Without an `OpenAPIConfig`, the title is the `PROJECT_NAME` setting, or the `SITE_NAME` setting, or `"API"`. The version is `"1.0.0"`.

## Describe an operation

Bolt makes the summary and the description of an operation from the docstring of the handler. The first line is the summary. The other lines are the description. The `get_article` handler above shows this.

To set them yourself, pass `summary` and `description` to the route decorator. These arguments win over the docstring:

```python
@api.get("/articles", summary="List articles", description="Return the published articles, newest first.")
async def list_articles() -> list[Article]:
    return []
```

Set `use_handler_docstrings=False` in the `OpenAPIConfig` to keep docstrings out of the schema.

The `operationId` is the HTTP method and the function name: `get_list_articles`. When two routes get the same ID, Bolt adds `_2`, `_3` and so on to the later ones. Client generators use this ID as the name of a function.

### Group operations with tags

Without `tags`, an operation gets one tag from the module of its handler. A handler in `articles/api.py` gets the tag `Articles`.

Set `tags` on a route or on a `Router`. A `tags` value on a route replaces the tags of its router:

```python
from django_bolt import Router

router = Router(prefix="/comments", tags=["Comments"])


@router.get("/{comment_id}")
async def get_comment(comment_id: int) -> dict:
    return {"id": comment_id}


api.include_router(router)
```

To give a tag a description, add a `Tag` to `OpenAPIConfig(tags=...)`:

<!-- fragment -->
```python
OpenAPIConfig(
    title="Publishing API",
    version="1.0.0",
    tags=[Tag(name="Comments", description="Read and write comments.")],
)
```

### Describe parameters and fields

Pass `description` to `Query`, `Path`, `Header`, `Cookie` or `Body`. Constraints such as `ge` show in the schema too:

```python
from typing import Annotated

from django_bolt.params import Query


@api.get("/articles")
async def list_articles(
    page: Annotated[int, Query(description="The page number, from 1.", ge=1)] = 1,
) -> list[Article]:
    return []
```

```json
{"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1, "default": 1}, "description": "The page number, from 1.", "required": false, "deprecated": false, "allowEmptyValue": false, "allowReserved": false}
```

For a field of a `msgspec.Struct`, use `msgspec.Meta`. The docstring of the struct is the description of its schema:

```python
from typing import Annotated

import msgspec


class Article(msgspec.Struct):
    """A published article."""

    id: int
    title: Annotated[str, msgspec.Meta(description="The headline of the article.", max_length=200)]
```

```json
{"properties": {"id": {"type": "integer"}, "title": {"type": "string", "maxLength": 200, "description": "The headline of the article."}}, "type": "object", "required": ["id", "title"], "title": "Article", "description": "A published article."}
```

For a Bolt `Serializer`, use `field(description=...)`. See [Serializers](serializers.md).

## Document responses

The return annotation of the handler is the schema of the success response. `response_model` does the same and wins over the annotation. `status_code` sets the status of the success response. The default is 200.

A handler with no return annotation and no `response_model` gets the schema `{"type": "object"}`. Client generators cannot type such a response. Add an annotation.

### One schema for each status

Pass a `dict` to `response_model` to document more than one status:

```python
class Error(msgspec.Struct):
    detail: str


@api.get("/articles/{article_id}", response_model={200: Article, 404: Error})
async def get_article(article_id: int):
    ...


@api.delete("/articles/{article_id}", status_code=204, response_model={204: None})
async def delete_article(article_id: int):
    ...
```

- A key `...` (Ellipsis) gives the `default` response: each status that the dict does not list.
- A value `None` gives a response with no body.

### Responses that are not JSON

`response_class` sets the media type. A return annotation with one of these classes has the same effect:

| `response_class` or return type | Media type in the schema |
| --- | --- |
| `HTML` | `text/html` |
| `PlainText`, `str` | `text/plain` |
| `EventSourceResponse` | `text/event-stream` |
| `StreamingResponse`, `File`, `FileResponse`, `bytes` | `application/octet-stream` |
| `Redirect` | No body. The status is 307, or the `status_code` of the route. |

A return annotation with `JSON` documents `application/json` with an empty schema.
`Response` and a Django `HttpResponse` document `*/*` with an empty schema.
`JSON` can hold each JSON value, and `Response` can hold each media type. Thus their empty schema accepts each body.
To document the body, use `response_model=`, or add the body type to the annotation.
For example, `-> Item | JSON` documents the schema of `Item`, and `-> Item | HTML` adds `text/html`.
`Annotated` and a `type` alias do not change the result. `-> Annotated[Item | JSON, ...]` documents the same schema as `-> Annotated[Item, ...]`.

### Error responses

Bolt adds the error responses that it sends itself:

- **422** for a route with a path, query, header or cookie parameter, or a request body. The body is `{"detail": ...}`.
- **401** for a route with an authentication backend and a guard, when the first guard is not `AllowAny`.
- **403** for a route with `Requires`.
- **302** in place of the 401 for a route with `SessionAuthentication(login_url=...)`.

With `BoltAPI(error_formatter=...)`, the error bodies have the schema of the formatter. See [Error handling](error-handling.md).

Set `include_error_responses=False` in the `OpenAPIConfig` to document only the success responses.

## Mark a route as deprecated

Pass `deprecated=True`. The operation gets `"deprecated": true`, and the UIs show it as deprecated. The server still serves the route:

```python
@api.get("/v1/articles", deprecated=True)
async def list_articles_v1() -> list[Article]:
    return []
```

The route decorators of `BoltAPI` and `Router`, `@api.view()`, `@api.viewset()` and `@action` take `deprecated`. On `@api.viewset()`, it marks each route of the viewset, its actions included.

## Leave a route out of the schema

Pass `include_in_schema=False`. The server still serves the route:

```python
@api.get("/internal/stats", include_in_schema=False)
async def stats() -> dict:
    return {"articles": 2}
```

You can set `include_in_schema` at three levels. The most specific level that is not `None` wins:

1. A route decorator, or an `@action`.
2. `@api.view()` or `@api.viewset()`, or the `include_in_schema` attribute of the view class.
3. `BoltAPI(include_in_schema=...)`. A mounted API without a value takes the value of its parent. The root API uses `True`.

This hides each route of a mounted API, except one:

```python
ops_api = BoltAPI(include_in_schema=False)


@ops_api.get("/stats")
async def ops_stats() -> dict:
    return {}


@ops_api.get("/status", include_in_schema=True)
async def ops_status() -> dict:
    return {}


api.mount("/ops", ops_api)
```

`OpenAPIConfig(exclude_paths=[...])` leaves out each path under a prefix. A prefix matches whole path segments: `/admin` matches `/admin` and `/admin/users`, not `/administrators`. The default is `["/admin", "/static"]`. The docs path is always left out.

## Document authentication

A route with `auth` gets a `security` requirement, with one entry for each backend. A request needs one of them. Bolt adds each scheme to `components.securitySchemes`:

| Backend | Scheme name | Scheme |
| --- | --- | --- |
| `JWTAuthentication()` | `BearerAuth` | `http`, `bearer`, format `JWT` |
| `JWTAuthentication(cookie="...")` | `JWTCookieAuth` | `apiKey` in that cookie |
| `JWTAuthentication(header="...")`, not `Authorization` | `JWTHeaderAuth` | `apiKey` in that header |
| `APIKeyAuthentication()` | `ApiKeyAuth` | `apiKey` in the header of the backend, `x-api-key` by default |
| `SessionAuthentication()` | `SessionAuth` | `apiKey` in the cookie `SESSION_COOKIE_NAME` |

```python
from django_bolt.auth import IsAuthenticated, JWTAuthentication


@api.get("/drafts", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def drafts() -> list[Article]:
    return []
```

```bash
curl -s localhost:8000/docs/openapi.json | jq -c '.paths["/drafts"].get | {security, responses: (.responses | keys)}'
```

```json
{"security":[{"BearerAuth":[]}],"responses":["200","401"]}
```

Two backends of one kind that read different places get two schemes, for example `ApiKeyAuth` and `ApiKeyAuth2`. A scheme with one of these names in `OpenAPIConfig(components=...)` stays as you wrote it.

## Choose the docs UIs

`render_plugins` replaces the five default UIs. Each plugin takes a `path`, relative to `OpenAPIConfig.path`. The docs root (`/docs`) shows the plugin whose path is `"/"`, or else the first plugin:

```python
from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig, RedocRenderPlugin, ScalarRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Publishing API",
        version="1.0.0",
        path="/api-docs",
        render_plugins=[ScalarRenderPlugin(path="/"), RedocRenderPlugin()],
    )
)
```

This serves Scalar at `/api-docs` and ReDoc at `/api-docs/redoc`. The JSON and YAML schema routes are always there: `/api-docs/openapi.json`, `/api-docs/openapi.yaml` and `/api-docs/openapi.yml`.

The `path` column gives the default of the constructor. The default UIs of `OpenAPIConfig` pass their own paths, as the URL table above shows.

| Plugin | Default `path` | Other arguments |
| --- | --- | --- |
| `SwaggerRenderPlugin` | `"/swagger"` | `version="5.32.9"`, `js_url`, `css_url`, `standalone_preset_js_url` |
| `RedocRenderPlugin` | `"/redoc"` | `version="2.5.3"`, `js_url`, `google_fonts=True` |
| `ScalarRenderPlugin` | `["/scalar", "/"]` | `version="latest"`, `js_url`, `css_url`, `options` (a `dict` of Scalar settings) |
| `RapidocRenderPlugin` | `"/rapidoc"` | `version="9.3.4"`, `js_url` |
| `StoplightRenderPlugin` | `"/elements"` | `version="7.7.18"`, `js_url`, `css_url` |
| `JsonRenderPlugin` | `"/openapi.json"` | |
| `YamlRenderPlugin` | `["/openapi.yaml", "/openapi.yml"]` | |

`path` takes a string or a list of strings. Bolt adds the leading slash and removes the trailing slash. Each plugin also takes `favicon` (an HTML `<link>` tag) and `style` (an HTML `<style>` tag).

The UI pages load their JavaScript and CSS from jsDelivr or unpkg. Without access to these CDNs, or with a strict Content Security Policy, host the files yourself and pass `js_url` and `css_url`.

## Protect the docs

The docs routes take `auth` and `guards`, as each route does. Without them, the docs use `BOLT_AUTHENTICATION_CLASSES` and `BOLT_DEFAULT_PERMISSION_CLASSES`.

A browser does not send a token. For docs that people open in a browser, use `SessionAuthentication`. This example lets in the staff users who logged in to the Django admin:

```python
from django_bolt import BoltAPI
from django_bolt.auth import Requires, SessionAuthentication
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Publishing API",
        version="1.0.0",
        auth=[SessionAuthentication(login_url="/admin/login/")],
        guards=[Requires("is_staff", True)],
    )
)
```

- An anonymous user gets a 302 to `/admin/login/?next=/docs`. After the login, Django sends the user back to the docs.
- A logged-in user who is not staff gets 403.
- Without `login_url`, an anonymous user gets 401.

To let in each logged-in user, use `guards=[IsAuthenticated()]`. For docs that a program reads, use `auth=[JWTAuthentication()]` or an API key, with `guards=[IsAuthenticated()]`. A request with no valid token then gets 401. `auth` with no `guards` uses `BOLT_DEFAULT_PERMISSION_CLASSES`. With the default `[AllowAny()]`, the docs stay open.

## Turn off the docs in production

Set `enabled=False`. `runbolt` then adds no docs routes, and `/docs` gives 404:

```python
import os

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Publishing API",
        version="1.0.0",
        enabled=os.environ.get("ENABLE_API_DOCS") == "1",
    )
)
```

## Check the schema in CI

Client generators need a typed schema. `strict=True` makes schema generation raise `OpenAPIStrictError` for each of these problems:

- A route has a JSON success response with no type: no return annotation and no `response_model`.
- Two types have the same short name, so a component name falls back to `module.qualname`.
- The `error_formatter` of the API has no `error_schema`, and a route has a 422, 401 or 403 response.

Routes with `include_in_schema=False` and routes that do not send JSON are not problems.

With `strict=True` on the served config, `/docs/openapi.json` gives a 500 for a schema with problems. Run the check in a test instead. Generate the schema with `SchemaGenerator` and a strict copy of the config:

```python title="tests/test_openapi.py"
from dataclasses import replace

from articles.api import api, openapi_config
from django_bolt.openapi.schema_generator import SchemaGenerator


def test_schema_has_no_untyped_shapes():
    strict_config = replace(openapi_config, strict=True)
    SchemaGenerator(api, strict_config).generate()
```

For a route `GET /stats` with no return annotation, the test fails with this error. It lists each problem:

```text
django_bolt.openapi.schema_generator.OpenAPIStrictError: OpenAPI strict mode: routes with an untyped JSON response (add a return annotation or response_model): GET /stats
```

## Generate client code

Get the schema from a running server:

```bash
curl -o openapi.json http://localhost:8000/docs/openapi.json
```

Then give the file to a client generator. For example, for TypeScript and for Python:

```bash
npx @hey-api/openapi-ts -i openapi.json -o src/client
openapi-python-client generate --path openapi.json
```

The function names come from the `operationId` values, and the type names come from the component names. Run [the strict check](#check-the-schema-in-ci) so that each response has a type.

An API with a `QUERY` route gets an OpenAPI 3.2.0 document, because OpenAPI 3.1 has no `query` operation. Each other API gets 3.1.0. Some generators do not read 3.2.0 yet.

## See also

- [BoltAPI reference](../ref/api.md): the `openapi_config` and `include_in_schema` arguments, and the route decorators.
- [Routing](routing.md)
- [Responses](responses.md)
- [Error handling](error-handling.md)
- [Authentication](authentication.md)
- [Testing](testing.md)
