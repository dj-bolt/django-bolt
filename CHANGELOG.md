# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed

- **`TestClient` reads the Django settings as `runbolt` does** - The test client had its own settings reader, and it disagreed with the server. Now one reader serves both. A test can see these changes:
  - A request body over `BOLT_MAX_UPLOAD_SIZE` gets 413. The default limit is 1 MB, not 10 MB. Set `BOLT_MAX_UPLOAD_SIZE` in a test that uploads more.
  - `BOLT_MAX_HEADER_SIZE` applies.
  - CORS uses the server defaults: the headers `Content-Type, Authorization`, and a preflight max-age of 3600 seconds. `CORS_ALLOWED_ORIGIN_REGEXES` applies. A `"*"` entry in `CORS_ALLOWED_ORIGINS` is not a wildcard; use `CORS_ALLOW_ALL_ORIGINS`.
  - `MEDIA_URL` is served, and `BOLT_STATIC_MAX_AGE` and `BOLT_MEDIA_MAX_AGE` add `Cache-Control`.
  - Each request comes from `127.0.0.1`, so `BOLT_TRUSTED_PROXIES` and `X-Forwarded-For` work as on a server.

  The `cors_allowed_origins`, `static_files_config` and `read_django_settings` arguments still work. An explicit `cors_allowed_origins` also gets the server defaults above, not the old test defaults (nine allowed headers and a max-age of 86400 seconds).
- **`TestClient` runs the request handler of `runbolt`** - The test client had its own copy of the request pipeline. Now each test request goes through the handler of the server, on a test worker thread. A sync handler no longer runs on the thread of the test, so its context variables and thread-locals stay out of the test. At exit, the client closes the database connections of the test workers.
- **macOS wheels are per architecture** - PyPI gets a macOS arm64 wheel and a macOS x86_64 wheel in place of one universal2 wheel. pip selects the wheel for the Mac, so an install does not change.

### Security

- **`set_cookie` quotes the cookie value as Django does** - Bolt wrote the value into `Set-Cookie` as it was. A value with `;` could add an attribute, for example `Domain` or `Max-Age`. Thus a value from a user could change the scope of the cookie. Now Bolt quotes a value as Django `set_cookie` does: a value with a character that is not safe gets double quotes, and each such character gets an escape. For example, `a b` gives `"a b"`, and `;` gives `\073`. A value of letters, digits and the characters `` !#$%&'*+-.^_`|~: `` does not change.

### Fixed

- **Cookies are read as Django reads them** - `request.cookies`, `Cookie()` parameters, the JWT cookie and the WebSocket scope now follow Django `parse_cookie`. A quoted value loses its quotes, and its escapes are decoded, so a cookie that Django or Bolt sets reads back unchanged. Spaces around the name and the value are removed. A pair with no `=` is a value with an empty name. When a name occurs two times, `request.cookies` and `Cookie()` use the last value. The JWT cookie is then not used, and the request is not authenticated by that backend: a sibling subdomain can add a cookie with the same name, and the order of the two does not show which one the site set.
- **A time or datetime with second 60 gets 422** - A leap second such as `23:59:60` became `23:59:59`, and Python logged a warning. Python cannot show a leap second, so Bolt now refuses the value.
- **A handler that hands on the request gets the body** - Bolt reads the source of a handler to find the request parts it uses, and skips the others. A handler can give the request to a helper, to `super().create(request)`, to another name or to a dict. The request can then be read where Bolt does not look. Such a handler with a request parameter and one more parameter got an empty body, query, headers and cookies. Now a handler that hands on the request gets every part. So does a handler that reads `request[key]` or `request.get(key)` with a key known only at run time.
- **TestClient keeps a response header value that is not ASCII** - TestClient gave an empty string for such a value, for example `X-Name: 日本`. A server sends the bytes of the value, and httpx decodes them. Now TestClient gives httpx the same bytes.
- **A preflight uses the CORS config of the route for the requested method** - With a global CORS config, a preflight to a path with no GET route used the global config, not the `@cors` config of the route. Thus the browser blocked the request. With two methods on one path, a preflight could also get the config of the wrong method. Now a preflight uses only the route of the method in `Access-Control-Request-Method`. When no route has that method, the global config applies: the config of another method grants nothing.
- **A CSRF form POST works when the handler does not read the body** - `CsrfViewMiddleware` reads the token from `request.POST`. A route behind Django middleware got no body when its handler did not read it, so a valid form POST got 403. Now a route behind Django middleware always gets the body.
- **The `/ready` database check uses the connection of the thread that runs it** - The check ran `connection.ensure_connection` on a pool thread, but bound it on the calling thread first. Thus it opened the connection of the event-loop thread from the pool thread, and that connection stayed open outside the pool. The check now looks up the connection on the pool thread. It also closes that connection after the check, unless a transaction is open. Before, `ensure_connection` did nothing for the connection of an earlier check, so `/ready` reported a healthy database after the database went away.
- **The CORS settings reference names the setting that Bolt reads** - The docs named `CORS_MAX_AGE`, but Bolt reads `CORS_PREFLIGHT_MAX_AGE`. The docs also gave wrong defaults for `CORS_ALLOW_HEADERS`, `CORS_ALLOW_METHODS` and the max-age.
- **Nested serializers work on Django 5.2** - On Django 5.2, a serializer with a relation field (a foreign key, a many-to-many field or a reverse relation) raised `NotImplementedError`. A route that returned such a serializer answered 500. Bolt called the deprecated `get_cache_name()`, and Django 5.2 relations raise in it. Bolt now reads `cache_name`, which each supported Django version has. The CI jobs for Django 5.2 and 6.0 did not find this: a build step reinstalled Django 6.1 before the tests ran.
- **`runbolt --dev` stops its worker on SIGTERM** - `kill` on the dev supervisor stopped only the supervisor. Its worker kept serving and held the port, so the next `runbolt --dev` failed with "address in use". The supervisor now stops the worker on SIGTERM and SIGHUP, as on Ctrl-C.
- **`runbolt --dev` reloads for a file in a new directory** - The watcher starts to watch a new directory after it gets the event for that directory. A file written before then, for example by `git checkout` or a code generator, sent no event, so no reload occurred. A directory moved into the project had the same problem. Now, when a directory is created or moved in, the watcher looks in it for a `.py` or `.html` file.

### Performance

- **A client's next async request reuses its lane** - An async request on a route with Django middleware gave its lane back only after the response left. Thus the next request of the same client often started a new lane, with a new thread and a new database connection. The lane now goes back to the idle list before the response leaves. The lane still closes the previous request before it runs the next one.

## [0.12.0]

### Added

- **`summary`, `description` and `include_in_schema` on `@api.websocket`** - These arguments work as on HTTP routes. `summary` and `description` replace the text from the docstring. `include_in_schema=False` keeps the route out of the OpenAPI schema. A route without it uses the value of its `BoltAPI`, also after `mount()`.
- **Free-threaded Python** - Django-Bolt now runs on the free-threaded build of CPython 3.14 (`python3.14t`, PEP 703). The Rust extension declares `gil_used = false`, so the interpreter keeps the GIL disabled when it imports `django_bolt`. CI tests the full suite on `3.14t` and publishes `cp314t` wheels next to the `abi3` wheels. The stable-ABI wheels still cover every GIL build from 3.12.
- **`runbolt --workers`** - Sets the Actix worker threads per process. The default is 1 with the GIL and the CPU count on a free-threaded interpreter. Each thread runs Python handlers, so a free-threaded process runs sync handlers in parallel. The startup banner shows the thread count and `free-threaded` when the GIL is off.
- **One asyncio loop per worker thread** - Each Actix worker thread owns its own `WorkerLoop`, pumped on that thread. An async handler resumes on the thread that accepted its request, so `threading.local` state survives an await and worker threads run async handlers in parallel. Before, every thread fed one shared loop, which serialized all awaits on one thread. Loop-bound asyncio objects cannot be shared between worker threads.
- **Dead database connections are dropped on error** - Bolt keeps Django connections open across requests. A handler, a QuerySet evaluation, or the load of `request.user` can raise on the executor pool. Bolt now runs Django's `close_if_unusable_or_obsolete()` on that thread when it does. A broken connection then does not fail every later request on the same thread. Before, a PostgreSQL server that closed its connections left the thread answering 500 `OperationalError: the connection is closed`. Only a process restart recovered it. With the default settings, the check runs on the error path only and adds no cost when a call succeeds. The request that meets the dead connection still fails, and the next one reconnects. When a database sets a positive `CONN_MAX_AGE` or `CONN_HEALTH_CHECKS`, the same check also runs before each executor call. That costs about 2 µs per open connection. Timed recycling and health checks then work without `BOLT_EMIT_SIGNALS`. A health check replaces a dead connection before the query, so no request fails. Neither setting is on by default. See [Database Connection Management](docs/src/topics/signals.md#database-connection-management).

- **`CurrentUser` and `OptionalCurrentUser` parameters** - `def me(user: CurrentUser)` gives the authenticated user, and answers 401 when the request has none. `OptionalCurrentUser` gives `None` then. In an async handler, both load the user with `await request.auser()`. In a sync handler, both read `request.user`, so the handler keeps its sync dispatch. For the type of your user model, make your own alias: `Annotated[User, Depends(require_current_user)]`.
- **Dev-mode report of a blocking `request.user` read** - A read in a helper or a template is not in the source of the handler, so it blocks the event loop for the query. With `DEBUG = True` or `runbolt --dev`, Bolt logs each such read that blocks the loop for more than 50 ms. The log names the file and the line, one time for each line.
- **`LaneAffinityError` names the route** - A sync read of `request.user` on a thread that cannot reach the request lane raises `LaneAffinityError`, a subclass of `RuntimeError` (see Changed). Its message names the route and the handler, with the file and the line. With `DEBUG = True` or `runbolt --dev`, Bolt also logs the fix one time for each route.

