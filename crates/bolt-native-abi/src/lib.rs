//! C ABI between django-bolt and a native route handler in a separately
//! compiled extension module.
//!
//! Only `#[repr(C)]` data and `extern "C"` function pointers cross the
//! boundary. Thus the two sides can use different rustc versions, different
//! copies of Tokio and different allocators. The crate has no dependencies, so
//! a handler can use any PyO3 version.
//!
//! The extension exports a `PyCapsule` named [`CAPSULE_NAME`]. Its pointer is a
//! `*const HandlerV1` that stays valid while the process runs.
//!
//! # Write a handler
//!
//! Use [`handler!`]. It writes the `extern "C"` function, gives the request to
//! a closure that cannot keep it after the call, and catches panics:
//!
//! ```
//! use bolt_native_abi::{handler, RequestRef, Responder};
//!
//! handler!(pub static ECHO = c"echo", |req: RequestRef<'_>, responder: Responder| {
//!     // Copy what you need. The request is valid only during the call.
//!     let body = req.body().to_vec();
//!     // Give the work to your own runtime. Do not block here.
//!     responder.send(200, &[(b"content-type", b"text/plain")], &body);
//! });
//! ```
//!
//! # Contract (v1)
//!
//! * Bolt calls `call` on its worker threads, concurrently. `ctx` must be
//!   thread-safe and static.
//! * `call` must not block. Copy what you need, give the work to your own
//!   runtime and return.
//! * Start threads and runtimes lazily, in the process that serves requests.
//!   With `runbolt --processes N`, a thread started at import time is not in
//!   the worker processes.
//! * Every pointer in [`Request`] is valid only during `call`.
//! * Call `complete` exactly once for each `call`, from any thread, possibly
//!   before `call` returns. Every pointer in [`Response`] is valid only during
//!   `complete`; Bolt copies the response. A null `resp` sends 500.
//! * Do not unwind across `call`. A panic that leaves `call` aborts the
//!   process. [`handler!`] catches a panic, unless the extension is built with
//!   `panic = "abort"`.
//! * If `complete` does not come within `BOLT_NATIVE_ROUTE_TIMEOUT`, Bolt sends
//!   504. v1 has no cancellation: the handler continues, so it must limit its
//!   own work. A later `complete` is valid and has no effect. The same applies
//!   when the client goes away.
//! * Bolt sends 500 for a status outside 200-599. It drops a response header
//!   that is not valid HTTP and logs it. The response body is buffered.
//! * A change to these types that is not compatible gets a new capsule name,
//!   for example `django_bolt.native_handler.v2`.
#![deny(missing_docs)]

use core::ffi::{CStr, c_char, c_void};

/// The name of the capsule that holds a `*const HandlerV1`.
pub const CAPSULE_NAME: &CStr = c"django_bolt.native_handler.v1";

/// Bytes borrowed across the ABI.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Slice {
    /// Start of the bytes. Can be null when `len` is 0.
    pub ptr: *const u8,
    /// Number of bytes.
    pub len: usize,
}

impl Slice {
    /// No bytes.
    pub const EMPTY: Slice = Slice {
        ptr: core::ptr::null(),
        len: 0,
    };

    /// Borrow `b`. The slice is valid while `b` is.
    pub fn from_bytes(b: &[u8]) -> Slice {
        Slice {
            ptr: b.as_ptr(),
            len: b.len(),
        }
    }

    /// # Safety
    /// When `len` is not 0, `ptr` must be valid for `len` bytes for `'a`.
    pub unsafe fn as_bytes<'a>(&self) -> &'a [u8] {
        if self.len == 0 || self.ptr.is_null() {
            &[]
        } else {
            // SAFETY: the caller guarantees `ptr` is valid for `len` bytes.
            unsafe { core::slice::from_raw_parts(self.ptr, self.len) }
        }
    }
}

/// A name and a value, for headers and path parameters.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Pair {
    /// The name.
    pub name: Slice,
    /// The value.
    pub value: Slice,
}

/// # Safety
/// When `len` is not 0, `ptr` must be valid for `len` pairs for `'a`.
unsafe fn pairs<'a>(ptr: *const Pair, len: usize) -> &'a [Pair] {
    if len == 0 || ptr.is_null() {
        &[]
    } else {
        // SAFETY: the caller guarantees `ptr` is valid for `len` pairs.
        unsafe { core::slice::from_raw_parts(ptr, len) }
    }
}

