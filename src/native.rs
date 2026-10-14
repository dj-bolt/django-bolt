//! Native routes: Rust handlers in other extension modules, called through the
//! `bolt-native-abi` C ABI. A native route is a route kind of the normal
//! router, so it matches, redirects and answers OPTIONS as a Python route
//! does. The request does not go into Python.
use std::ffi::{c_void, CStr};

use actix_web::http::header::{HeaderName, HeaderValue};
use actix_web::http::StatusCode;
use actix_web::{web, HttpRequest, HttpResponse};
use ahash::AHashMap;
use bolt_core::middleware::client_ip;
use bolt_core::responses;
use bolt_core::router::{NativeHandler, Router};
use bolt_core::state::AppState;
use bolt_native_abi as abi;
use pyo3::prelude::*;
use tokio::sync::oneshot;

/// `(method, path, capsule)` as Python passes a native route.
pub type NativeRouteSpec = (String, String, Py<PyAny>);

/// Build the router of an app: the Python routes, then the native routes.
#[expect(
    clippy::type_complexity,
    reason = "PyO3 wire format: Python passes each route as one tuple"
)]
pub fn build_router(
    py: Python<'_>,
    routes: Vec<(String, String, usize, Py<PyAny>, Py<PyAny>, Py<PyAny>)>,
    native_routes: Vec<NativeRouteSpec>,
) -> PyResult<Router> {
    let mut router = Router::new();
    for (method, path, handler_id, handler, dispatch, dispatch_sync) in routes {
        router.register(&method, &path, handler_id, handler, dispatch, dispatch_sync)?;
    }
    for (method, path, capsule) in native_routes {
        let native = handler_from_capsule(py, &capsule)?;
        router.register_native(py, &method, &path, capsule, native)?;
    }
    Ok(router)
}

fn handler_from_capsule(py: Python<'_>, capsule: &Py<PyAny>) -> PyResult<NativeHandler> {
    use pyo3::exceptions::{PyTypeError, PyValueError};
    let obj = capsule.bind(py).as_ptr();
    let name = abi::CAPSULE_NAME.as_ptr();
    // SAFETY: `obj` is alive while `capsule` is bound.
    let ptr = unsafe {
        if pyo3::ffi::PyCapsule_IsValid(obj, name) != 1 {
            return Err(PyTypeError::new_err(
                "expected a django_bolt.native_handler.v1 capsule",
            ));
        }
        pyo3::ffi::PyCapsule_GetPointer(obj, name) as *const abi::HandlerV1
    };
    if ptr.is_null() {
        return Err(PyValueError::new_err("native handler capsule holds NULL"));
    }
    // SAFETY: the capsule name guarantees the v1 layout, and the contract
    // requires the struct to be static. CPython does not unload extension
    // modules.
    let handler: &'static abi::HandlerV1 = unsafe { &*ptr };
    let call = handler
        .call
        .ok_or_else(|| PyValueError::new_err("native handler has no call function"))?;
    Ok(NativeHandler { handler, call })
}

fn handler_name(native: NativeHandler) -> String {
    if native.handler.name.is_null() {
        return "<unnamed>".to_owned();
    }
    // SAFETY: the contract requires a NUL-terminated, static name.
    unsafe { CStr::from_ptr(native.handler.name) }
        .to_string_lossy()
        .into_owned()
}

struct OwnedResponse {
    status: u16,
    headers: Vec<(Vec<u8>, Vec<u8>)>,
    body: Vec<u8>,
}

/// The `complete` callback. It can run on any thread, so it only copies the
/// response and sends it on a oneshot channel.
unsafe extern "C" fn complete(token: *mut c_void, resp: *const abi::Response) {
    if token.is_null() {
        return;
    }
    // SAFETY: the contract gives back the token of one `call`, exactly once.
    let tx = unsafe { Box::from_raw(token.cast::<oneshot::Sender<OwnedResponse>>()) };
    if resp.is_null() {
        // The contract: a null response sends 500. Dropping the sender does it.
        return;
    }
    // SAFETY: `resp` and its pointers are valid during this call.
    let resp = unsafe { &*resp };
    let pairs: &[abi::Pair] = if resp.n_headers == 0 || resp.headers.is_null() {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(resp.headers, resp.n_headers) }
    };
    let headers = pairs
        .iter()
        .map(|p| unsafe { (p.name.as_bytes().to_vec(), p.value.as_bytes().to_vec()) })
        .collect();
    // An error means that the request is gone (client or timeout).
    let _ = tx.send(OwnedResponse {
        status: resp.status,
        headers,
        body: unsafe { resp.body.as_bytes().to_vec() },
    });
}

