# Native Routes

!!! warning "Experimental"
    The API and the ABI can change.

A native route sends a request to a Rust handler in another extension module. The request does not go into Python. Use a native route for a hot endpoint that you wrote in Rust.

Users of a native route do not need Rust. The handler comes in a compiled wheel, as Bolt does. Only the author of the handler needs a Rust toolchain.

## Register a native route

The extension module gives a `PyCapsule` named `django_bolt.native_handler.v1`. Give the capsule to `native_route()`:

```python
from django_bolt import BoltAPI

import my_extension

api = BoltAPI()

api.native_route("POST", "/api/{project_id}/envelope", my_extension.envelope_handler())
```

A native route is a route of the normal router. The path syntax, the trailing slash rule, the mount prefix and the automatic `OPTIONS` response are the same as for a Python route. A static route comes before a route with a path parameter, also between a native route and a Python route.

Bolt checks the capsule at startup. These stop startup:

- A capsule with another ABI version, or with no `call` function.
- A native route with the method and path of another route.
- A native route at the path of an ASGI mount or an MCP mount.

## What runs for a native route

Bolt does these steps:

- Apply `BOLT_MAX_HEADER_SIZE` and the limit of 100 headers.
- Read the full body. A body larger than `BOLT_MAX_UPLOAD_SIZE` gets 413. As for a Python route, no timeout applies while the body arrives.
- Resolve the client address with `BOLT_TRUSTED_PROXIES`.
- Apply the global CORS configuration and compression.
- Send 504 when the handler does not complete within `BOLT_NATIVE_ROUTE_TIMEOUT`. This does not stop the handler, so the handler must limit its own work.

Bolt does not do these steps:

- Python middleware, authentication, guards and rate limits.
- Dependency injection and validation.
- The access log, because it calls the Python logger.
- An OpenAPI entry for the route.

The handler must do the checks that it needs.

## Write a handler

The `bolt-native-abi` crate defines the contract. It has no dependencies. Only `#[repr(C)]` data and `extern "C"` functions cross between Bolt and the handler. Thus the handler can use another Rust version, another Tokio runtime and another allocator.

Write the handler with the `handler!` macro. The closure gets a `RequestRef`, a safe view of the request, and a `Responder`, which completes the request exactly once. If you drop the `Responder` without a response, it sends 500. The macro also catches a panic in the closure and sends 500.

```rust
use bolt_native_abi::{handler, RequestRef, Responder, CAPSULE_NAME};
use pyo3::prelude::*;

handler!(static ENVELOPE = c"envelope", |req: RequestRef<'_>, responder: Responder| {
    // Copy what you need. The request is valid only during the call.
    let project_id = req.path_param(b"project_id").map(<[u8]>::to_vec);
    let body = req.body().to_vec();
    // Give the work to your own runtime. Do not block here.
    runtime().spawn(async move {
        let (status, json) = ingest(project_id, body).await;
        responder.send(status, &[(b"content-type", b"application/json")], &json);
    });
});

#[pyfunction]
fn envelope_handler(py: Python<'_>) -> PyResult<Py<PyAny>> {
    unsafe {
        let capsule = pyo3::ffi::PyCapsule_New(
            std::ptr::from_ref(&ENVELOPE).cast_mut().cast(),
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
- The request gives all headers, also `authorization` and `cookie`. Path parameters are percent-decoded one time. The path and the query are not decoded.
- Bolt sends 500 for a status outside 200-599. It drops a response header that is not valid HTTP and logs it. The response body is buffered.
- A native route for `GET` does not answer `HEAD`. Register `HEAD` too if you need it.

## Test a native route

`TestClient` and `AsyncTestClient` serve native routes as `runbolt` does:

```python
from django_bolt.testing import TestClient

with TestClient(api) as client:
    response = client.post("/api/1/envelope", content=b"...")
```

A handler that uses its own database connection does not see the transaction of a Django `TestCase`. Use `TransactionTestCase` for those tests.