/// The request that Bolt gives to `call`. Valid only during `call`.
#[repr(C)]
pub struct Request {
    /// The method, for example `POST`.
    pub method: Slice,
    /// The path as received, still percent-encoded.
    pub path: Slice,
    /// The query string as received, without `?`, still percent-encoded.
    pub query: Slice,
    /// Lower-case names and raw values, including `authorization` and
    /// `cookie`. The values of one name keep their order.
    pub headers: *const Pair,
    /// Number of headers.
    pub n_headers: usize,
    /// Path parameters, percent-decoded once (`%2F` becomes `/`).
    pub path_params: *const Pair,
    /// Number of path parameters.
    pub n_path_params: usize,
    /// The client address after `BOLT_TRUSTED_PROXIES`, as Python routes see
    /// it. Empty if unknown.
    pub client_addr: Slice,
    /// `http` or `https`. Only a trusted proxy can set it with
    /// `X-Forwarded-Proto`, as for Python routes.
    pub scheme: Slice,
    /// The full body, at most `BOLT_MAX_UPLOAD_SIZE` bytes.
    pub body: Slice,
}

/// The response that the handler gives to `complete`.
#[repr(C)]
pub struct Response {
    /// The status, 200-599.
    pub status: u16,
    /// The headers.
    pub headers: *const Pair,
    /// Number of headers.
    pub n_headers: usize,
    /// The body.
    pub body: Slice,
}

/// Bolt's completion function. See the contract.
pub type CompleteFn = unsafe extern "C" fn(token: *mut c_void, resp: *const Response);

/// The handler's entry point. See the contract.
pub type CallFn = unsafe extern "C" fn(
    ctx: *mut c_void,
    req: *const Request,
    complete: CompleteFn,
    token: *mut c_void,
);

/// The handler that the capsule points to.
#[repr(C)]
pub struct HandlerV1 {
    /// Name for logs and errors. NUL-terminated and static.
    pub name: *const c_char,
    /// Handler state, given back to `call`. Static and thread-safe.
    pub ctx: *mut c_void,
    /// Bolt refuses the capsule at startup when this is null.
    pub call: Option<CallFn>,
}

impl HandlerV1 {
    /// A handler with a static name, state and entry point.
    pub const fn new(name: &'static CStr, ctx: *mut c_void, call: CallFn) -> Self {
        HandlerV1 {
            name: name.as_ptr(),
            ctx,
            call: Some(call),
        }
    }
}

// SAFETY: the contract requires `ctx` to be static and thread-safe, and only
// `unsafe` code can use it.
unsafe impl Sync for HandlerV1 {}
unsafe impl Send for HandlerV1 {}

/// A safe view of a [`Request`] for the duration of `call`.
#[derive(Clone, Copy)]
pub struct RequestRef<'a> {
    req: &'a Request,
}

impl<'a> RequestRef<'a> {
    /// The method, for example `POST`.
    pub fn method(&self) -> &'a [u8] {
        // SAFETY: Bolt keeps every request pointer valid during `call`.
        unsafe { self.req.method.as_bytes() }
    }

    /// The path, still percent-encoded.
    pub fn path(&self) -> &'a [u8] {
        unsafe { self.req.path.as_bytes() }
    }

    /// The query string without `?`, still percent-encoded.
    pub fn query(&self) -> &'a [u8] {
        unsafe { self.req.query.as_bytes() }
    }

    /// The client address after `BOLT_TRUSTED_PROXIES`. Empty if unknown.
    pub fn client_addr(&self) -> &'a [u8] {
        unsafe { self.req.client_addr.as_bytes() }
    }

    /// `http` or `https`, after `BOLT_TRUSTED_PROXIES`.
    pub fn scheme(&self) -> &'a [u8] {
        unsafe { self.req.scheme.as_bytes() }
    }

    /// The full body.
    pub fn body(&self) -> &'a [u8] {
        unsafe { self.req.body.as_bytes() }
    }

    /// All headers, with lower-case names.
    pub fn headers(&self) -> impl Iterator<Item = (&'a [u8], &'a [u8])> {
        let pairs = unsafe { pairs(self.req.headers, self.req.n_headers) };
        pairs
            .iter()
            .map(|p| unsafe { (p.name.as_bytes(), p.value.as_bytes()) })
    }

    /// The first value of a header. The name matches in any case.
    pub fn header(&self, name: &[u8]) -> Option<&'a [u8]> {
        self.headers()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v)
    }

    /// A path parameter, percent-decoded once.
    pub fn path_param(&self, name: &[u8]) -> Option<&'a [u8]> {
        let pairs = unsafe { pairs(self.req.path_params, self.req.n_path_params) };
        pairs
            .iter()
            .map(|p| unsafe { (p.name.as_bytes(), p.value.as_bytes()) })
            .find(|(n, _)| *n == name)
            .map(|(_, v)| v)
    }
}

