use pyo3::prelude::*;

use bolt_core::state::AsgiMount;

/// Validate, normalize ordering, and de-duplicate ASGI mount configuration.
pub fn validate_and_sort_asgi_mounts(
    py: Python<'_>,
    mounts: Vec<(String, Py<PyAny>)>,
) -> PyResult<Vec<AsgiMount>> {
    let mut asgi_mounts: Vec<AsgiMount> = Vec::with_capacity(mounts.len());

    for (prefix, app) in mounts {
        if prefix.is_empty() || !prefix.starts_with('/') {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid ASGI mount prefix: {}",
                prefix
            )));
        }

        if prefix.len() > 1 && prefix.ends_with('/') {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "ASGI mount prefix must not end with '/': {}",
                prefix
            )));
        }

        // `mount_django()` marks an app that serves HTTP only.
        let websocket = match app.bind(py).getattr_opt("_bolt_http_only")? {
            Some(http_only) => !http_only.is_truthy()?,
            None => true,
        };

        asgi_mounts.push(AsgiMount {
            prefix,
            app,
            websocket,
        });
    }

    // Longest-prefix match requires descending sort.
    asgi_mounts.sort_by(|a, b| b.prefix.len().cmp(&a.prefix.len()));

    // Exact-duplicate prefixes are invalid.
    for idx in 1..asgi_mounts.len() {
        if asgi_mounts[idx - 1].prefix == asgi_mounts[idx].prefix {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Duplicate ASGI mount prefix: {}",
                asgi_mounts[idx].prefix
            )));
        }
    }

    Ok(asgi_mounts)
}
