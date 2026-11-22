---
description: Serve a route with a Rust handler from your own extension module. The request does not go into Python.
---

# Native routes

A native route sends a request to a Rust handler in a separate extension module.
The request does not go into Python, so it does not wait for the GIL.
Use it for a small number of hot routes. Keep the rest of the API in Python.

Register the handler with `api.native_route(method, path, capsule)`:

```python title="blog/api.py"
import article_tools  # your Rust extension module

from django_bolt import BoltAPI

api = BoltAPI()

api.native_route("POST", "/native/word-count", article_tools.word_count_handler())
api.native_route("GET", "/native/articles/{slug}/title", article_tools.article_title_handler())
```

```bash
curl -X POST --data "Bolt serves this route in Rust" localhost:8000/native/word-count
```

```json
{"words":6}
```

The users of your project do not need Rust.
You ship the handler in a compiled wheel, as Bolt does.
Only the author of the handler needs a Rust toolchain.

## Write the handler

The `bolt-native-abi` crate defines the contract between Bolt and the handler.
It has no dependencies. Only `#[repr(C)]` data and `extern "C"` functions cross between the two sides.
Thus the handler can use a different Rust version, Tokio runtime, allocator and PyO3 version.

```toml title="Cargo.toml"
[package]
name = "article_tools"
version = "0.1.0"
edition = "2024"

[lib]
crate-type = ["cdylib"]

[dependencies]
bolt-native-abi = "0.1"
pyo3 = { version = "0.30", features = ["abi3-py312"] }
tokio = { version = "1", features = ["rt-multi-thread"] }
```

```rust title="src/lib.rs"
use std::sync::OnceLock;

use bolt_native_abi::{CAPSULE_NAME, HandlerV1, RequestRef, Responder, handler};
use pyo3::prelude::*;
use tokio::runtime::Runtime;

// Count the words of the request body. The work is short, so the
// handler sends the response before it returns.
handler!(static WORD_COUNT = c"word_count", |req: RequestRef<'_>, responder: Responder| {
    let words = String::from_utf8_lossy(req.body()).split_whitespace().count();
    let body = format!("{{\"words\":{words}}}");
    responder.send(200, &[(b"content-type", b"application/json")], body.as_bytes());
});

// Start the runtime on the first request, in the worker process.
fn runtime() -> &'static Runtime {
    static RUNTIME: OnceLock<Runtime> = OnceLock::new();
    RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .expect("cannot start the Tokio runtime")
    })
}

async fn find_title(slug: &[u8]) -> Option<&'static str> {
    match slug {
        b"hello-bolt" => Some("Hello Bolt"),
        _ => None,
    }
}

handler!(static ARTICLE_TITLE = c"article_title", |req: RequestRef<'_>, responder: Responder| {
    // Copy what you need. The request is valid only during the call.
    let slug = req.path_param(b"slug").unwrap_or_default().to_vec();
    // Give the work to your runtime, and return at once.
    runtime().spawn(async move {
        match find_title(&slug).await {
            Some(title) => responder.send(200, &[(b"content-type", b"text/plain")], title.as_bytes()),
            None => responder.send(404, &[(b"content-type", b"text/plain")], b"Not found"),
        }
    });
});

fn capsule(py: Python<'_>, handler: &'static HandlerV1) -> PyResult<Py<PyAny>> {
    // SAFETY: the handler is a static, and the name is a static C string.
    unsafe {
        let capsule = pyo3::ffi::PyCapsule_New(
            std::ptr::from_ref(handler).cast_mut().cast(),
            CAPSULE_NAME.as_ptr(),
            None,
        );
        Ok(Bound::from_owned_ptr_or_err(py, capsule)?.unbind())
    }
}

#[pyfunction]
fn word_count_handler(py: Python<'_>) -> PyResult<Py<PyAny>> {
    capsule(py, &WORD_COUNT)
}

#[pyfunction]
fn article_title_handler(py: Python<'_>) -> PyResult<Py<PyAny>> {
    capsule(py, &ARTICLE_TITLE)
}

#[pymodule]
fn article_tools(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(word_count_handler, m)?)?;
    m.add_function(wrap_pyfunction!(article_title_handler, m)?)?;
    Ok(())
}
```