/// Completes one request exactly once. When it is dropped without
/// [`Responder::send`], for example on an error path or in a panic, it
/// completes with a null response, and Bolt sends and logs its own 500.
/// `mem::forget` on it leaves the request to time out with 504.
pub struct Responder {
    complete: CompleteFn,
    token: *mut c_void,
}

// SAFETY: the contract allows `complete` from any thread.
unsafe impl Send for Responder {}

impl Responder {
    /// [`handler!`] does this for you.
    ///
    /// # Safety
    /// `complete` and `token` must come from one `call`. Make one `Responder`
    /// per `call`, and do not call `complete` yourself.
    #[doc(hidden)]
    pub unsafe fn from_raw(complete: CompleteFn, token: *mut c_void) -> Self {
        Responder { complete, token }
    }

    /// Send the response.
    pub fn send(self, status: u16, headers: &[(&[u8], &[u8])], body: &[u8]) {
        let pairs: Vec<Pair> = headers
            .iter()
            .map(|(name, value)| Pair {
                name: Slice::from_bytes(name),
                value: Slice::from_bytes(value),
            })
            .collect();
        let resp = Response {
            status,
            headers: pairs.as_ptr(),
            n_headers: pairs.len(),
            body: Slice::from_bytes(body),
        };
        let this = core::mem::ManuallyDrop::new(self);
        // SAFETY: `from_raw` guarantees a valid pair, and ManuallyDrop makes
        // this the only call.
        unsafe { (this.complete)(this.token, &resp) };
    }
}

impl Drop for Responder {
    fn drop(&mut self) {
        // SAFETY: as in `send`; `send` skips this drop. A null response asks
        // Bolt for its own 500.
        unsafe { (self.complete)(self.token, core::ptr::null()) };
    }
}

/// Run `f` for one `call`: the body of the function that [`handler!`] writes.
/// Use [`handler!`] instead.
///
/// `f` cannot keep the request after the call, because it must accept any
/// lifetime `'a`. A panic in `f` drops the responder, which sends 500, and
/// does not leave `call`.
///
/// # Safety
/// The arguments must be the arguments of one `call` from Bolt.
#[doc(hidden)]
pub unsafe fn serve<F>(req: *const Request, complete: CompleteFn, token: *mut c_void, f: F)
where
    F: for<'a> FnOnce(RequestRef<'a>, Responder),
{
    // SAFETY: the caller passes Bolt's arguments for this call.
    let responder = unsafe { Responder::from_raw(complete, token) };
    let req = RequestRef {
        req: unsafe { &*req },
    };
    if let Err(payload) =
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(req, responder)))
    {
        // The Drop of a payload can panic too, and that panic would leave
        // `call` and abort the process.
        std::mem::forget(payload);
    }
}

/// Check that `f` accepts a request of any lifetime, so it cannot keep the
/// request. [`handler!`] uses it.
#[doc(hidden)]
pub fn handler_fn<F>(f: F) -> F
where
    F: for<'a> FnOnce(RequestRef<'a>, Responder),
{
    f
}

