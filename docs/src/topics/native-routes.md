---
icon: lucide/zap
---

# Native Routes

A native route sends a request to a Rust handler in another extension module. The request does not go into Python, so it does not wait for the GIL and has no Python overhead. For example, write a hot endpoint in Rust so that it runs efficiently, and keep the rest of your API in Python.

Users of a native route do not need Rust. The handler comes in a compiled wheel, as Bolt does. Only the author of the handler needs a Rust toolchain.

## Register a native route

The extension module gives a `PyCapsule` named `django_bolt.native_handler.v1`. Give the capsule to `native_route()`:

```python
from django_bolt import BoltAPI

import my_extension

api = BoltAPI()

api.native_route("GET", "/native/users/{id}", my_extension.user_handler())
```

A native route is a route of the normal router. The path syntax, the trailing slash rule, the mount prefix and the automatic `OPTIONS` response are the same as for a Python route. A static route matches before a route with a path parameter. This is also true when one of the routes is native.

Bolt does not check the type of a path parameter for a native route. Thus `native_route()` refuses each type other than `path`, also `{id:str}`. This rule also applies to the API prefix and the mount path. Use `{id}`, and check the value in the handler. `{rest:path}` is valid. It matches the rest of the path, with its slashes.

`native_route()` checks the capsule. These stop startup:

- A capsule with another name, a capsule smaller than `HandlerV1`, or a capsule with no `call` function.
- A native route with the method and path of another route.
- A native route at the path of an ASGI mount or an MCP mount.

## What runs for a native route

Bolt does these steps:

- Apply `BOLT_MAX_HEADER_SIZE` and the limit of 100 headers. The size limit applies to each raw value, also a value that is not ASCII.
- Read the full body. A body larger than `BOLT_MAX_UPLOAD_SIZE` gets 413. As for a Python route, no timeout applies while the body arrives.
- Resolve the client address and the scheme with `BOLT_TRUSTED_PROXIES`.
- Apply the global CORS configuration and compression. A native route cannot turn off the global CORS configuration.
- Send 504 when the handler does not complete within `BOLT_NATIVE_ROUTE_TIMEOUT`. This does not stop the handler, so the handler must limit its own work. A handler must complete each request one time, also after the 504. If it does not, the request token of Bolt stays in memory until the process stops.

Bolt does not do these steps:

- Python middleware, authentication, guards and rate limits.
- Dependency injection and validation.
- The access log, because it calls the Python logger.
- An OpenAPI entry for the route.

The handler must do the checks that it needs.

The `error_formatter` of the API formats the errors that Bolt makes for a native route: 400, 413, 500 and 504. It does not change a response of the handler, also when its status is an error.

## Write a handler

The `bolt-native-abi` crate defines the contract. It has no dependencies. Only `#[repr(C)]` data and `extern "C"` functions cross between Bolt and the handler. Thus the handler can use another Rust version, another Tokio runtime and another allocator.

Write the handler with the `handler!` macro. The closure gets a `RequestRef`, a safe view of the request, and a `Responder`, which completes the request exactly once. If you drop the `Responder` without a response, Bolt sends and logs a 500. The macro also catches a panic in the closure, which gives the same 500. With `panic = "abort"` in the profile of the extension, a panic stops the process.

```rust
use bolt_native_abi::{handler, RequestRef, Responder, CAPSULE_NAME};
use pyo3::prelude::*;

handler!(static USER = c"user", |req: RequestRef<'_>, responder: Responder| {
    // Copy what you need. The request is valid only during the call.
    let id = req.path_param(b"id").map(<[u8]>::to_vec);
    // Give the work to your own runtime. Do not block here.
    runtime().spawn(async move {
        let (status, json) = find_user(id).await;
        responder.send(status, &[(b"content-type", b"application/json")], &json);
    });
});

#[pyfunction]
fn user_handler(py: Python<'_>) -> PyResult<Py<PyAny>> {
    unsafe {
        let capsule = pyo3::ffi::PyCapsule_New(
            std::ptr::from_ref(&USER).cast_mut().cast(),
            CAPSULE_NAME.as_ptr(),
            None,
        );
        Ok(Bound::from_owned_ptr_or_err(py, capsule)?.unbind())
    }
}
```

Rules for the handler:

- Bolt calls the handler on many threads at the same time. Do not block in it.
- Start threads and runtimes lazily, in the process that serves requests. With `runbolt --processes N`, a thread that started at import time is not in the worker processes.
- The request gives all headers, also `authorization` and `cookie`. A header value can have bytes that are not UTF-8; Python routes do not get such a header.
- The path is the full request path, with the API prefix and the mount path. Use the path parameters to read the parts of the path.
- Path parameters are percent-decoded one time. A value that does not decode to UTF-8 stays encoded, as for Python routes. Bolt does not limit the length of a path parameter; the URL limit applies. The path and the query are not decoded.
- Bolt sends 500 for a status outside 200-599. It drops a response header that is not valid HTTP and logs it. The response body is buffered. Bolt sends no body for `HEAD`, 204, 205 and 304.
- Each ABI struct starts with `size`. A later version of `bolt-native-abi` adds fields only at the end, so a handler and Bolt can use different minor versions. `handler!` and `Responder` set `size` for you.
- A native route for `GET` does not answer `HEAD`. Register `HEAD` too if you need it.

## Test a native route

`TestClient` and `AsyncTestClient` serve native routes as `runbolt` does:

```python
from django_bolt.testing import TestClient

with TestClient(api) as client:
    response = client.get("/native/users/42")
```

A handler that uses its own database connection does not see the transaction of a Django `TestCase`. Use `TransactionTestCase` for those tests.