Build and install the module with [maturin](https://www.maturin.rs/), for example `maturin develop --release`.

```bash
curl localhost:8000/native/articles/hello-bolt/title
```

```text
Hello Bolt
```

The `handler!` macro writes the `extern "C"` function for you.
The closure gets two values:

- `RequestRef`: a safe view of the request. It has `method()`, `path()`, `query()`, `headers()`, `header(name)`, `path_param(name)`, `client_addr()`, `scheme()` and `body()`. Each returns bytes.
- `Responder`: completes the request one time with `send(status, headers, body)`. You can call `send()` from any thread.

The closure cannot keep the request after it returns. The compiler checks this.
When you drop the `Responder` without `send()`, Bolt sends and logs a `500`.
The macro catches a panic in the closure, which also gives a `500`.
With `panic = "abort"` in the build profile of the extension, a panic stops the process.

## Follow the rules of the contract

- Bolt calls the handler on many threads at the same time. Do not block in the handler.
- Give slow work, such as I/O, to your own runtime, and return at once.
- Start threads and runtimes lazily, on the first request. With `runbolt --processes N`, a thread that starts at import time is not in the worker processes.
- Copy the request data that you need. Each request pointer is valid only during the call.
- Complete each request one time. A `Responder` does this for you.
- A status outside 200 to 599 gives `500`. Bolt drops a response header that is not valid HTTP and logs it.

Bolt buffers the response body. Bolt sends no body for `HEAD`, 204, 205 and 304.

## Know what Bolt does for a native route

Bolt does these steps:

- Matches the route as it matches a Python route. The trailing slash rule, the API `prefix`, the mount path and the automatic `OPTIONS` response are the same.
- Applies `BOLT_MAX_HEADER_SIZE` and the limit of 100 headers. A bad request gets `400`.
- Reads the full body. A body larger than `BOLT_MAX_UPLOAD_SIZE` gets `413`.
- Finds the client address and the scheme with `BOLT_TRUSTED_PROXIES`.
- Applies the global CORS settings and compression.
- Sends `504` when the handler does not complete within `BOLT_NATIVE_ROUTE_TIMEOUT` seconds. The default is 30.

Bolt does not do these steps:

- Python middleware, authentication, guards and rate limits.
- Parameter conversion, validation and dependency injection.
- The access log, because it uses the Python logger.
- An entry in the OpenAPI schema.

!!! warning
    A native route has no authentication and no guards. The handler must check the credential that it needs, for example the `authorization` header.

The `error_formatter` of the API formats the errors that Bolt makes: `400`, `413`, `500` and `504`.
It does not change a response of the handler.

A timeout does not stop the handler. The handler continues, so it must limit its own work.
A `send()` after the timeout has no effect.

## Read the request

- The request has each header, with a lower-case name, also `authorization` and `cookie`. A value can contain bytes that are not UTF-8.
- `path()` is the full request path, with the API prefix and the mount path. It is still percent-encoded.
- `query()` is the query string without `?`. It is still percent-encoded.
- `path_param(name)` is percent-decoded one time. `%2F` becomes `/`.

## Write the route path

The path syntax is the same as for a Python route, with one rule:
Bolt does not check the type of a path parameter for a native route.
Thus `native_route()` raises `ValueError` for a typed parameter such as `{id:int}` or `{id:str}`.
Use `{id}`, and check the value in the handler.
`{rest:path}` is valid. It matches the rest of the path, with its slashes.

`native_route()` also raises in these cases:

| Case | Error |
| --- | --- |
| The method is not an HTTP method that Bolt supports | `ValueError` |
| The object is not a `django_bolt.native_handler.v1` capsule | `TypeError` |
| The capsule is smaller than `HandlerV1`, or it has no `call` function | stops startup |
| Another route, an ASGI mount or an MCP mount has the same method and path | stops startup |

A native `GET` route does not answer `HEAD`. Register `HEAD` too if you need it.

## Test a native route

`TestClient` and `AsyncTestClient` serve native routes as `runbolt` does:

```python title="blog/tests.py"
from django_bolt.testing import TestClient

from blog.api import api


def test_word_count():
    with TestClient(api) as client:
        response = client.post("/native/word-count", content=b"Bolt serves this route in Rust")
    assert response.status_code == 200
    assert response.json() == {"words": 6}
```

A handler with its own database connection does not see the transaction of a Django `TestCase`.
Use `TransactionTestCase` for those tests.

## Change to a later version of the contract

Each struct of the contract starts with `size`.
A later `bolt-native-abi` version adds fields only at the end.
Thus a handler and Bolt can use different minor versions.
`handler!` and `Responder` set `size` for you.
A change that is not compatible gets a new capsule name, for example `django_bolt.native_handler.v2`.

## See also

- [Routing](routing.md)
- [BoltAPI reference](../ref/api.md)
- [How it works](../architecture.md)
- [Benchmarks](../benchmarks.md)