/// Define a `static` [`HandlerV1`] from a closure `|req: RequestRef, responder: Responder|`.
///
/// The closure cannot keep the request:
///
/// ```compile_fail,E0521
/// use bolt_native_abi::{handler, RequestRef, Responder};
/// static KEPT: std::sync::Mutex<Option<&'static [u8]>> = std::sync::Mutex::new(None);
/// handler!(static ESCAPE = c"escape", |req: RequestRef<'_>, _r: Responder| {
///     *KEPT.lock().unwrap() = Some(req.body());
/// });
/// ```
///
/// The closure is not unsafe code:
///
/// ```compile_fail,E0133
/// use bolt_native_abi::{handler, RequestRef, Responder};
/// handler!(static RAW = c"raw", |_req: RequestRef<'_>, _r: Responder| {
///     let _byte = *(0x10 as *const u8);
/// });
/// ```
///
/// The closure cannot call the generated entry point with a request of its own:
///
/// ```compile_fail,E0133
/// use bolt_native_abi::{handler, Request, RequestRef, Responder, Response};
/// use std::ffi::c_void;
/// extern "C" fn fake(_: *mut c_void, _: *const Response) {}
/// handler!(static NESTED = c"nested", |_req: RequestRef<'_>, _r: Responder| {
///     call(std::ptr::null_mut(), 0x10 as *const Request, fake, std::ptr::null_mut());
/// });
/// ```
///
/// ```
/// bolt_native_abi::handler!(static HELLO = c"hello", |_req, responder: bolt_native_abi::Responder| {
///     responder.send(200, &[], b"hello");
/// });
/// ```
#[macro_export]
macro_rules! handler {
    ($vis:vis static $name:ident = $label:literal, $f:expr $(,)?) => {
        $vis static $name: $crate::HandlerV1 = {
            // The closure is built in a safe fn, so it is not unsafe code.
            // `call` stays unsafe, so the closure cannot call it with a
            // request of its own.
            fn user() -> impl for<'a> FnOnce($crate::RequestRef<'a>, $crate::Responder) {
                $crate::handler_fn($f)
            }
            unsafe extern "C" fn call(
                _ctx: *mut ::core::ffi::c_void,
                req: *const $crate::Request,
                complete: $crate::CompleteFn,
                token: *mut ::core::ffi::c_void,
            ) {
                // SAFETY: Bolt calls this with the arguments of one call.
                unsafe { $crate::serve(req, complete, token, user()) }
            }
            $crate::HandlerV1::new($label, ::core::ptr::null_mut(), call)
        };
    };
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static SEEN: Mutex<Vec<(usize, u16, Vec<u8>)>> = Mutex::new(Vec::new());

    /// Records each completion. Status 0 stands for a null response.
    unsafe extern "C" fn record(token: *mut c_void, resp: *const Response) {
        let (status, body) = if resp.is_null() {
            (0, Vec::new())
        } else {
            let resp = unsafe { &*resp };
            (resp.status, unsafe { resp.body.as_bytes() }.to_vec())
        };
        SEEN.lock().unwrap().push((token as usize, status, body));
    }

    fn seen(token: usize) -> Vec<(u16, Vec<u8>)> {
        SEEN.lock()
            .unwrap()
            .iter()
            .filter(|(t, _, _)| *t == token)
            .map(|(_, status, body)| (*status, body.clone()))
            .collect()
    }

    fn token(n: usize) -> *mut c_void {
        std::ptr::null_mut::<u8>().wrapping_add(n).cast()
    }

    fn request(headers: &[Pair], params: &[Pair]) -> Request {
        Request {
            method: Slice::from_bytes(b"POST"),
            path: Slice::from_bytes(b"/a/42"),
            query: Slice::EMPTY,
            headers: headers.as_ptr(),
            n_headers: headers.len(),
            path_params: params.as_ptr(),
            n_path_params: params.len(),
            client_addr: Slice::EMPTY,
            scheme: Slice::from_bytes(b"https"),
            body: Slice::from_bytes(b"ping"),
        }
    }

    #[test]
    fn send_completes_once() {
        let responder = unsafe { Responder::from_raw(record, token(1)) };
        responder.send(201, &[(b"x", b"y")], b"ok");
        assert_eq!(seen(1), vec![(201, b"ok".to_vec())]);
    }

    #[test]
    fn a_dropped_responder_completes_with_a_null_response() {
        drop(unsafe { Responder::from_raw(record, token(2)) });
        assert_eq!(seen(2), vec![(0, Vec::new())]);
    }

    #[test]
    fn request_ref_finds_headers_and_params() {
        let headers = [Pair {
            name: Slice::from_bytes(b"x-test"),
            value: Slice::from_bytes(b"1"),
        }];
        let params = [Pair {
            name: Slice::from_bytes(b"id"),
            value: Slice::from_bytes(b"42"),
        }];
        let req = request(&headers, &params);
        let req = RequestRef { req: &req };
        assert_eq!(req.header(b"X-Test"), Some(&b"1"[..]));
        assert_eq!(req.header(b"x-other"), None);
        assert_eq!(req.path_param(b"id"), Some(&b"42"[..]));
        assert_eq!(req.scheme(), b"https");
    }

    handler!(static ECHO = c"echo", |req: RequestRef<'_>, responder: Responder| {
        responder.send(200, &[], req.body());
    });
    handler!(static PANICS = c"panics", |_req, _responder: Responder| panic!("handler bug"));

    /// A panic payload whose Drop panics too.
    struct Bomb;
    impl Drop for Bomb {
        fn drop(&mut self) {
            panic!("payload drop");
        }
    }
    handler!(static BOMB = c"bomb", |_req, _responder: Responder| std::panic::panic_any(Bomb));

    #[test]
    fn handler_macro_serves_and_turns_a_panic_into_a_null_response() {
        let req = request(&[], &[]);
        let call = ECHO.call.unwrap();
        unsafe { call(ECHO.ctx, &req, record, token(3)) };
        assert_eq!(seen(3), vec![(200, b"ping".to_vec())]);

        let call = PANICS.call.unwrap();
        unsafe { call(PANICS.ctx, &req, record, token(4)) };
        assert_eq!(seen(4), vec![(0, Vec::new())]);

        // Without the forget, the second panic leaves `call` and aborts.
        let call = BOMB.call.unwrap();
        unsafe { call(BOMB.ctx, &req, record, token(5)) };
        assert_eq!(seen(5), vec![(0, Vec::new())]);
    }
}
