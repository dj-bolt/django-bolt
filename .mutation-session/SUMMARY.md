# Mutation testing of django-bolt: result

Date: 2026-11-15. Base: `master` at 7530034. All pull requests have base `master`.

## Totals

- 277 pull requests are open for this effort: #586 to #863, without #593 (not part of this effort).
- 216 fixes, 46 tests-only, 8 docs-only, 5 new options (`feat`), 2 removals of dead code (`refactor`).
- Each fix has one failing test first. Each pull request ran the full Python suite on its own branch.
- Merge check (local, nothing pushed): in number order, 189 of the 277 merge with no conflict (CHANGELOG.md merged with the union rule) and 88 conflict with an earlier pull request. The tree of the 189 builds. Full suite on that tree: 6,201 passed, 43 skipped, 1 failed. The one failure was a test of #859 that read a default status that #818 changes; a follow-up commit on #859 corrects the test, and its file then passes on the merged tree.
- A pull request that conflicts was tested only on its own branch (and on its stack).
- The merge check found three defects that I corrected with a follow-up commit on the branch of the pull request: a scratch test file in #622 and in #627 (removed), and the test of #859 above.

## What the mutation runs covered

Rust (`cargo-mutants`, the files of `.cargo/mutants.toml`): 1,724 mutants; 1,048 caught, 106 missed, 11 timeout, 559 unviable.
Of the 106 missed: 89 are killed by the Python suite or by other Rust tests, 8 were real gaps (closed by #669, #675, #731, #805), 4 are equivalent, 5 are code that this build does not compile (`Py_GIL_DISABLED`).

Python (`mutmut`): each module of `python/django_bolt/` had a run with the test files of its area. Numbers for the largest modules:

| Module | Mutants | Result |
|---|---|---|
| `api.py` | 3,705 | All groups run. Dispatch path: 1,108 mutants, 13 survived (10 gaps closed by #844, 3 cosmetic). |
| `openapi/schema_generator.py` | 2,152 | 545 survived the first run; after the tests-only pull requests 108 real gaps are left. |
| `management/commands/runbolt.py` | 1,752 | 725 are code that only a subprocess test runs; 246 were not run (watch-path helpers of `--dev`). |
| `serialization.py` | 1,204 | 20 survived; 17 closed by #757, 3 equivalent. |
| `concurrency.py` | 716 | About 237 need PostgreSQL and 40 cannot run under mutmut. |
| `middleware/compiler.py` | 556 | 6 survived, all equivalent. |
| `testing/websocket.py` | 519 | 178 survived; gaps closed by #748, bugs fixed in #728, #730. |
| `analysis.py` | 361 | 3 survived; closed by #757. |

Not mutation-tested, with the reason:
- Code that needs PostgreSQL or Redis (connection pool checks in `concurrency.py`, the Redis revocation store). The container has neither.
- Code that only a real `runbolt` process runs (`start_single_process`, `start_multiprocess`, signal handlers, the banner, parts of `_worker_loop.py`). mutmut does not reach a subprocess.
- `cli.py` (its tests load a copy of the file) and `request.py` (a Protocol).
- `mount_mcp` in `api.py` and the `python/bolt-mcp` package: their tests are in the bolt-mcp suite, which was not run under mutation.

## Security fixes (review these first)

- #606 a revoked token stays revoked until `JWTAuthentication` stops accepting it (the revocation entry expired before the leeway ended).
- #607 the logout routes reject a refresh token with no family. #608 `rotate_refresh_token` rejects claims that are not from a refresh token.
- #609 `JWTAuthViews` verifies with the public key for an asymmetric algorithm.
- #641 `APIKeyAuthentication` rejects an unsafe key configuration. #784 a key that starts with a scheme word (`{"abc", "Bearer abc"}`: the header `Bearer abc` authenticated as key `abc`).
- #657 the write actions of a ViewSet do not take the primary key from the body. #665 the arguments of `@api.viewset()` (auth, guards) apply to the `@action` routes.
- #797 a nested `Router` inherits `auth`, `guards` and `tags` of its parent (a child route answered 200 with no token).
- #725 an empty pattern in `CORS_ALLOWED_ORIGIN_REGEXES` stops startup (with credentials on, each origin was allowed).
- #723 the docs pages escape the text of the API (`</script>` in a docstring left the Swagger/ReDoc script).
- #780 a JWT of an inactive user loads no user. #821 `create_jwt_for_user` refuses an extra claim that replaces `sub`, `exp` or `is_superuser`.
- #619 a serializer in a dict, tuple or set field is dumped (write_only fields left the server). The maintainers' #570 changes the same code: #619 is probably not needed after #570.
- #762 (your decision) a guarded route with no auth backend uses `JWTAuthentication()`. Read its merge note: a token that the project signs with `SECRET_KEY` for another purpose now opens such a route.

## Pull requests that change behavior for existing code (your decision at merge)

Each pull request has a section "For the merge decision". The ones with the largest effect:

- Startup errors where there was silence: #736 (a second handler for one method and path), #746 (a config decorator above the route decorator), #751, #752, #750, #788 (a parameter type that Bolt cannot convert), #801, #660/#706/#712/#729 (bad CORS and rate-limit options), #789, #824/#850, #851/#859, #814, #699, #701, #711.
- HTTP answers: #782 (a wrong method answers 405 with `Allow`, was 404), #793 (a status must be 200 to 599), #776 (a WebSocket handler that returns closes with 1000; no reference server sends 1000), #759 (the 401 challenge names the scheme of the route: `Bearer`, `ApiKey`), #787.
- Response validation: #734, #754, #758 (FastAPI does not validate `-> None`; drop #758 if you prefer that), #795, #838 (a Struct or a generator from a `-> dict` or `-> list` route is now refused), #820.
- Serializers: #691/#713/#829 (read_only as pydantic), #708/#718 (validator modes and override), #817 (`field(alias=)` is the JSON name for input and output; it contradicts the docs text of #695: merge one of the two), #809 (a `DecimalField` is a JSON string), #696/#700 (field options and computed fields in the schema).
- Parameters: #761 (a `dict` parameter on POST is the body), #745/#816 (a `bytes` parameter is the UTF-8 bytes of the text), #742, #802 (`Body()` options work).
- OpenAPI document for client generators: #733 (422 for each route with parameters), #743 (401/403 and security schemes), #739 (`deprecated=`; an operation that is not deprecated has no `deprecated` key), #853, #855 (the null arm of an optional parameter-model field is dropped; close it if you prefer the arm), #719.
- Django middleware: #819 (`process_exception` runs), #827 (`process_response` gets the error response; `get_response()` returns it and does not raise), #807.
- Logging: #841 (a line for each response of a route: more WARNING lines), #846 (sample_rate and min_duration_ms work), #852 (the default `skip_paths` `/health`, `/ready`, `/metrics` now applies), #856.
- Routing: #738 (`mount()` applies the prefix of the parent API), #741, #791 (`include_router(child)` of a child with a parent gives `/p/c/x`), #598, #599.
- Test helpers: #778 (removes `auth_context` of `WebSocketTestClient`), #730, #834.
- Viewsets: #803 (the 422 body of a ViewSet is the body of a function route: first error only), #693.
- Removals: #594, #812, #599.

## Merge order

70 pull requests hold the commits of another pull request (a stack). The list at the end names the parents of each. Merge the parents first; GitHub then shows only the own commit.

Pull requests that hold the merge of two others plus one commit (merge the two parents first):
#830 (#690 + #696), #833 (#661 + #759), #836 (#743 + #759), #837 (#768 + #771), #839 (#768 + #782), #843 (#735 + #762), #849 (#841 + #846), #860 (#782 + #849), #861 (#853 + #855), #862 (#823 + #839), #863 (#788 + #832), #840 (#816 + #703), #845 (#783 + #806).

Conflict resolutions that the sessions worked out (write these lines when you merge by hand):
- #690 with #691, #700 or #713, in `_lazy_collect_field_configs`: `cls.__has_field_markers__ = has_field_markers or bool(cls.__read_only_input_fields__)`.
- #690 with #817, in `_SerializerMeta.__new__`: in the loop of #690, `namespace[field_name] = msgspec.field(name=config.alias)` when the config has an alias, else `del namespace[field_name]`. To keep both blocks as they are loses the alias.
- #735 with #762, WebSocket registration in `api.py`: `route_auth_backends(ws_auth, guards)`.
- #793 with #859: the status check of #793 must use the two range constants of #859.
- The maintainers' #570 is behind `master` and conflicts with `master` itself today, so it was not in the merge check. After its rebase, the pull requests that touch `serializers/base.py` (dump paths), `pagination.py` and `serialization.py` can need a rebase.

Almost every fix adds one line at the end of `### Fixed` in `CHANGELOG.md`. Those conflicts are expected: keep all entries.

## Found and not fixed

- A request with no route (404) and a rejection in Rust (auth, guard, rate limit) do not pass the Django middleware and the 404 has no access-log line. Django runs each middleware for a 404. The change is large (the miss path returns before Python). #808 documents it.
- The response classes keep the `headers=` dict by reference. A copy costs 19 to 35 % for a response with headers (27 to 50 ns), so it was not changed.
- `LoggingConfig.logger_name`, `log_level`, `log_response_body`, `max_body_log_size`, the field lists and the `obfuscate_*` options have no effect on the Rust access log. The docs show `logger_name` as a working option.
- `process_template_response` of a Django middleware does not run (documented in #646/#819). `raise_server_exceptions` of `TestClient` does not raise handler exceptions (docstring corrected in #646).
- `Config.read_only` that names a field of a base class accepts an input value equal to the default (after #829). The fix is not small and needs a check on Python 3.14.
- A plain header list parameter with a default of two or more items keeps `maxItems: 1` in the schema.
- The admin behind a path parameter (`path("<tenant>/admin/", ...)`) cannot be mounted automatically; runbolt prints a warning after #847.
- `openapi/schema_generator.py`: 108 real test gaps are left. `runbolt.py`: 246 mutants not run.
- Exported names with no caller in the library were listed, not removed (public API): `django_bolt.auth` `load_user`, `load_user_sync`, `get_registered_backend`, the default loaders and `register_auth_backend`'s registry; `LoggingMiddleware.log_request`/`log_response`; `websocket.run_websocket_handler`, `is_websocket_handler`; admin `get_admin_route_patterns`, `get_admin_info`; `request.is_request`; `BoltAPI._handler_api_map`; `meta["is_websocket"]`. The list with file and line is in the text of #812.
- Checked and equal to the reference, so no change: float parameters accept `nan`/`inf` (as pydantic); a non-unique `lookup_field` answers 500 (as Django REST framework); an unknown attribute of a serializer `Config` is ignored (as pydantic).

## What was not verified

- No pull request ran the benchmarks (`just save-bench`, CodSpeed) or a load test. Pull requests on a hot path state a `timeit` or a timed loop of the changed code only.
- PostgreSQL and Redis tests were skipped everywhere (no service in the container). The Rust test `bind_listener_binds_an_ipv6_address_in_brackets` was skipped (no IPv6).
- Only Python 3.13 and the installed Django ran. Python 3.12 and 3.14 and Django 5.2 did not.
- Most pull requests did not run the bolt-mcp suite or `python/example`. On `master` the bolt-mcp suite fails under `-n 4`; #813 corrects the cause.
- CI results on GitHub were not checked for the pull requests.
- Some sessions could not install FastAPI or simplejwt and read their source in place of a run (#782 to #808). Each such pull request says so.
- The OpenAPI and RFC sites were not reachable from the sessions. Quotes of the OpenAPI text come from the spec text in `python/django_bolt/openapi/spec/`; RFC 9110, 6455, 3986 and 9457 are cited by section.

## Branch `ccr-7394f596-lyh50r`

It holds only the working notes and the instructions of the worker sessions (`.mutation-session/`). Do not merge it.

## Every pull request, by area

`stack` = merge the named pull requests first. `conflict` = in number order it conflicts with an earlier pull request in the named files (CHANGELOG.md conflicts are not listed; almost every fix has one).


### Admin, nanodjango, shortcuts (7)

- #714 fix(admin): mount the admin at the prefix of the admin site
- #717 fix(nanodjango): keep class views, routers and mount calls in nanodjango convert
- #764 fix(shortcuts): render() sends text/html with the charset
- #772 fix(admin): find an admin site in a nested include or at the root path — stack: #714
- #800 test(admin): the admin mount serves only the admin prefix
- #847 fix(admin): mount the admin for each language of i18n_patterns() — stack: #714, #772
- #857 fix(admin): the admin in i18n_patterns() uses the default language that Django serves — stack: #714, #772, #847

### Authentication (24)

- #600 test(auth): cover the token version in the logout routes and custom model loading by pk
- #603 fix(auth): the pk loader returns None for a pk too large for the database
- #604 fix(auth): Token.decode does no audience check for an empty audience
- #606 fix(auth): keep a revocation entry until exp + leeway
- #607 fix(auth): the logout routes reject a refresh token with no family — conflict: test_jwt_auth_views.py
- #608 fix(auth): rotate_refresh_token rejects claims that are not from a refresh token
- #609 fix(auth): JWTAuthViews verifies with a public key for an asymmetric algorithm
- #628 test(auth): cover the guard check of session requests
- #641 fix(auth): APIKeyAuthentication rejects an unsafe key configuration
- #644 fix(auth): a session guard rejection uses the error format of the API
- #648 fix(auth): request.user of a token with no user answers 401
- #652 fix(auth): the Bearer scheme of a credential has no case
- #655 fix(auth): the session permissions check names get_all_permissions — conflict: test_session_authentication.py
- #661 fix(auth): each 401 from Python has the WWW-Authenticate header
- #759 fix(auth): the WWW-Authenticate header of a 401 names the scheme of the route's backends — stack: #644; conflict: backends.py
- #762 feat(auth): a guarded route with no auth backend uses JWTAuthentication()
- #780 fix(auth): a JWT of an inactive user loads no user — conflict: authentication.md, test_jwt_auth_views.py
- #784 fix(auth): APIKeyAuthentication rejects a key that starts with a scheme — stack: #641
- #787 fix(auth): a failed login of JWTAuthViews has a WWW-Authenticate header — stack: #644, #759; conflict: authentication.md, test_auth_tier1.py, backends.py, compiler.py
- #789 fix(auth): an empty string for an auth backend option is a configuration error
- #792 fix(auth): Token checks its time claims and its extra claims at construction
- #794 fix(auth): JWTAuthViews at the root gives the refresh cookie the path "/"
- #821 fix(auth): create_jwt_for_user refuses an extra claim that replaces a standard claim
- #833 fix(auth): a 401 from Python has the challenge of the route's backends — stack: #644, #661, #759; conflict: responses.rs, test_auth_tier1.py, backends.py, compiler.py

### Class-based views and viewsets (9)

- #645 test(views): cover the write actions, the view context and the rate limit defaults
- #657 fix(views): the write actions do not take the primary key from the body
- #667 fix(views): a lookup value of the wrong type answers 404 — conflict: views.py, test_model_viewset.py
- #673 fix(views): the write actions answer 422 for a body that is not a JSON object — conflict: views.py, test_model_viewset.py
- #674 fix(views): a view method gets self.request for each request position — conflict: views.py
- #684 fix(views): PATCH of a ViewSet works with a renamed serializer — conflict: test_model_viewset.py
- #693 fix(views): the create action of a ViewSet only inserts — conflict: views.py
- #773 test(views): cover custom actions, class settings and pagination of a ViewSet route
- #803 fix(views): the write actions of a ViewSet give the 422 body of a function route — stack: #673; conflict: views.py, test_model_viewset.py

### Concurrency (2)

- #722 test(concurrency): check the arguments and the thread of the thread helpers
- #781 test(concurrency): check the running loop after a blocking user read and the pool sizes — stack: #722

### Dependencies (4)

- #647 fix(dependencies): a cached dependency runs one time under parallel resolution
- #651 fix(dependencies): await a dependency object with async def __call__ — conflict: test_dependency_params.py
- #699 fix(dependencies): refuse a generator dependency and variable parameters at registration — conflict: test_dependency_params.py
- #702 docs(dependencies): Bolt has no API to replace a dependency in a test

### Errors (8)

- #639 test(errors): cover error kinds, problem details and dependency arguments
- #653 fix(exceptions): a str in the first position of an HTTPException is the detail
- #668 fix(errors): an error handler returns its own headers list
- #671 fix(errors): an HTTPException below 400 does not go to the error formatter
- #680 fix(exceptions): a number as a header value of an HTTPException keeps the status — conflict: exceptions.py
- #687 fix(errors): a response validation error of a Struct has the field in its loc
- #798 fix(exceptions): a dict or a list detail of an HTTPException is JSON in the body — conflict: exceptions.py
- #825 fix(errors): problem_details sends a string as "detail" — stack: #798; conflict: exceptions.py

### Logging (7)

- #710 fix(logging): print a record of the django logger one time
- #841 fix(logging): the access log has a line for each response of a route
- #846 fix(logging): the access log applies sample_rate and min_duration_ms — conflict: handler.rs
- #849 fix(logging): the redirect and the OPTIONS answer of a route use its access-log filter — stack: #841, #846
- #852 fix(logging): the access log applies skip_paths and skip_status_codes — stack: #841, #846, #849
- #856 fix(logging): the access log writes one 429 line for each second — stack: #841, #846, #849
- #860 fix(logging): the 405 of a route path writes its access-log line — stack: #782, #841, #846, #849; conflict: handler.rs

### Middleware, CORS, rate limit (27)

- #586 test(middleware): cover ResponseHeaders mapping operations
- #610 test(middleware): cover the response that Python middleware changes
- #618 fix(middleware): read multipart forms in the Django request
- #622 fix(middleware): send a Django streaming response from middleware
- #627 fix(middleware): keep every value of a repeated query key in request.GET
- #643 fix(middleware): give Django middleware the decoded request path — conflict: test_django_middleware_integration.py
- #660 fix(cors): a bad @cors option stops startup
- #664 fix(middleware): a Python middleware can answer with a response object
- #670 fix(middleware): @middleware rejects a form that nothing reads
- #676 fix(middleware): @skip_middleware takes a class and ignores the letter case
- #681 fix(rate-limit): a second @rate_limit on one route raises
- #682 docs(middleware): state the order of route middleware decorators
- #688 fix(rate-limit): the rate limit of the route wins over the limit of the API — conflict: middleware.md
- #694 fix(rate-limit): @rate_limit refuses a key that is not a header name — conflict: middleware.py
- #701 fix(middleware): refuse a middleware class with no __call__ — conflict: middleware.md
- #706 fix(cors): a CORS value that Rust cannot use stops startup — stack: #660
- #712 fix(middleware): a cors or rate_limit dict gets the checks of its decorator — stack: #660, #670, #694; conflict: middleware.md, middleware.py, test_middleware.py
- #725 fix(cors): an empty pattern in CORS_ALLOWED_ORIGIN_REGEXES stops startup — stack: #660, #706
- #729 fix(middleware): a cors or rate_limit dict refuses a key that nothing reads — stack: #660, #670, #694, #712; conflict: middleware.md, middleware.py, test_middleware.py
- #746 fix(middleware): a config decorator above the route decorator stops startup — conflict: middleware.py, pagination.py
- #760 fix(signals): the scope of request_started has the query string and the headers — conflict: test_request_signals.py
- #807 fix(middleware): a Django middleware sees the scheme of the Bolt request — conflict: django_adapter.py
- #808 docs(middleware): say that a request with no route skips the middleware
- #819 fix(middleware): run process_exception of a Django middleware — stack: #646
- #826 fix(middleware): DjangoMiddlewareStack takes the dotted path of a class
- #827 fix(middleware): process_response of a Django middleware gets the error response — stack: #646, #819
- #850 fix(compression): settings.BOLT_COMPRESSION refuses a value of a wrong type — stack: #824

### OpenAPI (32)

- #590 test(openapi): cover the Rapidoc and Stoplight docs pages
- #615 fix(openapi): give each operation a unique operationId
- #672 test(openapi): cover WebSocket, redirect and union example schemas — conflict: test_openapi_metadata.py
- #685 test(openapi): cover the YAML routes, config fields and prefixed docs — conflict: test_openapi_docs.py
- #696 feat(openapi): field() options of a Serializer are in the schema
- #697 fix(openapi): document a response class in the return annotation
- #700 feat(openapi): computed fields of a Serializer are in the response schema — stack: #691; conflict: schema_generator.py, base.py, test_advanced_features.py, test_openapi_schema_accuracy.py
- #703 fix(openapi): document bytes in JSON as base64 text
- #705 fix(openapi): show only multipart/form-data for a route with a file
- #709 fix(openapi): document the media type of a str or bytes return type
- #716 fix(openapi): exclude_paths matches whole path segments
- #719 fix(openapi): keep the null arm of T | None outside a struct field
- #723 fix(openapi): escape the text of the API in the docs pages — conflict: test_openapi_docs.py
- #724 test(openapi): cover responses, request bodies, parameters, tags and security schemes — conflict: test_openapi_schema_accuracy.py
- #727 test(openapi): cover the options of the docs page plugins — conflict: test_openapi_docs.py
- #733 fix(openapi): document the 422 of a route with parameters
- #735 fix(openapi): show the security of a WebSocket route with the default auth backends — conflict: test_openapi_metadata.py
- #739 feat(openapi): deprecated=True marks a route as deprecated — conflict: api.py, test_openapi_metadata.py
- #743 fix(openapi): document the 401 and 403 of a guarded route — conflict: test_openapi_docs.py
- #744 fix(openapi): keep a null field in the example of a tagged union
- #749 test(openapi): cover camelCase schema keywords and enum values of a user schema
- #755 fix(openapi): show the request body of a body that is not a struct
- #767 fix(openapi): document the media type of a str or bytes arm of a response type — stack: #709
- #777 fix(openapi): the path of a docs UI plugin with no leading slash gets one — conflict: middleware.md, test_openapi_docs.py
- #830 fix(openapi): a required Serializer field keeps its field() options in the schema — stack: #690, #696
- #836 test(openapi): the 401 of an API key route has the ApiKey challenge — stack: #644, #743, #759; conflict: responses.rs, test_auth_tier1.py, backends.py, compiler.py, test_openapi_docs.py
- #840 fix(openapi): a bytes parameter is plain text in the schema — stack: #703, #745, #816; conflict: requests.md, extractors.py, schema_generator.py
- #843 fix(openapi): a guarded WebSocket route with no auth backend shows the JWT default — stack: #735, #762; conflict: test_openapi_metadata.py
- #853 fix(openapi): a Header() or Cookie() model is one parameter for each field — stack: #703, #745, #816, #840; conflict: requests.md, extractors.py, schema_generator.py
- #855 fix(openapi): an optional field of a parameter model has the schema of an optional parameter — stack: #719; conflict: test_openapi_schema_accuracy.py
- #858 fix(openapi): a list parameter with an empty default has no minItems
- #861 fix(openapi): an optional field of a header or cookie model has the schema of its type — stack: #703, #719, #745, #816, #840, #853, #855; conflict: requests.md, extractors.py, schema_generator.py, test_openapi_schema_accuracy.py

### Other (5)

- #594 refactor: remove three definitions that nothing uses
- #595 test: cover the health route paths and the error line of a JSON decode error
- #597 test: cover @action options, route names for reverse(), and the param function arguments
- #646 docs: say what raise_server_exceptions and the Django hooks do
- #812 refactor: remove definitions and branches that nothing reaches — conflict: extractors.py, base.py, websocket.py

### Pagination (14)

- #602 test(pagination): cover boundary values, list results, build_response and the request lookup
- #611 fix(pagination): a URL-safe cursor, and no 500 for a cursor with a wrong value
- #612 fix(pagination): a page or an offset beyond the data gives an empty page
- #613 fix(pagination): @paginate finds the request of each handler — conflict: pagination.py
- #614 fix(pagination): @paginate accepts a sync handler
- #617 fix(pagination): CursorPagination names the QuerySet that it needs
- #621 fix(pagination): max_page_size limits the default page size
- #625 fix(pagination): the total of a sequence is its len()
- #629 fix(pagination): a cursor by primary key reads a values() row by each name
- #633 fix(pagination): a cursor orders by a ForeignKey or by a deferred field
- #638 docs(pagination): a cursor page has no previous cursor
- #642 fix(pagination): a cursor integer out of the database range is no cursor — stack: #611
- #726 fix(pagination): a page of a plain Struct type validates its items — conflict: pagination.py, test_pagination.py
- #796 fix(pagination): a page of a Serializer type validates its items — stack: #726; conflict: pagination.py, test_pagination.py

### Parameters and request bodies (19)

- #616 fix(params): a repeated form field that is not a sequence keeps its last value
- #620 fix(params): two body parameters each decode the body as their own type
- #624 fix(params): an Enum or Literal parameter converts and validates its value
- #631 fix(params): a request with no body gives the default of an optional body parameter
- #636 fix(typing): a list of structs with no marker reads the JSON body — conflict: requests.md
- #650 test(kwargs): cover injector patterns and the HEAD/OPTIONS body check
- #654 fix(params): a sequence field of a header model takes the header value
- #659 fix(params): a parameter model that forbids unknown fields ignores the keys of other parameters
- #663 fix(params): an optional parameter model takes its default — conflict: requests.md, extractors.py
- #720 test(typing): check the source of a parameter that has no marker
- #742 fix(params): a bytes file parameter gets the content of the file
- #745 fix(params): a bytes parameter gets the UTF-8 bytes of its text value — conflict: requests.md
- #761 fix(params): a dict parameter with no marker reads the JSON body
- #788 fix(params): a parameter type that Bolt cannot convert stops the registration — stack: #761; conflict: requests.md
- #802 fix(params): the options of Body() have an effect — stack: #631; conflict: extractors.py, schema_generator.py
- #816 fix(params): a bytes field of a parameter model gets the UTF-8 bytes of its text value — stack: #745; conflict: requests.md, extractors.py
- #828 feat(request): add request.scheme and request.is_secure() — stack: #807; conflict: django_adapter.py
- #832 fix(params): a timedelta parameter converts its value — stack: #624
- #863 fix(params): the hint of the parameter type error names timedelta — stack: #624, #761, #788, #832

### Responses and JSON (19)

- #601 test(responses): cover SSE framing with fields, cookie defaults and the media type of a file
- #605 fix(sse): format_sse_event frames bytes data with a line break
- #649 fix(handler): a response that Rust cannot build hides its cause from the client
- #662 fix(core): a response status that is not an HTTP status is a 500
- #734 fix(responses): send a scalar handler result as JSON
- #754 fix(responses): a response type validates a str or bytes result — stack: #734
- #757 test(serialization): cover response objects behind Django middleware, and mark equivalent mutants
- #758 fix(responses): a -> None route validates its result — stack: #734
- #769 fix(json): a subclass of Decimal, datetime or time encodes as its base type
- #793 fix(core): a response status is from 200 to 599 — stack: #662
- #795 fix(responses): dict, list and list[Serializer] refuse a result of another kind — stack: #734, #754
- #804 fix(cookies): an unknown samesite value of a cookie raises ValueError
- #815 fix(json): a subclass of int, float, bytes or timedelta encodes as its base type — stack: #769
- #818 fix(responses): a response_model dict with no 2xx code keeps the default status 200
- #820 fix(responses): a route with the response type dict can return a response object — conflict: api.py, test_handler_executors.py
- #838 fix(responses): a dict or list response type refuses each result of another kind — stack: #734, #754, #795
- #842 fix(json): a subclass of bytearray encodes as its base type — stack: #769, #815
- #848 fix(responses): a (status, response object) tuple sends the response object with the tuple status — stack: #662, #793; conflict: serialization.py
- #854 fix(json): a deque, a generator, a Fraction or a compiled pattern in a response is JSON — stack: #769, #815, #842; conflict: _json.py

### Routing and API registration (42)

- #588 test(router): cover the Router method decorators and ActionHandler
- #596 test(router): cover the router defaults, nested prefixes and the middleware chain
- #598 fix(router): include_router applies its prefix to routes added before the call
- #599 fix(router): Router does not accept dependencies that it never ran
- #626 fix(api): mount("/") serves the routes of the mounted API
- #630 fix(api): run the lifespan of a mounted API
- #637 docs(lifespan): read the lifespan state from the API object
- #665 fix(api): the arguments of @api.viewset() apply to the @action routes
- #683 fix(api): the lookup_field of @api.viewset() reaches get_object() — conflict: api.py, test_model_viewset.py
- #686 fix(api): get_serializer_class() can read self.action at registration
- #707 fix(api): api.viewset() registers a sync standard action
- #721 test(api): cover registration paths that mutation testing found untested
- #736 fix(router): a second handler for the method and path of a route stops startup
- #737 fix(router): a route path with a space or a non-ASCII character matches its requests
- #738 fix(api): mount() applies the prefix of the parent API — conflict: api.py
- #740 fix(api): resolve handler annotations in the globals of the handler
- #741 fix(api): a path or a prefix with no leading slash gets one — conflict: api.py, test_prefix_and_mount.py
- #747 fix(api): an object with __call__ is a route handler, and a partial gets a clear error — conflict: api.py, test_decorator_syntax.py
- #750 fix(api): mount() refuses an API that it cannot serve
- #751 fix(api): a path with one parameter name two times stops registration — conflict: api.py
- #752 fix(api): BoltAPI(trailing_slash=...) refuses an unknown mode
- #753 fix(api): viewset paths join with one slash, and view() refuses an empty methods list
- #756 fix(asgi): give an HTTP ASGI mount the percent-decoded path
- #763 test(api): cover WebSocket registration and the Django mount helpers
- #765 test(api): cover mount(), include_router() and mounted native routes — conflict: test_prefix_and_mount.py
- #768 fix(router): a request path matches its route for each form of an escape — stack: #737
- #770 test(api): cover the route metadata and the arguments of each method decorator — stack: #721
- #782 fix(router): a wrong method on a route path answers 405 with Allow
- #785 fix(api): register an @action method with the name of an HTTP method or standard action — conflict: test_viewset.py
- #791 fix(router): a router with a parent registers one time — stack: #598
- #797 fix(router): a nested Router inherits auth, guards and tags of its parent — conflict: router.py
- #801 fix(api): a view decorator on a handler with no request parameter stops registration — conflict: middleware.md, api.py
- #810 fix(api): the status_code of a ViewSet class applies to the @action routes — stack: #665
- #814 fix(api): a bool key of response_model stops registration
- #823 fix(router): a wrong method on the other slash form of a route path answers 405 — stack: #782; conflict: handler.rs
- #824 fix(api): BoltAPI(compression=...) refuses a value of a wrong type
- #835 test(api): cover the default docs pages, the compression setting and the request of a function middleware — conflict: test_compression.py
- #839 fix(router): a wrong method answers 405 for each form of a %XX escape — stack: #737, #768, #782
- #844 test(api): cover the executors that mutation testing found untested
- #851 fix(api): a response_model key that is not an integer or `...` stops registration — stack: #814
- #859 fix(api): a response_model key outside 200 to 599 stops registration — stack: #814, #851
- #862 test(router): a wrong method on the other slash form of an escaped path answers 405 — stack: #737, #768, #782, #823, #839; conflict: handler.rs

### Rust tests (2)

- #669 test(rust): kill the mutants that cargo-mutants missed in its configured files
- #675 test(rust): unit tests for settings bounds, lanes, static files and error responses

### Serializers (26)

- #587 test(serializers): cover SerializerView chaining and JSON output, and Serializer.validate
- #592 fix(serializers): exclude_unset leaves out the fields that hold msgspec.UNSET
- #619 fix(serializers): dump a serializer in a dict, tuple or set field
- #623 fix(serializers): subset() keeps each validator of a field
- #634 fix(serializers): dump() with rename or tag keeps the Python values
- #635 fix(serializers): model_validate() raises RequestValidationError for a validator error
- #640 fix(serializers): pattern types reject a value with a line break at its end
- #656 test(serializers): cover dump options, write-only, update_instance and error lists of base.py — conflict: test_base.py
- #658 test(serializers): cover fields, helpers, loading and nested behavior that no test checked
- #666 fix(serializers): exclude_defaults drops a field with a mutable default
- #677 fix(serializers): create_serializer() keeps the order of the fields
- #678 fix(serializers): a null BooleanField gives bool | None in create_serializer() — conflict: test_choices_fields.py
- #679 fix(serializers): a validator error of a renamed serializer has the wire name in loc
- #689 fix(serializers): subset() keeps the struct options of the parent
- #690 fix(serializers): a required field with field() has the error of a required field
- #691 fix(serializers): input does not set a read_only field — conflict: base.py, test_advanced_features.py
- #692 fix(serializers): model_validate() lists each value that the conversion rejects — conflict: test_base.py
- #695 docs(serializers): field(alias=) says what the alias does
- #708 fix(serializers): mode= of field_validator and model_validator has an effect
- #711 fix(serializers): Config.validators is an error, not a silent no-op
- #713 fix(serializers): a parameter model does not set a read_only field — stack: #691; conflict: extractors.py, schema_generator.py, base.py, test_advanced_features.py
- #718 fix(serializers): a subclass replaces a validator that it defines again — stack: #708
- #809 fix(serializers): create_serializer() maps a ForeignKey and a DecimalField to types that work — conflict: serializers.md, base.py, test_django_integration.py
- #817 fix(serializers): field(alias=...) is the name of the field in JSON — conflict: base.py, fields.py
- #822 fix(serializers): subset() keeps the model validators that it can run — conflict: base.py, test_advanced_features.py
- #829 fix(serializers): Config.read_only with a plain default is the same as field(read_only=True) — stack: #691; conflict: extractors.py, schema_generator.py, base.py, test_advanced_features.py

### Test clients (9)

- #589 test(testing): cover WebSocketTestClient.close and cursor iteration on the shared connection
- #591 fix(testing): WebSocketTestClient iterators give the messages sent before the server close
- #632 fix(testing): read multi-byte text in streamed test responses
- #704 fix(testing): ConnectionClosed of WebSocketTestClient has the close reason
- #730 fix(testing): the handler gets the disconnect when WebSocketTestClient exits — conflict: test_websocket_lifecycle.py
- #778 fix(testing): remove the auth_context option of WebSocketTestClient
- #779 fix(testing): WebSocketTestClient gives the close reason of a handler error — stack: #704
- #811 fix(testing): WebSocketTestClient refuses a send after the close — stack: #778; conflict: test_websocket_test_client_parity.py
- #834 fix(testing): the test clients remove the spaces around a request header value

### WebSocket (13)

- #715 docs(websocket): correct the test client example and the close code name
- #728 fix(websocket): a WebSocket route takes an async dependency
- #732 fix(websocket): register a WebSocket route path without its trailing slash
- #748 test(websocket): cover message modes, close messages and a client that left
- #766 fix(websocket): close() refuses a code or a reason that makes an invalid close frame
- #771 fix(websocket): a WebSocket route path with a space or a non-ASCII character accepts connections — stack: #737; conflict: test_websocket.py
- #774 fix(websocket): @api.websocket() takes a callable object and a bound method — stack: #747; conflict: api.py, test_decorator_syntax.py, test_websocket.py
- #776 fix(websocket): a handler that returns closes the connection with 1000 — conflict: test_websocket_lifecycle.py
- #783 fix(websocket): a raised WebSocketException closes the connection with its code and reason — stack: #776; conflict: websocket.md, websocket.py, test_websocket_lifecycle.py
- #786 fix(websocket): receive_text() and receive_bytes() raise for a frame of the other type — conflict: types.py, test_websocket_lifecycle.py
- #806 fix(websocket): a close message takes None for the reason, and reserved close codes are refused — stack: #766
- #837 fix(websocket): a WebSocket request path matches for each form of a %XX escape — stack: #737, #768, #771; conflict: test_websocket.py
- #845 fix(websocket): WebSocketException refuses a code or a reason that makes an invalid close frame — stack: #766, #776, #783, #806; conflict: websocket.py, test_websocket_lifecycle.py

### bolt-mcp tests (1)

- #813 test(bolt-mcp): give each pytest-xdist worker its own database file

### runbolt, workers, dev reload (7)

- #698 fix(runbolt): stop for an option value out of range
- #731 test(dev-reload): cover a watcher error in the supervisor loop
- #775 fix(runbolt): --workers-kill-timeout works with one process
- #790 test(workers): cover the /proc RSS reader and the limit boundaries
- #799 test(runbolt): cover the option warnings, --backlog, --no-admin, merge and autodiscovery — stack: #698
- #805 test(dev-reload): cover a failed first start in the supervisor loop — stack: #731
- #831 test(runbolt): cover MCP mounts, conflicts and broken BOLT_API entries in the merge — stack: #698, #799