- **A `revoked_token_handler` can read the claims of the token** - A handler with two parameters gets the verified claims: `handler(jti, claims)`. A handler with one parameter still gets the `jti` only. Bolt reads the number of parameters one time, at registration. Use it to reject a token whose session ended. For example, a django-allauth access token names its session in `sid`, and allauth ends that session at logout. See [Session-bound tokens](docs/src/topics/authentication.md#session-bound-tokens-django-allauth).

- **WebSocket subprotocol negotiation** - `accept(subprotocol=...)` now puts the subprotocol in the `Sec-WebSocket-Protocol` header of the `101` response. Before, Bolt dropped it, and a browser that requested a subprotocol failed the connection with close code 1006. This blocked GraphQL subscriptions (`graphql-transport-ws`), versioned protocols, `mqtt` and `stomp`. `websocket.subprotocols` and `scope["subprotocols"]` give the subprotocols that the client requested. `accept()` raises `ValueError` for a subprotocol that the client did not request, as RFC 6455 requires. `accept(headers=[...])` adds headers to the `101` response. It raises `ValueError` for a header that the handshake sets, such as `Sec-WebSocket-Protocol`. See [Subprotocols](docs/src/topics/websocket.md#subprotocols).

### Removed

- **Django 4.2, 5.0, and 5.1** - All three reached end of life upstream (5.0 in April 2025, 5.1 in December 2025, 4.2 LTS in April 2026) and are no longer supported. The minimum is now Django 5.2 LTS, and the supported series are 5.2, 6.0, and 6.1. Projects on an end-of-life Django receive no upstream security fixes; pin `django-bolt<0.12` if you cannot upgrade Django yet.

### Changed

- **The WebSocket `101` response waits for `accept()`** - Bolt sent the `101` response before the handler started. It now sends it when the handler calls `accept()`. Auth, guards, origin checks and rate limits still run before the handler. A handler that calls `close()` or returns before `accept()` now refuses the handshake with HTTP `403`, as uvicorn and Daphne do. A handler that raises before `accept()` gets HTTP `500`. Before, the client got a `101` and then a close frame. The first `receive()` before `accept()` gives `websocket.connect`, as ASGI requires. A WebSocket app mounted with `mount_asgi()` gets the same handshake, so it can refuse a connection and select a subprotocol.
- **`WebSocketTestClient` waits for the handshake** - The `async with` now waits until the handler accepts or refuses the connection, as the server does. A `close()` or a return before `accept()` raises `HandshakeRejected` with `status_code` 403. An error before `accept()` raises that error. Before, the test continued and passed where the server refused the connection.
- **A send to a client that left raises `WebSocketDisconnect`** - The server raises `ConnectionResetError`, an `OSError`, as ASGI requires. `WebSocket.send_*()` converts it to `WebSocketDisconnect` with code 1006. Bolt no longer logs it as a handler error. Before, the send raised `RuntimeError: Failed to send text: Mailbox has closed`.
- **A WebSocket route has no module tag in OpenAPI** - Before, the schema gave each WebSocket operation a second tag from the module name of its handler. Now an operation without `tags` gets only the `WebSocket` tag. To keep a group by module, set `tags` on the route. See [#368](https://github.com/dj-bolt/django-bolt/issues/368).
- **Async ORM calls of plain async routes run on the ORM pool** - `await Model.objects.aget()`, `acount()`, `async for` over a QuerySet and `await request.auser()` are `sync_to_async(thread_sensitive=True)` calls. In a route without Django middleware, these calls ran on one asgiref thread per process. One query was in flight per process, whatever `--workers` or `DJANGO_BOLT_ORM_THREADS` said. On a free-threaded interpreter with one process, every such call in the server waited for that one thread. The same query gave 2.4k requests/s in an async handler and 24k in a sync handler. A 20 ms query gave 10 requests/s, with timeouts. No Bolt code ran on that thread. A dead database connection on it thus failed every later async ORM call until a restart. Bolt now installs its own executor for these calls. They run on the ORM pool, bounded by `DJANGO_BOLT_ORM_THREADS`. A broken connection is dropped on the error path, as on the other pools. Measured on the free-threaded build with one process and `DJANGO_BOLT_ORM_THREADS=12`: 2.4k to 9.9k requests/s. A 20 ms query goes from 10 to 3,000 requests/s with 64 threads. Set `DJANGO_BOLT_ORM_THREADS` to the number of queries you want in flight per process. Two calls of one request can run on different pool threads. A sync function cannot await, so a transaction cannot span two calls. A route that needs thread-local state across calls uses Django middleware. Such a route runs its calls on its request lane, as before. On SQLite the default pool has one thread. On the free-threaded build, 12 threads gave 4x the throughput of one on the example project. Measure your workload before you change it.
- **asgiref 3.12 or later is required** - A sync read of `request.user` on the event loop runs its loader on the lane, while the loop waits. asgiref 3.11 kept a parent loop on each `async_to_sync` wrapper, from when the wrapper was made or first called. A loader that used such a wrapper then sent its coroutine to the waiting loop, and the request never answered. asgiref 3.12 finds the parent loop at each call.
- **The CI test matrix is derived from the `pyproject.toml` classifiers** - The Django axis is now read from the `Framework :: Django :: X.Y` classifiers, as the Python axis already was. The declared support matrix and the versions actually tested can no longer disagree. A new `support-matrix` workflow checks those classifiers against [endoflife.date](https://endoflife.date) every week. It opens an issue when a series goes end of life, or when a new one ships. Run `python scripts/check_support_matrix.py` to check locally.
- **BREAKING: sync `request.user` raises for a backend with only an async `get_user`** - Bolt ran such a `get_user` with `asyncio.run` on a pool thread. A sync read of `request.user` now raises `RuntimeError`, and the message names the fix. To migrate, define `get_user_sync` on the backend, or read the user with `await request.auser()`.
- **`await request.auser()` awaits the user loader** - `auser()` returned the lazy `request.user`, so the first attribute read ran a blocking query on the event loop. It now awaits an async `get_user` directly. It runs `get_user_sync` through the hand-off of other framework queries: on the lane of the request when there is one, else on the ORM pool. `request.user` then returns the same user with no second query. `auser()` returns the user, not the lazy proxy, as in Django. Thus `await request.auser() is request.user` is now false. Concurrent `auser()` calls of one request share one query. An async `get_user` now runs in the context of the request, as in Django, so its `ContextVar` writes reach the handler.
- **BREAKING: sync `request.user` raises on a thread that cannot reach the request lane** - A request with Django middleware keeps its thread-local state on its lane, for example a tenant schema. A read of `request.user` on a thread from `asyncio.to_thread` ran the query on that thread, without that state. The query could thus read the wrong schema, and a handler on the lane then used that user too. Such a thread cannot wait for the lane, because the lane can wait for it. The read now raises `LaneAffinityError`, a subclass of `RuntimeError`. To migrate, read `request.user` on the thread of the request, or use `await request.auser()`. A request with no Django middleware is not affected.
- **`get_current_user` loads the user with `await request.auser()`** - It ran its own pk query on the asyncio default executor. That query ignored a custom `get_user` of the backend, did not run on the request lane, and did not fill `request.user`. Behind django-tenants, it thus read the public schema. It now uses the loader of the backend and the lane of the request, and `request.user` then returns the same user. It also returns a user that Django's session authentication loaded. It still returns `None` when the request has no authenticated user.
- **The user loader functions take two arguments** - `default_django_user_loader(user_id, auth_context)` and `load_user_sync(user_id, backend_name, auth_context)` lost the `is_async_context` argument. `resolve_user_loader()` returns a `(sync loader, async loader)` pair. A sync read of `request.user` on a thread with no event loop, for example a `sync_to_thread` worker, now runs the query on that thread.
- **BREAKING: the arguments of a parameter marker apply** - `Query()`, `Path()`, `Header()`, `Cookie()` and `Form()` ignored their arguments when the marker was the default of a parameter. So `name: str = Query(min_length=3)` gave the handler the `Param` marker when `name` was missing, and it accepted `?name=a`. `Query(default="all")` also gave the marker, not `"all"`. Bolt now uses the `default` of the marker as the default of the parameter. The constraint arguments (`gt`, `ge`, `lt`, `le`, `min_length`, `max_length`, `pattern`) apply as `msgspec.Meta`, in both forms. The OpenAPI schema shows `required`, the default, the constraints, `description`, `example` and `deprecated`. These changes can break an application (#365):
  - Before, `q: str = Query()` made `q` optional, and a request without `q` gave the handler the `Query()` object as its value. Now `q` is required, and a request without `q` gets a 422. To keep `q` optional, write `q: str = Query(default="")` or `q: str | None = Query()`.
  - A value outside the constraints of the marker gives a 422.
  - A constraint that does not fit the type raises `TypeError` when the route registers, for example `Query(ge=0)` on a `str`, `Query(pattern=...)` on an `int`, or `Query(ge=1)` on a `list[int]`. Before, Bolt ignored it. Remove the constraint, or change the type.
  - A default in an `Annotated` marker raises `TypeError` when the route registers, as in FastAPI. Write the default after `=`: `page: Annotated[int, Query()] = 1`. The first argument of a marker is its default, not its alias, so `Annotated[str, Form("tag")]` also raises. Write `Form()`, or `Form(alias="tag")` for another key.
  - The OpenAPI schema of such a parameter changes, so a client generated from it can change.
- **BREAKING: Bolt checks the type of a sequence or constrained parameter at registration** - These changes come with the sequence and `msgspec.Meta` parameter support (see Fixed). They can break an application:
  - A `msgspec.Meta` constraint that msgspec cannot apply to the type raises `TypeError` when the route registers. Examples are `Annotated[Decimal, Meta(ge=0)]` and `ge` on `int | str`. Before, Bolt ignored the constraint. Remove the constraint, or change the type.
  - A path, a header or a cookie gives one value. A type that cannot hold one item now raises `TypeError` when the route registers. Examples are `tuple[int, str]` and `Annotated[list[int], Meta(min_length=2)]` on a header. Before, the route registered. Use a type that holds one item.
  - For the key of a sequence query parameter, `request.query[key]` is now a list of each value. Before, it was the last value as a string. To read the last value, use `request.query[key][-1]`.

### Performance

- **Faster query parsing** - Bolt copied each query key and value into a new Rust string, and then into a Python string. The query map now borrows each key and value from the request. It copies only a value that it must decode. One pass decodes `+` and `%XX`, and an inline scan finds the values that need no decode. A query with four parameters parses in about 185 ns, down from 355 ns. A query with twelve parameters parses in about 560 ns, down from 1000 ns (measured with mimalloc, in a microbenchmark of `parse_query_string`).
- **Sync dependencies keep sync dispatch** - A handler with any `Depends` used an async argument injector, also when each dependency was sync. A sync handler then lost the sync fast path and, behind Django middleware, the dispatch of its complete request on one lane. When each dependency is sync, the injector is now sync too.
- **A dependency gets its binding at registration** - Each request looked up the binding of each dependency in a dict. The injector of the route now gets each binding one time, at registration, and the bindings of the nested dependencies too.
- **The lazy `request.user` costs less to build** - Bolt builds one for each request with a user ID. It takes about 220 nanoseconds, down from 360 (measured with `timeit`).
- **Request lanes for Django middleware routes** - A route with Django middleware runs each request on a lane. A lane is a thread that Rust owns, and it keeps its database connections open between requests. A sync handler behind `DjangoMiddlewareStack` runs the complete request on one lane with no asyncio. An async handler sends all sync work of its request to one lane, and all hooks of a phase run together. Lanes need no configuration. A lane that stays idle for 10 seconds stops (`DJANGO_BOLT_LANE_IDLE_SECONDS`). Measured in one process with no network, an async request with four middleware takes 514 microseconds, down from 707. With django-tenants on PostgreSQL, one process, and 64 connections, a sync ORM route serves 1066 requests per second. django-ninja serves 795 on gunicorn with 32 threads and persistent connections. An async ORM route serves 914 against 310 for django-ninja on uvicorn.
- **Arguments bound from the Rust request maps** - A route whose parameters are all path, query, header or cookie values, and that reads nothing else from the request, now gets its arguments straight from the Rust maps. Rust builds no Python `headers`, `cookies`, `query` or `params` dict for such a route. Measured with `runbolt`, one process and 64 connections, a route with three typed headers serves 62,000 requests per second, up from 54,000. A route with one typed cookie serves 64,000, up from 59,000. A route with a `request` parameter, a dependency or Python middleware keeps the dicts. The merge of coerced values into a request dict is now two linear passes instead of a scan per value.
- **Faster `uuid.UUID` parameters** - Rust built each typed UUID value with `uuid.UUID(int=...)`, which runs the Python `__init__`. Rust now builds the object as CPython 3.14 `UUID._from_int` does. The value, hash, pickling and immutability are the same. A UUID path, query, header or cookie value takes about 89 ns instead of 300 ns.

### Fixed

- **A WebSocket route shows once in OpenAPI renderers** - `@api.websocket` now accepts `tags`, as HTTP routes do. Without `tags`, the operation gets only the `WebSocket` tag. Before, it also got a tag from the module name, so renderers showed the route in two groups. Explicit tags replace the default `WebSocket` tag. An empty list gives no tags, as on an HTTP route. ([#368](https://github.com/dj-bolt/django-bolt/issues/368))
- **A query string decodes as Django `QueryDict` decodes it** - Browsers and HTML forms encode a space in a query as `+`. Bolt kept the `+`, so `?q=hello+world` gave `"hello+world"`. A test client call with `params={"q": "x y"}` gave `"x+y"` too. Bolt now decodes `+` as a space in query keys and values, before the percent-decode. Thus `%2B` still gives a literal `+`. Three other rules now match Django too. Bytes that are not UTF-8 become U+FFFD, not the raw text, so `?q=%FF` gives `"\ufffd"`. A key with an empty name stays, for example `?=v`. A WebSocket route gives an empty value to a key with no `=`, for example `?flag`, as an HTTP route does. The rules apply to HTTP routes, to WebSocket routes, and to the test clients. A `+` in a path parameter stays a `+`, as in Django. A client that sends a literal `+` in a query must now encode it as `%2B`. For example, `?n=+5` now gives `" 5"`, so an `int` parameter answers 422.
- **`META["QUERY_STRING"]`, `get_full_path()` and `build_absolute_uri()` keep the query as the client sent it** - Bolt built them again from the decoded query values. A decoded `&` or `=` then made a new parameter: `?q=a%26b%3Dc` gave the `QUERY_STRING` `q=a&b=c`. Django middleware parses `QUERY_STRING` again, so it got `q=a` and `b=c`. A `%2B` became a space for Django, and `get_full_path()` held an unencoded space for a `%20`. Bolt now keeps the query string as it arrived, as Django does. Also, a route with Django or Python middleware now parses the query when its handler does not read it. Thus the middleware gets `request.GET`, `QUERY_STRING` and `get_full_path()` with the query, for example for a login redirect.
- **Each JWT backend of a route checks its own tokens** - The scheme name of each JWT backend is `"jwt"`. Bolt found the `revoked_token_handler` and the user loader of a backend by that name. On a route with two `JWTAuthentication` backends, a token that backend A accepted was checked by the handler of backend B. The user loader of backend A loaded the user of a backend B token. This applied to HTTP routes and to the WebSocket handshake. Rust now records the position of the backend that accepted the credential. Bolt uses that position to find the handler and the loader. `request.context["auth_backend"]` does not change. The new key `auth_backend_index` gives the position of the backend in the `auth` list.
- **`JWTAuthentication` rejects the refresh and ID tokens of other issuers** - A route with no `token_type` rejected a token with `typ: "refresh"` only. Other issuers put the token type in other claims: `token_type` (djangorestframework-simplejwt) and `token_use` (django-allauth headless, AWS Cognito). When such an issuer signs with the same key, its refresh token authenticated each Bolt route as an access token. For example, allauth signs with `SECRET_KEY` for `HS256`, and so does `JWTAuthentication` by default. A route with no `token_type` now rejects a token when any of these claims names a refresh or ID token: `refresh`, `offline` or `id`, in any case. Keycloak writes `Refresh`, `Offline` and `ID`, and AWS Cognito ID tokens have `token_use: "id"`. An OIDC ID token with no type claim still passes; set `audience` to reject it. A route with a `token_type` also rejects a token when one of these claims has a different type.
- **A typed value that Python cannot build no longer aborts the worker** - Rust parses a `date` with year 0 or 10000, and a `Decimal` with an exponent above 10^18. Python `datetime` and `decimal` reject them, and the Python error was unwrapped under the GIL. The process ended with SIGABRT and the client got no response. This applied to path and query values before, and to headers and cookies with this release. Rust now rejects such values before the GIL with a 422 that names the source, for example `Query parameter 'day': Invalid date '0000-01-01': year 0 is out of range (1 to 9999)`. The Python construction is fallible as well, so a Python error is returned, never unwrapped.
- **Typed `Header()` and `Cookie()` parameters are converted and validated** - Rust converted typed path and query values only. A handler with `x_count: Annotated[int, Header()]` got the string `"5"`, and `X-Count: abc` reached the handler. A bad typed cookie became `0` or `False`, and a typed query parameter named `accept` also changed the `Accept` header. Rust now converts `int`, `float`, `bool`, `uuid.UUID`, `decimal.Decimal`, `datetime`, `date` and `time` headers and cookies before the handler runs. A bad value gives a 422 that names the header or the cookie, for example `Header 'x-count': Invalid integer 'abc'`. A WebSocket upgrade with a bad value gets a 400. Header and cookie types have their own maps, keyed by wire name, so a header and a query parameter cannot clash. The converted value also replaces the string in `request.headers` and `request.cookies`, as for `request.query`. Django middleware still gets the strings as they arrived: `request.META` and `HttpRequest.COOKIES` hold the original text, so `0005` keeps its leading zeros there. The request dicts are also cheaper to build. For 16 headers and 3 cookies, the handoff to Python takes about 900 ns instead of 1200 ns. A route with an int path parameter and two int query parameters takes about 200 ns instead of 300 ns.
- **Typed path, query and form parameters with an alias are converted** - Rust looked up the type of a path, query or form value by the Python name of the parameter. The value arrives by its alias, so it stayed a string. Thus `page: Annotated[int, Query(alias="p")]` received `"3"` for `?p=3` and accepted `?p=abc`. The same applied to `Path(alias=...)`, `Form(alias=...)`, and to WebSocket path and query parameters. When the name of one parameter was the alias of another, Rust checked the value against the wrong type. Rust now uses the alias, so the handler gets `3` and `?p=abc` gets a 422.
- **A bad typed WebSocket path or query value rejects the upgrade** - The WebSocket scope builders gave the raw string to the handler when a typed path or query value did not convert. Thus `limit: Annotated[int, Query()]` received `'abc'` for `?limit=abc`. A bad typed header or cookie already rejected the upgrade. Now a bad path or query value also rejects it. The server answers 400, and the body names the parameter by its wire name, for example `Query parameter 'limit': Invalid integer 'abc'`. `WebSocketTestClient` raises `ValueError` with the same text. A path or query value that is too long now also names its parameter, for example `Path parameter 'id': Parameter too long`. Bolt checks only the last value of a repeated query key, as in HTTP. Thus `?limit=abc&limit=5` still gives `5`.
- **WebSocket path values are decoded** - Bolt did not decode the path values of a WebSocket route. Thus `/ws/rooms/hello%20world` gave `hello%20world`, and a typed value such as `%34%32` did not convert. Bolt now decodes them with the same code as for HTTP, so the handler gets `hello world` and `42`.
- **Two types for one wire key fail at registration** - Two parameters can read the same key, for example `p: str` and `page: Annotated[int, Query(alias="p")]`. Rust converts a value one time, so one of them got the wrong type. A dependency parameter counts too. Registration now raises `TypeError` when such parameters have different types. Parameters with the same type can share a key. Path and query parameters share one set of keys. Headers, cookies and form fields each have their own.
- **The 422 body for a bad path or query parameter is valid JSON** - The error message repeats the value that the client sent. Bolt escaped only quotes and backslashes in it. A value with a control character, for example `?n=%0A` or `?n=%09` for an `int` parameter, then gave a body that JSON clients could not parse. Bolt now escapes all control characters (U+0000 to U+001F), for example as `\n` or `\u0000`. The 422 body is also faster to build than before.
- **The test clients send the encoded path** - `TestClient` and `AsyncTestClient` sent the decoded path to Rust. A request such as `client.get("/int/%0A")` then stopped with a Rust panic (`InvalidUri`), and the test clients could not test encoded path parameters. The test clients now send the path as a real server receives it. Bolt decodes the path parameters with the same code as the server, so `/items/hello%20world` gives `hello world`. The Rust test backend raises `ValueError` for a URI that is not valid.
- **The parameters of a dependency bind like the parameters of a handler** - A dependency used its own copy of the parameter binding, which had these bugs:
  - A `Header()` parameter such as `x_api_version` looked up `x_api_version`, not the `x-api-version` header. So it never found the header.
  - A default value was ignored. A missing header or cookie raised `ValueError`, which became a 500, and a missing query value became `None`.
  - Query and path values were not converted to their types, so `page: int` got a string.
  - A `Body()`, `Form()` or `File()` parameter got `None`. So did a nested dependency (`def outer(values=Depends(inner))`).
  - The OpenAPI schema did not show the parameters or the body of a dependency.
  A dependency now uses the same extractors as a handler. It gets the same names, aliases, defaults, type conversion and 422 errors. Bolt compiles the binding of a dependency one time for each route, so one dependency reads `item_id` from the path on `/items/{item_id}` and from the query on `/items`. Its form fields, file constraints and upload cleanup work as for a handler. A handler and a dependency that take the same body type share one decode of the body. Other body types decode separately. The OpenAPI schema of the route shows its parameters and its body. A handler parameter and a dependency parameter with the same name follow the wire key rule above: the same type shares one key, another type fails at registration, and another source is another key. A sync handler uses the sync injector only when no dependency in its tree is async. A nested dependency counts in its sync form, so `def org(user=Depends(get_current_user))` keeps a sync handler on the sync injector. The dependencies of a WebSocket handler bind the same way. Their query values, headers and cookies also have their types.
- **A revoked token fails the WebSocket handshake** - A `revoked_token_handler` or a `revocation_store` applied only to HTTP routes. A WebSocket route with the same backend accepted a revoked token. The handshake now awaits the revocation check before the upgrade, and a revoked token gets `401`. `WebSocketTestClient` runs the same check. An open connection stays open after its token is revoked. A denied handshake in `WebSocketTestClient` now destroys its test app. Before, each denial kept its test app until the process ended.
- **`CurrentUser` in a sync handler gives `None` for a user ID with no row** - The sync form of `get_current_user` read `is_authenticated` on the lazy user before it checked for `None`. A token with the ID of a deleted user then answered 500. It now gives `None`, and `CurrentUser` answers 401, as in an async handler.
- **A user loader can use `async_to_sync` while the loop waits for it** - A sync read of `request.user` on the event loop of a request with Django middleware waits for the request lane. An `async_to_sync` in `get_user_sync` scheduled its coroutine on that waiting loop, and the request never answered. The loader now runs with no asgiref parent loop, so `async_to_sync` runs its coroutine on its own loop.
- **The OpenAPI schema names a header as Bolt reads it** - A header parameter `x_request_id` showed as `x_request_id`. Bolt reads the `x-request-id` header, and the schema now shows that name.
- **The test clients close the database connections of their lanes at exit** - A lane keeps its connection open for 10 seconds after a test. With `@pytest.mark.django_db(transaction=True)` on PostgreSQL, pytest-django then could not drop the test database ("database is being accessed by other users"). `TestClient` and `AsyncTestClient` now stop their idle lanes at exit.
- **Django middleware state stays with its request** - Middleware that keeps request state in `threading.local`, such as the tenant schema of django-tenants, leaked that state between concurrent requests. Bolt now gives each request with Django middleware one thread for its sync work, as Django's ASGI handler does. The middleware, sync handlers, `sync_to_thread`, QuerySet evaluation, and Django's async ORM calls of one request all use that thread.
- **`DjangoMiddleware` runs a `MiddlewareMixin` subclass that has its own sync `__call__`** - `MiddlewareMixin` marks each instance as async, so Bolt awaited the response of the sync `__call__` and the request failed. Bolt now runs such a middleware on the thread of the request.
- **Sync handlers behind Django middleware run on the thread of the request** - A sync handler with no ORM call ran inline on the event loop thread. It then read thread-local state that the middleware did not set. Such a handler now runs on the lane of its request. This applies to Django middleware on the API, on a router, and on the route.
- **A raw database cursor marks a sync handler as blocking** - The handler analysis found `Model.objects` calls only. A sync handler that used `connection.cursor()` or `connections["alias"].cursor()` ran on the event loop, and Django raised `SynchronousOnlyOperation`. Such a handler now runs in a worker thread.
- **Custom middleware attributes reach `request.state`** - Bolt copied only a fixed list of attributes from the Django request. Bolt now copies each public attribute that a middleware sets, for example `request.tenant` from django-tenants, to `request.state`. An async handler can read `request.state["tenant"]` on any thread. The copy costs about 0.5 microseconds per request.
- **`DjangoMiddleware` copies attributes before the handler runs** - The single wrapper copied the session, the user, and other attributes after the handler returned. The handler thus could not read them. The copy now runs before the handler, as in `DjangoMiddlewareStack`.
- **Async-only middleware runs on the event loop** - `DjangoMiddleware` and `DjangoMiddlewareStack` ran a middleware with `sync_capable = False` on a thread with no event loop, and the request failed. Both now read `sync_capable` when they select the call path. They await the response of such a middleware on the event loop and accept any awaitable, a Future included. In a stack, such a middleware gets an async `get_response`, as in Django. The hooks before it and the handler after it still run on the lane of the request.
- **A sync `request.user` read on the event loop runs on the request lane** - A request with Django middleware keeps its thread-local state on its lane, for example a tenant schema. A sync read of `request.user` on the event loop sent the query to the ORM pool, where that state is not set. The query could thus read the wrong schema, and a handler on the lane then used that user too. The query now runs on the lane, and the event loop waits for it. This applies to an async handler, an async Python middleware, and a template that reads `{{ user }}`. It also applies to a coroutine that `async_to_sync` runs. The lane can wait for the event loop in `async_to_sync`. The query then goes through the `CurrentThreadExecutor` of the lane, as asgiref sends thread-sensitive work, so the two do not deadlock. `await request.auser()` does not block the event loop and stays the faster form in async code.
- **`request.user` loads on the thread that reads it** - Bolt chose the thread of the user query at registration, from the type of the handler. A trivially-async handler read the user on the sync fast path, where Rust installs an event loop, and Django raised `SynchronousOnlyOperation`. The query now runs on the thread that reads the user, also on a thread with a running loop: the loop waits in any case, and a hop to the ORM pool only added two thread wakeups (measured: +40% throughput on SQLite, +14% on PostgreSQL for a handler that reads `request.user`). The loader hides the running loop from Django's `async_unsafe` guard during the query. A lane keeps its thread-local state. On the event loop of a request with Django middleware, the query runs on the request lane (see above). This now includes the session user of `AuthenticationMiddleware`, which raised `SynchronousOnlyOperation` in an async handler. Bolt never loads the user before a handler: a sync read loads it when it occurs, and `await request.auser()` loads it on the ORM pool without blocking the loop. Measured with one process per core, the lazy read serves more requests per second than a load before the handler, at half the CPU per request.
- **`await request.auser()` returns the session user behind chained `DjangoMiddleware` wrappers** - Each wrapper copies the attributes of the Django request. A wrapper after `AuthenticationMiddleware` replaced Django's async `auser` with the lazy session user. An attribute read of that user in async code then raised `SynchronousOnlyOperation`. Each copy now keeps Django's `auser` for the user that an earlier copy took.
- **A sync-only middleware in `DjangoMiddleware` gets a response from `get_response`** - Bolt gave each middleware an async `get_response`. In an async request, a middleware that is not async-capable got a coroutine, and the request failed. Such a middleware now gets the response, as Django gives it a sync `get_response`.
- **`async_to_sync` on an ORM pool worker does not deadlock** - `async_to_sync` starts its event loop on a new thread. An ORM hand-off from that loop waited for the ORM pool, but the worker that waited for `async_to_sync` held the only slot. This occurred with a `get_user_sync` that wraps an async `get_user` with `async_to_sync`, when that `get_user` calls `run_in_orm_executor`. Such a hand-off now uses the default pool.
- **The repr of an unloaded `request.user` hides the token claims** - `repr(request.user)` showed the arguments of the user loader, with each JWT claim. A debug page or a log could show it. It now shows the user ID only, and it does not load the user.
- **Sync dependencies of a blocking sync handler run on the thread of the handler** - Bolt resolved them on the event loop, and then ran the handler in a worker thread. A sync dependency that used the ORM raised `SynchronousOnlyOperation`. Behind Django middleware, it also missed the thread-local state of the lane, for example the tenant schema. The dependencies now run in the same thread hop as the handler. This also applies to `CurrentUser` in a sync handler, which read the session user of Django on the event loop.
- **Bolt rejects a middleware with no capability** - `DjangoMiddleware` and `DjangoMiddlewareStack` now raise `RuntimeError` for a middleware with `sync_capable = False` and `async_capable = False`, as Django does at load.
- **The OpenAPI schema gives the format of each date, time, UUID and decimal value** - A parameter or a form field of type `datetime`, `date`, `time`, `timedelta`, `uuid.UUID` or `Decimal` showed as `{"type": "object"}`. This applied to path, query, header and cookie parameters, `Form()` fields, and the fields of a form struct. A JSON body struct had the same problem for `timedelta` and `Decimal` fields. Code generators then made an untyped value, for example `dynamic` in Dart. Each such value now shows as a string with its format: `date-time`, `date`, `time`, `duration`, `uuid` or `decimal`. (#337)
- **A sequence parameter takes each value of its repeated key** - A `list[int]`, `set[int]` or `tuple[int, ...]` query parameter got the last value of its key as a string, so `?tag=1&tag=2` gave `"2"`. A form field got a list of strings, and a set field got a list. Rust now collects each value of a repeated query key, and Bolt converts the values to the declared sequence and item type. A bare `list`, `set`, `frozenset` or `tuple` works too, and keeps each item as a string. A bad item gives a 422, for example `?tag=x` for a `list[int]`. A header or a cookie gives a sequence of one item. `request.GET.getlist()` gives each value to Django middleware. The length limit applies to each value. See Changed for the parts that can break an application.
- **A `NewType` or a `type` alias parameter converts as the type it names** - `user_id: UserId` with `UserId = NewType("UserId", int)`, or a `type Page = int` alias, stayed a string, and `?user_id=x` reached the handler. Rust now converts and validates it as an `int`. A parameterized alias gets its arguments, so `Pair[int]` with `type Pair[T] = list[T]` converts as `list[int]`.
- **`msgspec.Meta` constraints of a parameter apply** - Bolt removed `msgspec.Meta` from a parameter annotation with its `Query()` or `Header()` marker. So `page: Annotated[int, Meta(ge=1)]` accepted `?page=0`, and `PositiveInt` did not check its value. The constraints now apply to path, query, header, cookie and form parameters, and a value outside them gives a 422 that names the parameter. On `int | None`, they apply to the `int` value, and the parameter stays optional. A constraint that msgspec cannot apply to the type now fails at registration (see Changed).
- **The OpenAPI schema documents sequence, NewType and constrained values** - A `set`, `frozenset` or `tuple` parameter, form field or JSON body field showed as `{"type": "object"}`. It is now an array, also for a bare `list`, `set`, `frozenset` or `tuple`. A set shows `uniqueItems: true`, and a `tuple[A, B]` shows one schema for each position with `prefixItems`. A path, header or cookie array shows `minItems: 1` and `maxItems: 1`, because Bolt gives it one value. A `NewType` or a `type` alias shows the type it names. The `msgspec.Meta` constraints of a parameter show, and a `Meta(min_length, max_length)` on a list, a set or a tuple shows as `minItems` and `maxItems`.
- **The OpenAPI schema documents dataclass, TypedDict, NamedTuple, Any and None values** - A dataclass or a `TypedDict` field now shows as an object component, and a `NamedTuple` as an array component, as msgspec encodes them. `Any` and `msgspec.Raw` show as `{}` (any value), not `{"type": "object"}`, in a field and as a return type. `None` shows as `{"type": "null"}` in both places, and `bytearray` and `memoryview` show as `bytes` does.
- **A WebSocket sequence parameter takes each value of its repeated key** - A WebSocket route kept the last value of a repeated query key, as a string. Thus `tag: Annotated[list[int], Query()]` gave `"3"` for `?tag=1&tag=2&tag=3`. The production upgrade and `WebSocketTestClient` now collect each value of a sequence key, as an HTTP route does. The handler gets `[1, 2, 3]`. (#364)

### Documentation

- **PostgreSQL on macOS with forked workers** - In local development on macOS, a forked `runbolt` worker can hang when libpq opens a PostgreSQL connection. When libpq is built with GSSAPI support, its GSSAPI negotiation calls the macOS Kerberos framework, which is not safe after `fork()`. The FAQ and `CONTRIBUTING.md` now show the fix: set `PGGSSENCMODE=disable`, or `"gssencmode": "disable"` in `DATABASES[...]["OPTIONS"]`. Linux does not need it. See [the FAQ](docs/src/faq.md#why-does-runbolt-hang-on-macos-when-it-connects-to-postgresql).

## [0.11.0]

### Removed

- **`Nested()` and the nested list item cap** - `Nested(max_items=...)`, `NestedConfig`, and the default cap of 1000 items on nested lists are gone, on input and on `from_model()` output. The type hint is the only nesting API: `author: AuthorSerializer` or `tags: list[TagSerializer]`. The request body size limit bounds input size.

### Performance

- **`TCP_NODELAY` on every connection** - Bolt left Nagle's algorithm on. A response that Bolt writes in more than one segment waited for the delayed ACK of the client, about 40 ms per response. This hit every `StreamingResponse`, `EventSourceResponse`, and ASGI mount, `mount_django` included. On one keep-alive connection, an SSE route with three events went from 24 to 10,700 responses per second. A trivial ASGI mount went from 24 to 6,700. Single-write JSON responses did not change.

### Fixed

- **`JWTAuthentication` reads `SECRET_KEY` lazily** - The backend captured `settings.SECRET_KEY` in `__init__`. A project with split settings that built `BOLT_AUTHENTICATION_CLASSES` before a later file set `SECRET_KEY` kept the placeholder key, and every token failed to verify. The secret is now read when the routes compile to Rust metadata. (#291)
- **Access tokens carry a `jti`** - `create_token_pair` and `rotate_refresh_token` minted access tokens without a `jti`. A `JWTAuthentication` with a `revocation_store` sets `require_jti`, so it rejected every access token with `Authentication required`. Access tokens now get a fresh `jti` at issue and at each rotation. A store can now revoke one access token by its `jti`. (#304)
- **JSON encoding of `str` subclasses** - msgspec sends a `str` subclass, for example Django's `SafeString` from `mark_safe()`, to the encoder hook. The hook had no entry for `str` and raised `TypeError`. Such values now encode as a plain string.
- **ASGI mount: a client that went away raised into the app** - Bolt had no client-disconnect signal for mounted apps. A mounted app that parked in `receive()` never got `http.disconnect`, and Actix kept its handler alive until the app wrote. After `BOLT_ASGI_MOUNT_TIMEOUT`, Bolt returned 504, cancelled the app, and dropped the response channel. On Django 4.2 to 6.0, `ASGIHandler.handle()` waits with `asyncio.wait()`, which does not cancel its child tasks. The orphaned `process_request` task finished the view, called `send()`, and got `RuntimeError: http.response.start sent more than once`. Django 6.1 uses a `TaskGroup` and was not affected. Bolt now does what hyper and uvicorn do. A read-side EOF while a handler runs is a disconnect (`h1_allow_half_closed(false)`). `receive()` then returns `http.disconnect`, before headers and mid-body. After Bolt closes a response, on disconnect, timeout, or app exit, `send()` is a no-op. A real second `http.response.start` still raises. (#313)
- **`django_middleware=[...]` loads the list you pass** - The list form was a filter over `settings.MIDDLEWARE`. A path not in `settings.MIDDLEWARE`, a class object, or a typo was dropped with no error, and the route ran with no Django middleware. The list is now loaded as given, in order. A non-string entry raises `ImproperlyConfigured`. A path that does not import raises `ImportError` at startup, as in Django. The `include`/`exclude` dict form still filters `settings.MIDDLEWARE`.
- **`django_middleware`: `request.META` is the Rust-built dict** - The adapter built a second `META` in Python with no `REMOTE_ADDR` and a fixed `SERVER_NAME`. Middleware that reads the client address (django-debug-toolbar, django-axes, django-ratelimit) got `KeyError` or never matched. Django middleware and the handler now share the one `META` that Rust builds and caches, with `REMOTE_ADDR` from the client-ip resolver and `SERVER_NAME` from `Host`. The `request.state["META"]` round trip is gone.

### Added

- **`HTTPException(body=...)` for typed error responses** - Set `body` on the exception to send an error body as-is, with the status and headers of the exception. The return annotation of the handler stays the success type. Per-status bodies are no longer validated: the `response_model` dict documents the schema only. A tuple with a status code not in the dict is sent as-is in place of a 500. (#322)
- **Access-log level follows the status** - The `django.server` access log writes 5xx at ERROR, 4xx at WARNING, and other responses at INFO, as `runserver` does. A logger set to WARNING still gets the error lines. (#322)
- **`runbolt` runs the system checks and the migration check at startup** - Before it binds the port, `runbolt` runs the Django system checks and prints the unapplied-migration warning, as `runserver` does. A check error stops startup with a non-zero exit code. In dev mode each spawned worker runs the checks again on reload. A new `--skip-checks` flag skips the system checks. (#317)
- **Layered `include_in_schema`** - The flag now works like Litestar. Set it on `BoltAPI(include_in_schema=False)` to hide every route of that API, on a mounted sub-API, on an `APIView`/`ViewSet` class attribute, in `@api.view(...)`/`@api.viewset(...)`, or on one route. The most specific layer wins. `None` inherits from the outer layer.
- **Django Debug Toolbar guide** - `docs/src/topics/debug-toolbar.md` shows the setup. The toolbar works on Bolt routes through `django_middleware=[...]` and on mounted Django views. The example project has the setup and a mounted Django view at `/django/missions/`.

## [0.10.3]

### Added

- **Rate-limit buckets key on a hash** - A bucket key was stored as its own text. The map now holds an 8-byte hash of it, seeded once per process so a caller cannot craft a value that lands in another caller's bucket. Nothing is allocated per request, and the identity path matches the `key="ip"` path. This removes the 256-byte limit on a key: `@rate_limit(key="<header>")` with a long value, such as `key="authorization"` with a JWT, returned `400 Rate limit key too long` and now counts like any other value. (#301)

- **`@rate_limit(key="user")` and `key="api_key"` count per identity** - Both keys were documented but never implemented. They fell into the header catch-all, matched no header, and resolved to one shared bucket. A route marked "per user" was one global limit that one caller could exhaust for everyone. Both keys now run after authentication and count per `AuthContext` identity: any backend for `"user"`, API keys only for `"api_key"`. A caller with no identity is counted per client address. A route with an identity key and no auth backend fails at startup. `key="ip"` and header keys still run before authentication. (#301)

### Changed

- **`TestClient` lends its database connection to handlers** - A test that held an open transaction made rows that no handler could read. Django's `TestCase` and pytest-django's plain `django_db` are the two usual holders. Handlers run on framework threads, and Django gives each thread its own connection. On SQLite the write lock of the test also stopped the query of the handler, which showed as an unclear 500. While the client is open, the handler threads now use the connection of the test. Thus `@pytest.mark.django_db` and `TestCase` work. `transaction=True` is no longer a condition of testing a handler that uses the database. Rollback then removes the rows, which is faster than the table flush that `transaction=True` needs: 60 tests dropped from 0.93 s to 0.29 s. One connection cannot serve two threads at the same moment. Thus a lock is held for the life of each cursor, and concurrent database work waits in place of corrupting rows. The thread that makes a request gives its locks back while it waits, because it is blocked in the pipeline and runs no query. Thus a request inside a `QuerySet.iterator()` loop is safe. A second thread of the test that holds a cursor open cannot be parked in that way. That condition raises `SharedTestConnectionError`, which names the fixes. One fix is the escape hatch `TestClient(api, share_db_connection=False)`. `AsyncTestClient` does not share, because Django gives code in an event loop a different connection object. (#276)

### Performance

- **Faster `Response` serialization** - A `Response` now builds its wire metadata straight from its media type. Before, the content-type went into a copied header dict, and a second pass read it back out. A `Response` with no custom headers and no cookies now uses the static integer metadata tag. This applies to the four media types that match a static Rust content-type, and includes the `application/json` default. Any other media type keeps a metadata tuple. Per response, the metadata step drops from about 517 ns to 78 ns. Bytes bodies also render about 40% faster, because exact `bytes` now pass through unchanged.

### Security

- **`@rate_limit(key="ip")` trusted `X-Forwarded-For`** - The key came from the leftmost entry of `X-Forwarded-For`, which the client sends. Behind a proxy that appends the header, and with Bolt exposed directly, a client changed one header and got a new bucket for every request. Bolt now uses one canonical client address for rate limiting, `request.META["REMOTE_ADDR"]`, and request logging: the peer address by default, or the first untrusted `X-Forwarded-For` hop when the peer matches `BOLT_TRUSTED_PROXIES`. Malformed chains fall back to the peer. **Add the setting if you run behind a proxy**, or every caller behind it shares one bucket. (#302)

### Fixed

- **bolt-mcp: loopback code delivery** - After consent, a loopback redirect URI (`http://localhost` / `127.0.0.1` / `[::1]`) receives an interstitial page on the issuer origin. Before, it received a raw 302. Some browsers block the `https → http://localhost` redirect. Firefox with HTTPS-Only mode is one example. The client then never received the code. The page navigates with JavaScript, keeps a click-through link, and shows the code for copy-paste. An `https` redirect URI keeps the plain 302. To change the post-consent response, override the new `AuthorizationServer.code_redirect_response(redirect_url)`. (#307)
- **Pre-compressed `Response` bodies** - `Response(b"...")` with the default JSON media type wrapped the bytes in a base64 JSON string. A pre-compressed body with a `Content-Encoding` header reached the client corrupt. Bytes-like content now passes through verbatim, and the response validator skips it. (#305)
- **bolt-mcp 0.2.2: `resultType` on Python-built results** - `tools/call`, `resources/read` and `prompts/get` results now carry `resultType: "complete"` for 2026-07-28 clients. Claude Code and claude.ai connectors rejected every tool call without it. Legacy peers keep the old wire shape.

## [0.10.2]

### Added

- **Serializer loading plans** - A `Serializer` derives its own `select_related` (FK/O2O, dotted `source`), `prefetch_related` (M2M/reverse, with the nested serializer plan inside the `Prefetch`) and `annotate` (`Config.annotations`) from its fields. Use `Serializer.from_models(qs)`, `afrom_models(qs)` or `loading_plan(Model)`. Django 6.1+ also gets `fetch_mode(FETCH_PEERS)`. The plan is additive: loads that the caller applied are kept and not duplicated. Routes that return `list[Serializer]`, and `@paginate` pages that return a bare QuerySet, get the plan automatically, thus no N+1 queries and no user code. Async routes iterate with the async ORM, with no thread hop. (#292, #295)
- **`include_in_schema=False`** - Available on all route decorators. It was documented but not implemented. (#289)
- **`response_class` in the schema** - `HTML` documents `text/html`, `PlainText` documents `text/plain`, `File`/`FileResponse`/`StreamingResponse` document `application/octet-stream` binary, `EventSourceResponse` documents `text/event-stream`, and `Redirect` documents a 3xx response with a `Location` header and no body. (#289)
- **Custom pagination envelope** - `PaginationBase.response_class` and `build_response(items, *, total, request, has_next, has_previous, **info)` set the envelope on the wire and in the OpenAPI schema. (#288)
- **OpenAPI strict mode** - `OpenAPIConfig(strict=True)` raises `OpenAPIStrictError` that lists each untyped JSON response and each component that needed the `module.qualname` name fallback. (#290)

### Fixed

- **Generic component names** - `Page[UserRead]` renders as the component `Page_UserRead_` (msgspec naming), one component for each parametrization. `@paginate` routes document `PaginatedResponse_UserRead_` instead of a bare array. `PaginatedResponse` now uses `Generic[T]`, because its PEP 695 type parameter could not be resolved under PEP 563 and `items` was documented as `Any`. (#287)
- **Fieldless serializer** - A `Serializer` subclass with no fields dumped `{}`.
- **bolt-mcp: public OAuth routes** - The OAuth authorization-server routes and the protected-resource metadata route register with `auth=[], guards=[]`. Global `BOLT_DEFAULT_PERMISSION_CLASSES` / `BOLT_AUTHENTICATION_CLASSES` no longer answer 401 to discovery. `mount_mcp(auth=..., guards=...)` still overrides the globals for `/mcp` itself. (#296)

## [0.10.1]

### Changed

- **Cargo workspace** - The Rust extension is split into crates: `bolt-loop`, `bolt-core`, `bolt-asgi`, `bolt-websocket`, `bolt-mcp` and the root `django-bolt` PyO3 crate. No change to the Python API.
- **Repository cleanup** - Benchmark results moved to `python/benchmark/`, the nanodjango example folded into `python/example`, and the micro-benchmark harnesses, `llm.txt` and scratch endpoints removed from the public tree.

### Fixed

- **Serializer validation errors** - Per-field validation errors are kept, and `@classmethod` validators are no longer dropped. (#282)
- **`WorkerLoop` as a `SelectorEventLoop`** - The worker loop is a real `asyncio.SelectorEventLoop` on the Tokio reactor, thus stdlib transports, TLS, DNS, pipes, `sock_*`, datagrams and subprocesses all work with no second loop. (#286)

## [0.10.0]

### Added

- **MCP 2026-07-28 support** - bolt-mcp now uses the official MCP Rust SDK (rmcp). The `/mcp` endpoint serves 2026-07-28 clients without sessions, and older session clients (2024-11-05 to 2025-11-25) continue to work on the same endpoint. Elicitation on new clients uses multi round-trip requests: `ctx.elicit()` ends the call with an `input_required` result and a signed state, then the client sends the answers again (`ctx.is_replay`, `ctx.elicit(key=...)`). Rust answers `tools/list`, `server/discover`, `resources/list` and `prompts/list`, and applies the per-tool guards. New options: `MCP(title=, instructions=, website_url=, icons=, list_ttl_ms=, list_cache_scope=)`, `@mcp.tool(annotations=, icons=, output_schema="auto")`, and `mount_mcp(allowed_hosts=, allowed_origins=)` against DNS rebinding. The OAuth server adds the RFC 9207 `iss` parameter and obeys the DCR `application_type` value. If the client closes the stream, Bolt cancels the tool call.
- **BREAKING (bolt-mcp 0.2)** - bolt-mcp needs django-bolt 0.10 or later. `bolt_mcp.transport`, `bolt_mcp.sessions`, `bolt_mcp.types`, `MCP.dispatch` and `McpError` are removed. `MCP(stateless=)` and `MCP(json_response=)` now change only the legacy behavior, and `json_response=True` disables sessions. Some transport errors have different status codes: 415 for a malformed body, 422 for an unknown session.
- **`Requires` exclusion and custom denial messages** - `Requires(claim, none_of=[...])` permits the request only if the claim matches none of the given values. Give only one of positional values, `all_of` or `none_of`. To spell "has X but not Y", use two guards. A request with no authentication fails every `Requires` guard with 401. `message="..."` sets the `detail` text of the 403 response. (#273)
- **JWKS support for external identity providers** - `JWTAuthentication(jwks_url=...)` reads the key set of a provider (Clerk, Auth0, Okta) at server start. The `kid` header of each token selects the key. Use `jwks=` to give the key set directly. To use a new `kid`, start the server again. (#255)
- **Access and refresh tokens** - `create_token_pair(user, ...)` and `rotate_refresh_token(...)` give the two-token flow. Access tokens are short-lived and Rust validates them. Refresh tokens are long-lived and are used only at your rotation endpoint. Full rotation with reuse detection and access-only re-issue are both available. Issuance accepts any authentication method and an optional RFC 8176 `amr` claim. `set_token_cookies(response, pair)` sets the two tokens as `HttpOnly`, `Secure`, `SameSite=Lax` cookies. Set `refresh_path="/auth/refresh"` to keep the refresh cookie off usual API calls. Extra `claims=` cannot replace the reserved claims, and custom claims are not copied at rotation. (#239)
- **Token type enforcement** - `JWTAuthentication(token_type="refresh")` accepts only refresh tokens, for rotation endpoints. Usual routes now refuse refresh tokens. Rust does both checks before the handler runs. (#239)
- **Bulk revocation** - Revocation stores get `get_user_version`/`bump_user_version`, to log out a user everywhere, and `revoke_family`/`is_family_revoked`, to detect refresh-token reuse. `InMemoryRevocation` and `DjangoCacheRevocation` support both. A version increase stops all earlier refresh tokens at rotation. Access tokens stay valid until `exp`, thus keep their lifetime short. (#239)
- **CSRF protection for cookie authentication** - If a JWT backend reads its token from a cookie, Rust checks the origin of unsafe-method requests that send that cookie (`Sec-Fetch-Site`, or the host in `Origin`/`Referer`). Requests that do not send the auth cookie are not checked. To disable the check, set `csrf=False`. (#239)
- **`public_key=` and `leeway=` parameters** - `JWTAuthentication(public_key=...)` is the clear name for an asymmetric verification key. `leeway=` sets the permitted clock difference for `exp` and `nbf` (default 60 s). (#261)

### Performance

- **Faster dispatch** - Async requests run on a process-lived worker loop, which removes the per-request task scheduling and the thread wakeups. More routes now use the sync fast path, which saves approximately 6-12 µs for each request. Rust makes `datetime`, `date`, `time` and `UUID` parameters directly, with no string step. Async handlers that return a QuerySet now use a bounded thread pool, which you can set with `DJANGO_BOLT_ORM_THREADS`. (#268)

### Changed

- **New `--dev` output** - `runbolt --dev` shows a compact banner, the server URLs, and the start time in ms. Reload messages have a time stamp and color. Color obeys `--no-color`, `--force-color`, `NO_COLOR` and `TERM=dumb`. (#272)
- **BREAKING for cookie-JWT deployments** - Routes that use `JWTAuthentication(cookie=...)` now do the CSRF origin check by default on unsafe methods. Browsers are not affected. Clients that are not browsers, and that send the auth cookie with no origin data, now get 403. Send `Sec-Fetch-Site: none` or a correct `Origin`, use a header token, or set `csrf=False`.

### Fixed

- **`--dev` reload sees the full project** - The dev server now watches the project root and the modules that your api module imports. Edits to helper modules and new files start a reload. The new `--reload-dir` flag adds other directories. If the first start fails, for example because the port is in use, the server stops with the worker exit code. (#272)
- **Streaming and WebSocket handlers no longer hang** - These handlers ran on a different event loop than async HTTP handlers. Thus a WebSocket handler that waited for a future from an HTTP request waited forever. All Bolt handlers now run on the same loop. Mounted ASGI apps keep the selector loop. (#273)
- **`sync_to_thread` keyword arguments** - Keyword arguments now go correctly to the function. (#268)
- **Async compatibility on the worker loop** - Outgoing socket I/O such as `asyncio.open_connection()`, TLS, pipes, file descriptors, datagrams, subprocesses and Unix signals all work. Background tasks continue after the response, and module-level locks, queues and client pools stay usable between requests. If the two loops block each other, the crossing raises an error after `DJANGO_BOLT_LOOP_CROSSING_TIMEOUT` seconds (default 30) instead of waiting forever.
- **Asymmetric JWT algorithms verify correctly** - `JWTAuthentication` with a PEM public key refused all tokens, because Bolt always built the key as an HMAC secret. Bolt now builds the correct key type once at registration. PS256, PS384, PS512 and EdDSA are new. ES512 is removed from the docs, because the crypto library does not support it. (#261)
- **Tokens with non-string JOSE header values** - Providers put numbers or arrays in the token header (Clerk `oiat`, Auth0 `gty`). Such tokens did not parse, and thus failed before signature verification. They now verify correctly.
- **`aud` claims of real tokens** - An `aud` array, which is the Auth0 default, caused a failure. A token that only carried an `aud` claim was also refused when no audience was configured. `aud` now accepts a string or an array, and Bolt checks it only if you set `audience=`.
- **All configured algorithms are used** - Bolt used only the first algorithm in `algorithms`. The `alg` header of a token can now name any algorithm in the list. All algorithms in the list must use the same key family.

### Security

- **Bad authentication configuration stops the server at start** - A malformed PEM, an unknown algorithm name, mixed key families, or route metadata that does not parse now stop the start, and raise in `TestClient`. Before, Bolt removed the backend or the metadata with no message, and the route served with no authentication.
- **Strict issuer check** - If you set `issuer=`, Bolt now refuses a token that has no `iss` claim. Before, it accepted the token.
- **JWT failure reasons in the log** - An expired token, a bad signature, an algorithm that is not permitted, and an audience or issuer mismatch are now visible with `RUST_LOG=debug`. Before, all failures became a general 401 with no trace.

## [0.9.1]

### Added

- **Cookie-sourced JWT tokens** - `JWTAuthentication(cookie="<name>")` reads the token from a cookie instead of the `Authorization` header (no fallback between the two). Extraction happens in Rust via a zero-allocation scan of the raw `Cookie` header, so cookie auth stays GIL-free like header auth. (#260)
- **Per-backend custom user loading** - Auth backends can override `get_user` (async) and/or `get_user_sync` (sync) to customize how `request.user` is resolved; the strategy is resolved once per backend at registration, so the request path is a dict lookup plus a call. (#260)
- **ARM Linux wheels** - `aarch64` manylinux wheels are now built and published. (#258)

### Fixed

- **Custom `get_user` resolved per route, not per scheme name** - The user-loading registry was keyed globally by scheme name with first-registration-wins, so in an app mixing a custom-`get_user` JWT subclass with a plain JWT backend, whichever route registered first supplied the loader for all routes. Loaders are now resolved from each route's own backend instances at registration. (#260)
- **Project URLs in `pyproject.toml`** - Corrected the package's repository links on PyPI. (#257)

## [0.9.0]

### Added

- **HTTP `QUERY` method support** - The safe, body-bearing `QUERY` method — a `GET` that carries a request body, for search and filter endpoints that outgrow the query string — is wired through the whole stack: `@api.query` / `Router.query`, `APIView`, the `@action` decorator, the nanodjango plugin, and `TestClient.query`. Body structs are inferred as they are for `POST`/`PUT`/`PATCH`, `QUERY` joins the default CORS method list, and it coexists with `GET` on the same path. The OpenAPI generator emits it as a `query` operation, declaring OpenAPI `3.2.0` only when a `QUERY` route is present so every other schema stays `3.1.0` for maximum tooling compatibility. (#253)
- **Worker recycling for long-running servers** - `runbolt` can now recycle workers on actual memory pressure rather than a request-count proxy. `--max-rss <MiB>` recycles a worker once its resident set crosses the threshold, `--workers-lifetime <s>` recycles on wall-clock age, and `--respawn-failed-workers` replaces crashed workers instead of letting the fleet silently shrink. Recycling is spawn-first — the replacement binds the port via `SO_REUSEPORT` before the old worker receives `SIGTERM`, so there is always an accepting process — and staggered to at most one worker per tick. On shutdown (recycling, `systemd stop`, or `kubectl delete pod`) the server closes every WebSocket with code `1012` (Service Restart) so clients reconnect to a healthy worker, refuses new upgrades with `503` while draining, then performs a graceful stop for in-flight HTTP; `--workers-kill-timeout <s>` bounds the grace window before `SIGKILL`. (#248)
- **Configurable maximum parameter length** - `DJANGO_BOLT_MAX_PARAM_LENGTH` overrides the default request-parameter size limit. It is resolved once at startup (no per-request lock or env lookup) and applied uniformly across every parameter source: path, query, header, cookie, form, multipart, and WebSocket. (#240)

### Changed

- **Lower per-request memory footprint** - A pass of allocation reductions across the hot path. Request bodies are pre-allocated from `Content-Length`, collapsing the O(n²) reallocation traffic of repeated `extend_from_slice` into a single sized allocation (chunked/unknown-length bodies keep standard growth). Raw `ResponseWireV1` tuples bypass the `MiddlewareResponse` round-trip that previously allocated a wrapper and demoted integer meta tags to the slow path. The HTTP method is stored as a 1-byte enum instead of a heap-allocated `String`, and fast-path handlers (no middleware, signals, or Django middleware) skip the per-request connection-info string allocations they never read. The route metadata store moved from a sparse `Vec<Option<…>>` sized to the maximum route ID to a hash map sized to the actual route count, and common error responses — `400`/`413`/`500`, joining the existing `401`/`403`/`404` — now serve pre-computed static bodies with no per-response allocation. (#232)

### Security

- **Parameter-length limit is now enforced as a hard boundary** - Oversized values could previously slip past the limit on two paths: multipart text fields were fully buffered into memory before the check ran, and WebSocket query/path params fell back to passing the raw string through on any coercion error. Multipart fields are now checked incrementally while reading, and a length violation rejects the WebSocket upgrade with `400`. (#240)
- **Bounded async logging queue** - The `QueueHandler` behind non-blocking log delivery was unbounded, so a burst of records — e.g. a client flooding `5xx` errors — could grow in memory until OOM. It is now capped at 10,000 records; when the queue is full, new records are dropped rather than accumulating without limit. (#232)

### Fixed

- **Serializer response models render via `from_model()` + `dump()`** - Serializer (and `list[Serializer]`) responses were projected through `QuerySet.values()`, which drops computed fields, inherited fields, `write_only` exclusions, aliases, `source` mappings, and nested serializers. They now route through `from_model()` + `dump()` — including paginated list actions — with the faster `.values()` projection reserved for plain `msgspec.Struct` output. Unloaded Django relations detected mid-dump fall back to the async `afrom_model()` loader instead of failing. (#251)
- **Admin session support is detected via middleware, not `INSTALLED_APPS`** - The admin auto-mount runs through Django's own ASGI application and therefore `settings.MIDDLEWARE`, so its real requirement is a `SessionMiddleware` in that chain (mirroring Django's own `admin.E410` system check), not `django.contrib.sessions` in `INSTALLED_APPS`. Matching against a `SessionMiddleware` subclass means alternative backends like `django-qsessions` — which replace the sessions app and subclass the middleware — are recognized instead of spuriously disabling the admin.
- **OpenAPI: named enums now emit reusable components** - Enums (`enum.Enum`, msgspec `EnumType`, Django `TextChoices`/`IntegerChoices`) used in request/response bodies were inlined as `{"type": "string", "enum": [...]}` at every use site, so codegen tools (`openapi-typescript`, `typescript-fetch`) couldn't emit a shared named type and the enum's docstring was lost. Named enum classes are now promoted to `#/components/schemas/<Name>` components + `$ref` — parallel to how Structs are registered — carrying their docstring as `description`, matching `msgspec.json.schema_components`. Anonymous `Literal[...]` unions and query-parameter enums stay inline (no name to register under / inline context). Component naming is two-pass: a type keeps its short `__name__` unless another *distinct* type shares it, in which case the colliding types expand to their `module.qualname` so same-named types from different apps coexist (also matching `msgspec.json.schema_components`); only types that are indistinguishable by module + qualname raise `ComponentNameCollisionError`. (#246)
- **OpenAPI: struct field `default_factory` values are no longer dropped** - Fields whose default came from one of the builtin mutable factories (`list`/`dict`/`set`/`bytearray`, e.g. `tags: list[str] = msgspec.field(default_factory=list)`) lost their `default` in the generated schema because the generator read only `field.default`, never `field.default_factory`. It now materializes those four factories so the schema carries `default: []`/`default: {}`, matching `msgspec.json.schema` exactly — other factories (e.g. `datetime.now`) deliberately get no `default`, as msgspec does, which also avoids embedding a non-JSON-encodable value that would break spec serialization. (#245)

### Documentation

- **Testing guide rewritten** - The testing guide is now anchored by an executable, end-to-end proof rather than standalone snippets, so the documented `TestClient` workflow is verified against the real stack. (#252)

## [0.8.4]

### Fixed

- **OpenAPI: typed dict values now render their value type** - `dict[K, V]` was emitted as `{"type": "object", "additionalProperties": true}` regardless of `V`, so codegen tools (`openapi-typescript`, `typescript-fetch`) generated `Record<string, unknown>` for every typed map (`dict[str, int]`, `dict[str, SomeStruct]`, str→str maps, …). Both dict paths in the schema generator now recurse into `V` — exactly as the adjacent list handlers recurse into the item type — emitting `additionalProperties: {"type": "integer"}`, `additionalProperties: {"$ref": …}` (nested Structs are registered as components), or `additionalProperties: {"anyOf": [...]}` for `dict[str, T | None]`, matching `msgspec.json.schema`. Untyped dicts (`dict`, `dict[str, Any]`) keep `additionalProperties: true`.
- **OpenAPI: documented constrained types now render as their base type** - Custom types built with `Annotated[T, msgspec.Meta(...)]` that also carry a `description`/`examples`/`title` — every type in `django_bolt.serializers.types` (`Email`, `PositiveInt`, `HttpsURL`, …) — were emitted as an empty `{"type": "object"}` schema, so codegen tools (`openapi-typescript`, `typescript-fetch`) generated `object` instead of `string`/`integer`. `msgspec.inspect` wraps such fields in a `Metadata` node that the generator didn't recognise; it now unwraps that node to the underlying type, carrying constraints (`maxLength`, `pattern`, `exclusiveMinimum`, …) and docs through, matching `msgspec.json.schema`. Custom types used directly as response models (`-> Email`, `-> list[Email]`) are normalised the same way. (#235)

## [0.8.3]

### Added

- **Optional `mcp` / `bolt-mcp` install extras** - The MCP add-on introduced in 0.8.2 is now installable through package extras, so `mount_mcp()` users pull the dependency explicitly while it stays out of the base install.

## [0.8.2]

### Added

- **Optional `bolt-mcp` add-on (MCP over Streamable HTTP)** - New pure-Python `bolt-mcp` package and an `api.mount_mcp()` entrypoint serve a Model Context Protocol server at a route, reusing Django-Bolt's Rust-side auth and per-tool guards. Supports OAuth 2.1 — including a built-in Authorization Server (RFC 9728/8414 discovery, RFC 7591 dynamic client registration, Authorization Code + PKCE, RFC 7009 revocation) so OAuth-native clients (Claude.ai, ChatGPT, Claude Code) link once and refresh silently — plus an explicit tool allowlist. Stays an optional dependency via lazy import and ships its own tag-driven release flow (`just release-mcp`).
- **URL reversing for Bolt routes** - Include `django_bolt.urls` in your `ROOT_URLCONF` (`path("", include("django_bolt.urls"))`) and Django's `reverse()`, `reverse_lazy()`, and the `{% url %}` template tag resolve Bolt route names. Entries are reverse-only — Bolt still serves the paths in Rust and the registered views never run; path converters, `args`/`kwargs`, `query`, and `fragment` come from Django's own resolver.
- **`name=` on every route decorator** - `@api.get/post/put/patch/delete/head/options`, `@api.websocket`, `@api.view`, `@api.viewset`, and `@action` accept an explicit reverse name. Unnamed routes derive a name verbatim from the Python identifier (function name, or class name for class-based views); viewsets name each route `{base}-{action}` (e.g. `user-list`, `user-partial_update`).
- **Opt-in reverse namespaces** - `BoltAPI(namespace="...")` mirrors Django's `app_name`; namespaced routes reverse as `namespace:name` and resolve only under that namespace.

### Security

- **Supply-chain hardening for CI** - All third-party GitHub Actions and pre-commit hooks are SHA-pinned (with version comments), closing the tag-mutation hole; Renovate keeps the digests updated. A new `prek` lint workflow (ruff + trailing-whitespace/end-of-file hooks) runs on every PR. (#238)

### Fixed

- **OpenAPI component schemas carry struct title and description** - `_struct_to_schema` now populates `title` from a `msgspec.Struct`'s `__name__` and `description` from the struct's own docstring, matching `msgspec.json.schema_components`. Downstream codegen (e.g. `openapi-typescript`) renders the description as JSDoc on generated types. The docstring is read from `struct_type.__dict__` directly so an undocumented struct doesn't inherit `msgspec.Struct`'s base-class docstring. (#224)

### Documentation

- **Routing docs** - New "URL names and reversing" section in `routing.md` covering wiring, naming, derived names, namespaces, viewset/`@action` naming, and collision rules; `class-based-views.md` documents the `@action` `name=` parameter and cross-links it.

## [0.8.1]

### Added

- **Native media serving** - With `MEDIA_URL` + `MEDIA_ROOT` set, uploads are served from Rust: `GET`/`HEAD`, ETag/Last-Modified, conditional (`304`) and range requests.
- **Native static serving** - `/static` now uses the same Rust handler (Python static route removed), resolving via `STATIC_ROOT`/`STATICFILES_DIRS`, with staticfiles finders as a `DEBUG`-only fallback.
- **`BOLT_STATIC_MAX_AGE` / `BOLT_MEDIA_MAX_AGE`** - Emit `Cache-Control` on `2xx`: `public` for static, `private` for media. Validated into a pre-built header at startup.
- **Optional CSP on file responses** - `SECURE_CSP`, if set, applies `Content-Security-Policy` to static and media responses (including errors).
- **Union response types** - Handlers can declare union (`X | Y`) return types. (#228)
- **Sequence form fields** - Repeated form keys bind to `list[T]` struct fields, with list emission handled Rust-side.
- **Per-chunk streaming compression** - `StreamingResponse`/`EventSourceResponse` compress per chunk with a sync flush (events reach the client immediately); one encoder per connection preserves cross-chunk ratio. brotli/gzip/zstd; opt out with `@no_compress`.
- **Unified `CompressionConfig`** - One config drives both buffered and streaming compression, sharing `@no_compress` and `Accept-Encoding` negotiation.

### Security

- **Script-bearing uploads force-downloaded** - Media with script-capable extensions (`.html`, `.svg`, `.js`, `.xml`, `.wasm`, …) is served as `application/octet-stream` + `Content-Disposition: attachment`. Inert types (images, PDF, CSS, JSON, text) still render inline. Media only — static keeps native content types.
- **`X-Content-Type-Options: nosniff` on every file response**, including 404s.
- **Traversal/dotfile/symlink protections** - `..` paths rejected with `400`; leading-dot components (`.env`, `.git/config`, …) return `404`; symlink targets must stay inside the root.
- **No static/media route collisions** - Media misses no longer fall through to staticfiles finders, so `STATICFILES_DIRS` assets can't leak under `/media/`.
- **Media config validated at startup** - `MEDIA_URL` must start with `/`; `MEDIA_ROOT` must be absolute.

### Changed

- **Unified static/media config** - Collapsed into one `ScopeConfig` tagged by a `ServeMode` enum (one `app_data` lookup per request). `HEAD` registered for both scopes, mirroring GET headers.
- **`brotli_lgwin` default 18 → 14** - 16 KiB window (was 256 KiB) cuts per-stream memory ~16× at minimal ratio cost. Tune up for large repetitive bodies, down for high-fanout streams.
- **Buffered Accept-Encoding negotiation now RFC 7231 §5.3.4-compliant** - Shares the streaming parser: honors q-values (`br;q=0`), the `*` wildcard, and case-insensitive tokens. The old substring matcher mis-handled all three.

### Fixed

- **`BoltAPI(compression=False)` now disables buffered compression** - Previously it still fell back to defaults and compressed anyway; the negotiator now returns `identity` when no config is attached. (`compression=None`/omitted still applies the default `CompressionConfig()`.)

### Documentation

- **New `media-files.md`** - Upload security model, `Cache-Control`, and production offloading (Nginx / object storage). `static-files.md` and settings reference updated for native serving and `BOLT_*_MAX_AGE`.
- **New `compression.md`** - Buffered + streaming compression documented together (replaces `streaming_compression.md`): `CompressionConfig`, negotiation, per-chunk flush, `lgwin` memory table, level/ratio tradeoffs, CRIME/BREACH. `middleware.md` shortened to link here.

## [0.7.5]

### Added

- **`EventSourceResponse` for Server-Sent Events** - FastAPI-style SSE response type with automatic event framing and reconnection support. (#203)
- **Class-based views improvements** - `ViewSet`/`ModelViewSet` refinements alongside built-in pagination support for class-based list actions. (#189)

### Fixed

- **`Depends()` targets that read `request.query`/`.headers`/`.cookies`/`.body`** - Registration-time static analysis now recurses into `Depends()` targets (including class-callable backends like `Depends(FilterBackend(...))`), so a dep that accesses request components no longer sees an empty dict. Rust was silently skipping request-component parsing for routes whose handler didn't directly reference `request.*` even though a dep did.
- **Serializer `model_validate` now reports missing required fields** - Validation errors now include the names of missing required fields instead of a generic message. (#201)
- **OpenAPI schema unwraps `_FieldMarker` defaults** - Generated OpenAPI schemas no longer leak internal `_FieldMarker` objects as serializer defaults.

## [0.6.4]

### Added

- **ASGI support for Django views and URL mounts** - Added ASGI mounting support for Django views/URLs with updates in routing, middleware response handling, server integration, and test client behavior. (#145)
- **Rust-side argument prebinding on hot path** - Added Rust prebinding path to reduce Python injector overhead during request handling.
- **Structured `runbolt` startup banner** - Redesigned startup output with clearer structured runtime information.

### Changed

- **Core hot-path optimizations** - Reduced unnecessary cloning/parsing and improved sync serialization and request pipeline performance.
- **Parameter extraction updates for msgspec annotations** - Improved extraction behavior and middleware compilation for msgspec-based annotations.
- **Testing/runtime dependencies** - Added `httpx` to installation dependencies and aligned testing docs with the runtime setup.

### Fixed

- **OpenAPI nested serializer schema generation** - Fixed nested serializer schema rendering in generated OpenAPI docs. (#144)
- **Multiprocess shutdown handling** - Fixed process shutdown behavior for multiprocess `runbolt` execution.
- **Static file serving** - Fixed static file serving behavior in fresh installs and removed bundled example admin staticfiles to rely on the proper Django static pipeline.
- **Minor typo fix** - Corrected typo. (#138)

### Documentation

- Added ASGI mounts documentation and updated API/routing/settings references.
- Fixed repository URL in README. (#148)
- Corrected Agents file naming and additional docs assumption fixes.

## [0.6.0]

### Added

- **Actix-native static file serving** - Serve static files directly from Actix without Python handler overhead. Reads Django settings (`STATIC_URL`, `STATIC_ROOT`, `STATICFILES_DIRS`) at startup and uses actix-files with proper ETag, Last-Modified, and MIME type handling. Includes Content Security Policy header support, directory traversal prevention, symlink security, and falls back to Django staticfiles finders in debug mode only. (#123)
- **Session authentication** - Django session framework integration with async session methods (`request.session.aget()`, `aset()`, `apop()`, `aflush()`, etc.) for non-blocking session access. Includes login/logout endpoint examples and browser-based session demo. Removed the deprecated `SessionAuthentication` class in favor of using Django's built-in session middleware directly. (#112)
- **Global authentication and permission classes** - New `BOLT_AUTHENTICATION_CLASSES` and `BOLT_PERMISSION_CLASSES` Django settings to configure project-wide defaults instead of specifying them on every endpoint. (#121 (Docs Changes))
- **camelCase/snake_case field mapping in serializers** - Serializers now support automatic field name transformation via `rename="camel"` in the Config class, allowing snake_case Python attributes to serialize as camelCase JSON and vice versa. (#115)
- **`request.META` compatibility** - Added `request.META` dictionary populated in Rust with HTTP headers (as `HTTP_*` keys), server info (`SERVER_NAME`, `SERVER_PORT`), and request metadata (`REQUEST_METHOD`, `PATH_INFO`, `QUERY_STRING`, `CONTENT_TYPE`, `CONTENT_LENGTH`). Enables easier migration from Django views and compatibility with libraries expecting Django request objects. (#128)

### Changed

- **Cookie and header parsing moved to Rust** - Cookie parsing now uses the actix-cookie crate in Rust instead of Python-side parsing. Added `response.set_cookie()` shortcut method with support for all cookie attributes (max_age, expires, path, domain, secure, httponly, samesite). Removed Python-side cookie serialization for better performance. (#127)
- **Removed legacy middleware configuration** - Removed deprecated `middleware` parameter format from `BoltAPI` constructor that accepted raw middleware config dicts. Middleware should now be passed as classes or the `@cors`/`@rate_limit` decorators. (#120)
- **Middleware safety classification** - `DjangoMiddlewareStack` now automatically classifies Django middleware as safe (async-compatible) or unsafe (blocking I/O) based on known patterns. Added `auser` property setter on Request for compatibility with Django's `alogin()` and `alogout()` functions. (#93)

### Fixed

- **Streaming response handling in middleware** - Fixed `TypeError: cannot unpack non-iterable StreamingResponse object` when using `StreamingResponse` with middleware. The serialization layer now correctly returns streaming responses as tuples, and both `handler.rs` and `testing.rs` detect and process `StreamingResponse` bodies appropriately while preserving SSE-specific headers. Added `PyOnceLock`-based caching to avoid repeated imports on the streaming path. (#126)
- **Pagination respects serializer fields** - Pagination now correctly uses serializer field definitions for item serialization instead of raw model fields. Added `extract_pagination_item_type()` helper and enhanced `PaginatedResponse` with `omit_defaults=True` for cleaner output. (#100)
- **`from_model()` respects `field(source=...)` parameter** - Fixed serializer's `from_model()` ignoring fields defined with `source="..."`. Now checks `__source_mapping__` and correctly retrieves values using the source path, including dot-notation nested access (e.g., `source="author.name"`). Also improved Python 3.14 compatibility using `inspect.get_annotations()` for PEP 649 deferred annotation evaluation. (#107)

### Documentation

- Clarified global vs per-view file upload size limits with examples. (#102)
- Added comprehensive static files documentation with CSP configuration.
- Added session authentication setup guide with login/logout examples.
- Enhanced pagination documentation with serializer integration patterns.

### New Contributors

- @NourEldin-Osama
- @Rey092
- @athul-binu

## [0.5.1]

### Changed

- **Starlette-style trailing slash redirects** - When a request URL doesn't match a route, Django-Bolt now checks if the alternate path (with/without trailing slash) exists. If found, returns a **308 Permanent Redirect** to the canonical URL. This means both URLs work, with the non-canonical one redirecting.
- **Mixed trailing_slash settings** - When auto-discovering multiple `api.py` files, each API's routes now keep their own `trailing_slash` format. Different apps can use different conventions without conflict.

### Fixed

- **Trailing slash with multiple APIs** - Fixed issue where APIs with different `trailing_slash` settings would conflict when merged. Each API now respects its own setting.

### Documentation

- Updated routing docs with trailing slash redirect behavior and multi-API examples.

## [0.5.0]

### Added

- **Trailing slash configuration** - New `trailing_slash` parameter for `BoltAPI` to control path normalization:
  - `"strip"` (default): Remove trailing slashes (`/users/` → `/users`)
  - `"append"`: Add trailing slashes (`/users` → `/users/`)
  - `"keep"`: No normalization, keep paths as defined
- **URL prefix support** - New `prefix` parameter for `BoltAPI` to apply a URL prefix to all routes (e.g., `/api/v1`).
- **Memory allocator options** - Optional jemalloc and mimalloc support via feature flags for improved memory performance.

### Performance

- **Type coercion moved to Rust** - Path, query, and header parameter type coercion now happens in Rust before reaching Python.
- **Form parsing in Rust** - Multipart form data and URL-encoded body parsing moved to Rust for faster file uploads and form handling.
- Combined, these changes provide **10-60% performance improvement** depending on endpoint complexity.
- **Interned Python strings** - Attribute access in Rust hot paths uses interned strings for faster lookups.

### Fixed

- **OpenAPI authorize button** - Fixed authorize button not showing for routes with authentication in Swagger UI.
- **OpenAPI docs with prefix** - Documentation routes (`/docs/*`) now work correctly when API has a prefix, staying at absolute paths.
- **Mount path normalization** - Mount paths without leading slash are now properly normalized.

### Changed

- **Refactored parameter extraction** - Consolidated `binding.py` into `_kwargs` module with pre-compiled extractors for better performance and maintainability.
- **Updated jsonwebtoken to v10** - Upgraded Rust JWT library.

## [0.4.8]

### Added

- **UploadFile class** - Django-compatible file upload handling with automatic resource cleanup, size validation, and content type checking.
- **FileSize enum** - Human-readable file size constants (`FileSize.MB(10)`, `FileSize.GB(1)`) for upload limits.
- **MediaType enum** - Content type constants for response handling.
- **Tags support for views** - Added `tags` parameter to `@api.view()` and `@api.viewset()` decorators for OpenAPI grouping.

### Changed

- **Documentation improvements** - Updated routing and cookie documentation.

## [0.4.7]

### Performance

- **Lazy user loading optimization** - Replaced lambda with `functools.partial` for `SimpleLazyObject` user loader, avoiding closure allocation overhead per authenticated request.
- **Response serialization fast path** - Added dedicated fast path for dict/list responses (90%+ of handlers) that skips `_convert_serializers()` check and unnecessary isinstance chain.

## [0.4.6]

### Added

- **Multi-error validation collection** - Serializer now collects ALL validation errors before raising, matching Pydantic's behavior. Both `@field_validator` and `@model_validator` errors are collected across all fields.
- **Meta constraint multi-error collection** - `model_validate()` and `model_validate_json()` now collect all msgspec Meta constraint errors (min_length, pattern, ge, le, etc.) using Litestar's field-by-field validation approach.
- **Parameter models** - Support for `Annotated[Struct/Serializer, Form()]` pattern like FastAPI. Group related form fields, query parameters, headers, or cookies into a single validated object using `msgspec.Struct` or `Serializer`.
  - `Annotated[FormModel, Form()]` - Group form fields with validation
  - `Annotated[QueryModel, Query()]` - Group query parameters
  - `Annotated[HeaderModel, Header()]` - Group headers (snake_case fields map to kebab-case headers)
  - `Annotated[CookieModel, Cookie()]` - Group cookies
- **Testing documentation** - Comprehensive testing guide covering TestClient usage, database transactions, and integration testing patterns.
- **Serializer vs msgspec.Struct documentation** - New docs section explaining differences in error handling between raw msgspec.Struct and Django-Bolt Serializer.

### Changed

- **204 No Content support** - Framework now properly handles `None` return values for endpoints with `status_code=204`. DELETE endpoints should return nothing with 204 status.

### Fixed

- **500 error on 204 responses** - Fixed server error when handlers returned `None` for 204 No Content responses.
- **Validation errors now return 422** - Missing required parameters (query, header, cookie, form, file) now return 422 Unprocessable Entity instead of 400 Bad Request, per RFC standards.

### Removed

- **`django-bolt init` command** - Removed CLI initialization command. The CLI now only provides the `version` command.

## [0.4.0]

### Changed

- **Python 3.12+ required** - Dropped support for Python 3.10 and 3.11, now requires Python 3.12 or newer.
- **Modern Python syntax** - Adopted PEP 695 generic syntax, `datetime.UTC`, and native `NotRequired` from Python 3.12+.
- **PyO3 ABI update** - Updated from `abi3-py310` to `abi3-py312` for improved Rust-Python interop.

## [0.3.13]

### Added

- **Django middleware integration** - Full support for Django's middleware pattern with automatic loading from `settings.MIDDLEWARE`.
- **DjangoMiddleware adapter** - Seamlessly wrap and use existing Django middleware in Bolt applications.
- **Middleware loader** - Automatic discovery and loading of Django middleware with configurable selection.
- **Ruff linting and type checking** - Added comprehensive code quality tooling with pyproject.toml configuration.

### Changed

- **Unified middleware pattern** - Middleware now uses Django's `__init__(get_response)` and `__call__(request)` pattern for consistency.
- **Middleware architecture** - Complete redesign following the middleware design document with zero overhead as priority.
- **Router enhancements** - Added `middleware`, `auth`, and `guards` parameters for router-level configuration with inheritance support.
- **Response builder optimizations** - Refactored response building pipeline in Rust with dedicated modules (`response_builder.rs`, `responses.rs`, `headers.rs`).
- **Code quality improvements** - Applied Ruff linting across entire codebase, improved type hints and imports.

### Fixed

- **Middleware error handling** - Improved exception catching and proper response generation in middleware pipeline.
- **Error handling improvements** - Enhanced error messages to include original exception context for better debugging.
- **Redirect Error in docs and admin** - Fixed Error because of path normalization that we added because websocket.

## [0.3.12]

### Added

- **WebSocket parameter injection** - Pre-compiled injectors for improved WebSocket parameter handling (query, header, and cookie parameters).

### Changed

- **WebSocket performance** - Enhanced WebSocket route registration with pre-compiled metadata for better parameter handling.
- **Admin route improvements** - Updated admin route registration to support both trailing and non-trailing slash versions.

### Fixed

- **WebSocket connection management** - Enhanced resource management and stability in WebSocket handlers.

## [0.3.11]

### Added

- **WebSocket support** - Complete WebSocket implementation with FastAPI-like syntax using `@api.websocket()` decorator.
- **WebSocket testing** - `WebSocketTestClient` for testing WebSocket handlers without network.
- **WebSocket security** - Origin validation, authentication guards, and permission checks for WebSocket routes.
- **WebSocket configuration** - Configurable via Django settings: channel size, heartbeat interval, client timeout, allowed origins.
- **WebSocket documentation** - Comprehensive guide at `docs/WEBSOCKET.md`.

### Changed

- **Rust WebSocket infrastructure** - Actix-based WebSocket actor system with ASGI-style message queue bridge using tokio channels.
- **WebSocket routing** - Zero-overhead WebSocket route matching with support for path parameters.

### Fixed

- **WebSocket resource leak** - Fixed thread pool exhaustion by using `pyo3_async_runtimes::into_future()`.
- **WebSocket type coercion** - Fixed handling of `Annotated` types with PEP 563 string annotations.
- **WebSocket error handling** - Proper panic safety with `catch_unwind` and differentiated setup vs runtime errors.

## [0.3.10]

### Changed

- **Lazy user loading by default** - User loading now uses Django's `SimpleLazyObject` to defer database queries until `request.user` is first accessed. This avoids unnecessary DB calls when user data isn't needed.

## [0.3.9]

### Changed

- **Precompile optimizations** - Handler metadata (parameter extraction, validation, injectors) is now precompiled at route registration time instead of per-request. This eliminates repeated introspection overhead during request handling.

### Added

- **Static analysis for sync handlers** - New `analysis.py` module performs AST-based analysis of handler source code to detect Django ORM usage and blocking I/O patterns. Sync handlers without blocking calls can skip thread pool dispatch for better performance.

## [0.3.8]

### Fixed

- **Static file serving** - Fixed static route handler missing `is_async` and `injector` metadata, which caused static file routes to fail.

### Added

- **CLI `version` command** - Added `django-bolt version` command to display the installed version.
- **`llm.txt`** - Added LLM-friendly project summary file.

## [0.3.7]

### Fixed

- **CORS `@cors()` decorator validation** - The `@cors()` decorator now requires an explicit `origins` argument. Using `@cors()` without arguments previously created an empty CORS config that silently overrode global Django CORS settings, causing credentials and other headers to be missing. Now raises `ValueError` with helpful examples.
- **CORS for POST-only routes** - Routes that only had POST/PUT/PATCH methods (no GET) were not finding their CORS config during preflight, causing CORS failures.

### Changed

- **Shared CORS implementation** - Unified CORS handling between production server and test infrastructure. Test client now reads all CORS settings from Django (`CORS_ALLOWED_ORIGINS`, `CORS_ALLOW_CREDENTIALS`, `CORS_ALLOW_METHODS`, `CORS_ALLOW_HEADERS`, `CORS_EXPOSE_HEADERS`, `CORS_PREFLIGHT_MAX_AGE`).

## [0.3.6]

### Fixed

- **CORS preflight for non-existent routes** - OPTIONS preflight requests to non-existent routes now return 204 (success) instead of 404, allowing browsers to proceed with the actual request and display proper error messages.
- **CORS headers on 404 responses** - Non-existent routes now include CORS headers using global config, so browsers can read error responses.

### Changed

- Updated CORS documentation to emphasize Django settings-based configuration as the preferred approach.

## [0.3.5]

### Changed

- **Extended Serializer class** - Added more features like write_only, more built-in types to better work with django models.
- **Serializer Config class** - Renamed `Meta` to `Config` to avoid conflicts with `msgspec.Meta`.
- **Field configuration** - Removed direct Meta constraints from `field()` function; validation constraints now require `Annotated` and `Meta`.

### Fixed

- Fixed Python 3.14 annotation errors.

## [0.3.4]

### Added

- Python 3.14 support with msgspec 0.20.
- Advanced Serializer features including `kw_only` support.

### Changed

- Refactored concurrency handling in `sync_to_thread` function.
- Updated logging levels to DEBUG for improved debugging.

## [0.3.3]

### Added

- Docs changes related to serializer.

### Changed

- When None is returned from field validation function it uses the old value instead of setting it into None.

- dispatch function clean for performance.

### Fixed

## [0.3.2]

### Added

- `Serializer` class that extends msgspec struct using which we can validate response data using python function.

### Changed

- sync views are not handled by a thread not called directly in the dispatch function.

### Fixed

- Fixed Exception when orm query evaludated inside of the sync function.

- Fixed `response_model` not working.

## [0.3.1]

### Added

- **`request.user`** - Eager-loaded user objects for authenticated endpoints (eager-loaded at dispatch time)
- Type-safe dependency injection with runtime validation
- `preload_user` parameter to control user loading behavior (default: True for auth endpoints)
- New `user_loader.py` module for extensible user resolution
- Custom user model support via `get_user_model()`
- Override `get_user()` in auth backends for custom user resolution
- Authentication benchmarks for `/auth/me`, `/auth/me-dependency`, and `/auth/context` endpoints

### Changed

- Replaced `is_admin` with `is_superuser` (Django standard naming)
- Optimized Python request/response hot path
- Auth context type system improvements in `python/django_bolt/types.py`
- Guards module updated to use `is_superuser` instead of `is_admin`

### Fixed

-
