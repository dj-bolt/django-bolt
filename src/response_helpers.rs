//! Native operations used while constructing Python responses.

use std::ffi::OsString;
use std::path::PathBuf;

use actix_web::cookie::time::{Date, Month};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDateTime, PyDict, PyTuple};

/// Format the datetime's wall time, matching the existing Cookie API.
#[pyfunction]
pub fn format_cookie_date(value: &Bound<'_, PyDateTime>) -> PyResult<String> {
    let py = value.py();
    let year = value.getattr(pyo3::intern!(py, "year"))?.extract::<i32>()?;
    let month = value.getattr(pyo3::intern!(py, "month"))?.extract::<u8>()?;
    let day = value.getattr(pyo3::intern!(py, "day"))?.extract::<u8>()?;
    let hour = value.getattr(pyo3::intern!(py, "hour"))?.extract::<u8>()?;
    let minute = value
        .getattr(pyo3::intern!(py, "minute"))?
        .extract::<u8>()?;
    let second = value
        .getattr(pyo3::intern!(py, "second"))?
        .extract::<u8>()?;
    let date = Date::from_calendar_date(
        year,
        Month::try_from(month).map_err(|e| PyValueError::new_err(e.to_string()))?,
        day,
    )
    .map_err(|e| PyValueError::new_err(e.to_string()))?;
    const DAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    Ok(format!(
        "{}, {:02} {} {:04} {:02}:{:02}:{:02} GMT",
        DAYS[date.weekday().number_days_from_monday() as usize],
        day,
        MONTHS[month as usize - 1],
        year,
        hour,
        minute,
        second,
    ))
}

/// Make the exception `name` of `django_bolt.exceptions` with `message`.
///
/// The FileResponse errors subclass `FileNotFoundError` and `PermissionError`.
/// Only Bolt raises them, so the error handler maps only them to 404 and 403.
fn file_response_error(py: Python<'_>, name: &str, message: String) -> PyErr {
    match py
        .import("django_bolt.exceptions")
        .and_then(|module| module.getattr(name))
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Resolve the path and check its type with one metadata lookup.
#[pyfunction]
pub fn resolve_response_file(
    py: Python<'_>,
    path: PathBuf,
    allowed: Option<Vec<PathBuf>>,
) -> PyResult<OsString> {
    let resolved = std::fs::canonicalize(&path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            file_response_error(
                py,
                "ResponseFileNotFound",
                format!("File not found: {}", path.display()),
            )
        } else if error.kind() == std::io::ErrorKind::InvalidInput
            || error.raw_os_error() == Some(libc::ELOOP)
        {
            PyValueError::new_err(format!("Invalid file path: {error}"))
        } else {
            PyErr::from(error)
        }
    })?;
    if !resolved.metadata()?.is_file() {
        return Err(PyValueError::new_err(format!(
            "Path is not a regular file: {}",
            path.display()
        )));
    }
    if allowed.is_some_and(|roots| !roots.iter().any(|root| resolved.starts_with(root))) {
        return Err(file_response_error(
            py,
            "ResponseFileNotAllowed",
            format!(
                "File path '{}' is not within allowed directories. Configure BOLT_ALLOWED_FILE_PATHS in Django settings.",
                path.display()
            ),
        ));
    }
    Ok(resolved.into_os_string())
}

/// Validate attributes in one native loop, then populate ordinary Django morsels.
#[pyfunction]
pub fn populate_response_cookies(
    cookies: &Bound<'_, PyAny>,
    rows: &Bound<'_, PyAny>,
) -> PyResult<()> {
    for row in rows.try_iter()? {
        let row = row?;
        let row = row.cast::<PyTuple>()?;
        if row.len() != 9 {
            return Err(PyValueError::new_err("A cookie must contain nine fields"));
        }
        let name = row.get_item(0)?;
        // SimpleCookie retains its name validation, quoting, and duplicate behavior.
        cookies.set_item(&name, row.get_item(1)?)?;
        let morsel = cookies.get_item(&name)?;
        let attrs = morsel.cast::<PyDict>()?;
        for (index, key) in [
            (3, "max-age"),
            (4, "expires"),
            (2, "path"),
            (5, "domain"),
            (6, "secure"),
            (7, "httponly"),
            (8, "samesite"),
        ] {
            let value = row.get_item(index)?;
            let present = if index == 3 {
                !value.is_none()
            } else {
                value.is_truthy()?
            };
            if !present {
                continue;
            }
            let text = value.str()?;
            if text.to_str()?.bytes().any(|b| b < 32 || b == 127) {
                let error = cookies
                    .py()
                    .import("http.cookies")?
                    .getattr("CookieError")?;
                return Err(PyErr::from_value(error.call1((format!(
                    "Control characters are not allowed in cookies {key:?}"
                ),))?));
            }
            // PyDict_SetItem bypasses Morsel's repeated checks after validation above.
            if index == 6 || index == 7 {
                attrs.set_item(key, true)?;
            } else {
                attrs.set_item(key, value)?;
            }
        }
    }
    Ok(())
}
