use pyo3::prelude::*;

use crate::state::AsgiMount;

/// Validate, normalize ordering, and de-duplicate ASGI mount configuration.
pub(crate) fn validate_and_sort_asgi_mounts(
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

        asgi_mounts.push(AsgiMount { prefix, app });
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
