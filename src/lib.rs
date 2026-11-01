use pyo3::prelude::*;

use bolt_core::request::PyRequest;

mod dev_reload;
mod error_formatter;
mod handler;
mod normalize;
mod response_body;
mod response_helpers;
mod serializer_scan;
mod server;
mod testing;

/// Pure-Rust hot-path helpers re-exported for criterion benches
/// (benches/hot_path.rs links the rlib). Not part of the Python API.
pub mod bench_support {
    pub use bolt_core::router::{convert_path, parse_query_string};
    pub use bolt_core::type_coercion::{
        CoercedValue, DEFAULT_MAX_PARAM_LENGTH, TYPE_BOOL, TYPE_DATETIME, TYPE_DECIMAL, TYPE_INT,
        TYPE_STRING, TYPE_UUID, coerce_param,
    };
    pub use bolt_core::validation::parse_cookies_inline;
}

// Global allocator selection (mutually exclusive features)
// Use jemalloc for sustained loads with lower memory fragmentation
// Use mimalloc (default) for short-lived objects - often faster for web requests
#[cfg(all(feature = "jemalloc", feature = "mimalloc"))]
compile_error!("Features 'jemalloc' and 'mimalloc' are mutually exclusive. Enable only one.");

#[cfg(all(feature = "jemalloc", not(feature = "mimalloc")))]
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

#[cfg(all(feature = "mimalloc", not(feature = "jemalloc")))]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The stack size of the threads that this module starts with the default size.
///
/// Python code runs on these threads: Actix workers, request lanes, Tokio
/// runtime threads, sync streaming threads and test workers. CPython sizes
/// its C recursion limit for the 8 MiB stack of a main thread.
const THREAD_STACK_SIZE: usize = 8 * 1024 * 1024;

/// Give each thread that this module starts with the default size a stack of
/// `THREAD_STACK_SIZE`.
///
/// The default Rust stack is 2 MiB. CPython 3.12 and 3.13 stop C recursion at
/// a fixed depth, and msgspec needs about 3 MiB of stack to get there. Deep
/// JSON on a 2 MiB thread thus overflowed the stack, and the process got
/// SIGSEGV. Actix has no setting for the stack of its worker threads. The
/// standard library reads `RUST_MIN_STACK` when it starts its first thread of
/// the default size, and keeps the value. Thus set the variable, start one
/// thread, and restore the variable. Do this before any other thread starts.
/// This module links its own copy of the standard library, so a thread of
/// another extension module does not set the value for it.
/// A `RUST_MIN_STACK` int from the user stays in effect.
///
/// CPython 3.14 and later check the stack itself, and raise RecursionError
/// before it overflows. There, the default size stays: a larger stack would
/// only let each thread keep more memory after a deep request.
fn set_default_thread_stack_size(py: Python<'_>) -> PyResult<()> {
    if py.version_info() >= (3, 14) {
        return Ok(());
    }
    let previous = std::env::var_os("RUST_MIN_STACK");
    // The standard library ignores a value that is not an int, and uses 2 MiB.
    // Such a value thus gets `THREAD_STACK_SIZE` too.
    if previous
        .as_deref()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.parse::<usize>().is_ok())
    {
        return Ok(());
    }
    // SAFETY: No thread of this module runs yet. Native code on other threads
    // can read the environment at the same time. `os.putenv` has the same
    // race, and Python code (runbolt too) calls it at any time.
    unsafe { std::env::set_var("RUST_MIN_STACK", THREAD_STACK_SIZE.to_string()) };
    let started = std::thread::Builder::new().spawn(|| {});
    // SAFETY: As for `set_var` above. The environment gets back the value of the user.
    unsafe {
        match previous {
            Some(value) => std::env::set_var("RUST_MIN_STACK", value),
            None => std::env::remove_var("RUST_MIN_STACK"),
        }
    };
    started
        .and_then(|thread| {
            thread
                .join()
                .map_err(|_| std::io::Error::other("the thread panicked"))
        })
        .map_err(|err| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "could not set the thread stack size: {err}"
            ))
        })
}

// `gil_used = false` attests that this module is safe to run with the GIL
// disabled (free-threaded CPython, PEP 703). Without the attestation the
// interpreter re-enables the GIL at import and prints a RuntimeWarning.
// Shared state is Rust-owned: the router and metadata are read-only after
// registration, and the mutable parts use `OnceLock`, atomics, `DashMap`,
// or a `Mutex`. Every `#[pyclass]` is `frozen` or belongs to one request,
// stream, or connection.
#[pymodule(gil_used = false)]
fn _core(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    set_default_thread_stack_size(py)?;
    m.add_function(wrap_pyfunction!(response_helpers::format_cookie_date, m)?)?;
    m.add_function(wrap_pyfunction!(
        response_helpers::resolve_response_file,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        response_helpers::populate_response_cookies,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(serializer_scan::find_serializer, m)?)?;
    m.add_class::<serializer_scan::SerializerScanEncoder>()?;
    use crate::dev_reload::run_dev_reloader;
    use crate::server::{
        register_asgi_mounts, register_middleware_metadata, register_routes,
        register_websocket_routes, start_server,
    };
    use crate::testing::{
        TestRateLimiters, close_test_worker_connections, create_test_app, destroy_test_app,
        handle_test_websocket, register_test_asgi_mounts, register_test_middleware_metadata,
        register_test_routes, register_test_websocket_routes, test_request,
    };

    // Class
    m.add_class::<PyRequest>()?;
    m.add_class::<bolt_loop::WorkerLoopScheduler>()?;
    m.add_function(wrap_pyfunction!(bolt_loop::worker_timer_count, m)?)?;
    m.add_function(wrap_pyfunction!(bolt_loop::worker_fd_watcher_count, m)?)?;
    m.add_class::<bolt_core::lane::RequestLane>()?;
    m.add_class::<bolt_core::metadata::GuardCheck>()?;
    m.add_function(wrap_pyfunction!(bolt_core::lane::stop_idle_lanes, m)?)?;

    // Production server functions
    m.add_function(wrap_pyfunction!(register_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_websocket_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_asgi_mounts, m)?)?;
    m.add_function(wrap_pyfunction!(bolt_mcp::register_mcp_mounts, m)?)?;
    m.add_function(wrap_pyfunction!(register_middleware_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(start_server, m)?)?;
    m.add_function(wrap_pyfunction!(run_dev_reloader, m)?)?;

    // Test infrastructure functions (async-native, uses Actix test utilities)
    m.add_class::<TestRateLimiters>()?;
    m.add_function(wrap_pyfunction!(create_test_app, m)?)?;
    m.add_function(wrap_pyfunction!(destroy_test_app, m)?)?;
    m.add_function(wrap_pyfunction!(close_test_worker_connections, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_websocket_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_asgi_mounts, m)?)?;
    m.add_function(wrap_pyfunction!(
        crate::testing::register_test_mcp_mounts,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(register_test_middleware_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(test_request, m)?)?;
    m.add_function(wrap_pyfunction!(handle_test_websocket, m)?)?;

    Ok(())
}