pub async fn dispatch(
    req: &HttpRequest,
    mut payload: web::Payload,
    native: NativeHandler,
    path_params: Option<AHashMap<String, String>>,
    state: &AppState,
) -> HttpResponse {
    if let Err(response) = bolt_core::request_pipeline::check_headers(req, state.max_header_size) {
        return response;
    }
    if crate::handler::parse_content_length(req).is_some_and(|len| len > state.max_payload_size) {
        return responses::error_413();
    }
    let body = match crate::handler::read_body(&mut payload, state.max_payload_size).await {
        Ok(body) => body,
        Err(response) => return response,
    };

    let (tx, rx) = oneshot::channel::<OwnedResponse>();
    {
        let headers: Vec<abi::Pair> = req
            .headers()
            .iter()
            .map(|(n, v)| abi::Pair {
                name: abi::Slice::from_bytes(n.as_str().as_bytes()),
                value: abi::Slice::from_bytes(v.as_bytes()),
            })
            .collect();
        let path_params: Vec<abi::Pair> = path_params
            .iter()
            .flatten()
            .map(|(k, v)| abi::Pair {
                name: abi::Slice::from_bytes(k.as_bytes()),
                value: abi::Slice::from_bytes(v.as_bytes()),
            })
            .collect();
        let client = client_ip::resolve_from_headers(
            req.headers(),
            req.peer_addr().map(|a| a.ip()),
            &state.trusted_proxies,
        )
        .map(|ip| ip.to_string())
        .unwrap_or_default();
        let request = abi::Request {
            method: abi::Slice::from_bytes(req.method().as_str().as_bytes()),
            path: abi::Slice::from_bytes(req.path().as_bytes()),
            query: abi::Slice::from_bytes(req.query_string().as_bytes()),
            headers: headers.as_ptr(),
            n_headers: headers.len(),
            path_params: path_params.as_ptr(),
            n_path_params: path_params.len(),
            client_addr: abi::Slice::from_bytes(client.as_bytes()),
            body: abi::Slice::from_bytes(&body),
        };

        let token = Box::into_raw(Box::new(tx)).cast::<c_void>();
        // SAFETY: `request` and the buffers it borrows live until `call` returns.
        // The handler owns `token` from now on and gives it back in `complete`.
        unsafe { (native.call)(native.handler.ctx, &request, complete, token) };
        // The handler copied what it keeps, so the request buffers end here and
        // do not stay alive during the wait.
    }
    let out = match tokio::time::timeout(state.native_route_timeout, rx).await {
        Ok(Ok(out)) => out,
        Ok(Err(_)) => return HttpResponse::InternalServerError().finish(),
        Err(_) => {
            eprintln!(
                "[django-bolt] WARNING: native handler '{}' did not complete within {:.3} seconds",
                handler_name(native),
                state.native_route_timeout.as_secs_f64()
            );
            return HttpResponse::GatewayTimeout().finish();
        }
    };

    let status = match StatusCode::from_u16(out.status) {
        Ok(status) if (200..600).contains(&out.status) => status,
        _ => {
            eprintln!(
                "[django-bolt] WARNING: native handler '{}' sent status {}; sending 500",
                handler_name(native),
                out.status
            );
            return HttpResponse::InternalServerError().finish();
        }
    };
    let mut response = HttpResponse::build(status);
    for (name, value) in &out.headers {
        // An internal header of Bolt's CORS middleware, never from a handler.
        if name.eq_ignore_ascii_case(b"x-bolt-skip-cors") {
            continue;
        }
        match (HeaderName::from_bytes(name), HeaderValue::from_bytes(value)) {
            (Ok(name), Ok(value)) => {
                response.append_header((name, value));
            }
            _ => eprintln!(
                "[django-bolt] WARNING: native handler '{}' sent an invalid header {:?}; dropped",
                handler_name(native),
                String::from_utf8_lossy(name)
            ),
        }
    }
    if req.method() == actix_web::http::Method::HEAD {
        return response.finish();
    }
    response.body(out.body)
}
