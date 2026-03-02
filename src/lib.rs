use pyo3::prelude::*;

mod asgi_http;
mod asgi_mounts;
mod cookies;
mod cors;
mod error;
mod form_parsing;
mod handler;
mod json;
mod metadata;
mod middleware;
mod permissions;
mod request;
mod request_pipeline;
mod response_builder;
mod response_meta;
mod responses;
mod router;
mod server;
mod state;
mod static_files;
mod streaming;
mod testing;
mod type_coercion;
mod validation;
mod websocket;

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

#[pymodule]
fn _core(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    use crate::server::{
        register_asgi_mounts, register_middleware_metadata, register_routes,
        register_websocket_routes, start_server,
    };
    use crate::testing::{
        create_test_app, destroy_test_app, handle_test_websocket, register_test_asgi_mounts,
        register_test_middleware_metadata, register_test_routes, register_test_websocket_routes,
        test_request,
    };

    // Production server functions
    m.add_function(wrap_pyfunction!(register_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_websocket_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_asgi_mounts, m)?)?;
    m.add_function(wrap_pyfunction!(register_middleware_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(start_server, m)?)?;

    // Test infrastructure functions (async-native, uses Actix test utilities)
    m.add_function(wrap_pyfunction!(create_test_app, m)?)?;
    m.add_function(wrap_pyfunction!(destroy_test_app, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_websocket_routes, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_asgi_mounts, m)?)?;
    m.add_function(wrap_pyfunction!(register_test_middleware_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(test_request, m)?)?;
    m.add_function(wrap_pyfunction!(handle_test_websocket, m)?)?;

    Ok(())
}
