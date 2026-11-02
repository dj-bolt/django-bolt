use pyo3::prelude::*;

use bolt_core::state::{AsgiMount, parse_websocket_origin_policy};

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

        // A raw ASGI mount keeps the legacy global Origin protection.
        let policy_config = app.bind(py).getattr_opt("_bolt_websocket_origin_policy")?;
        let origin_policy = if policy_config.is_some() {
            parse_websocket_origin_policy(py, Some(app.bind(py)))?
        } else {
            parse_websocket_origin_policy(py, None)?
        };
        asgi_mounts.push(AsgiMount {
            prefix,
            app,
            websocket,
            websocket_origin_policy: origin_policy,
        });
    }

    // Longest-prefix match requires descending sort.
    asgi_mounts.sort_by_key(|mount| std::cmp::Reverse(mount.prefix.len()));

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
