//! Native operations used while constructing Python responses.

use std::ffi::OsString;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

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

pyo3::import_exception!(django_bolt.exceptions, ResponseFileNotFound);
pyo3::import_exception!(django_bolt.exceptions, ResponseFileNotAllowed);

/// Win32 error for a symlink loop. Rust gives it no stable `ErrorKind`.
#[cfg(windows)]
const SYMLINK_LOOP: i32 = 1921;
#[cfg(not(windows))]
const SYMLINK_LOOP: i32 = libc::ELOOP;

/// Map an error of the path lookup to the FileResponse error that Bolt answers.
///
/// The path often comes from the request, so a client must not get a 500 for
/// it. A path that names no regular file is a 404, as in Django's `serve`.
/// Only Bolt raises these errors, so the error handler maps only them to 404 and 403.
///
/// When the lookup stops out of the allowed directories, the path is a 403.
/// This is also true when the server may not read it. A 404 there would tell
/// a client which paths exist. A server that may not read a path in the allowed
/// directories has a fault: a logged 500. This is also true with no allowed directories.
fn file_lookup_error(error: std::io::Error, path: &Path, allowed: Option<&[PathBuf]>) -> PyErr {
    let names_no_file = matches!(
        error.kind(),
        ErrorKind::NotFound
            | ErrorKind::NotADirectory
            | ErrorKind::InvalidInput
            | ErrorKind::InvalidFilename
    ) || error.raw_os_error() == Some(SYMLINK_LOOP);
    if !names_no_file && error.kind() != ErrorKind::PermissionDenied {
        return PyErr::from(error);
    }
    if allowed.is_some_and(|roots| !within(&lookup_stop(path), roots)) {
        return not_allowed(path);
    }
    if names_no_file {
        file_not_found(path)
    } else {
        PyErr::from(error)
    }
}

/// Return the real path at which the OS lookup of `path` stops.
///
/// The OS resolves the parts of a path in sequence, and it follows a link
/// before it applies a later `..`. Thus resolve the parts one at a time.
fn lookup_stop(path: &Path) -> PathBuf {
    let mut reached = PathBuf::new();
    let Ok(absolute) = std::path::absolute(path) else {
        return reached;
    };
    for component in absolute.components() {
        let next = reached.join(component);
        if matches!(component, Component::Prefix(_) | Component::RootDir) {
            reached = next;
            continue;
        }
        match std::fs::canonicalize(&next) {
            Ok(resolved) => reached = resolved,
            Err(_) => break,
        }
    }
    reached
}

fn within(resolved: &Path, roots: &[PathBuf]) -> bool {
    roots.iter().any(|root| resolved.starts_with(root))
}

fn file_not_found(path: &Path) -> PyErr {
    ResponseFileNotFound::new_err(format!("File not found: {}", path.display()))
}

fn not_allowed(path: &Path) -> PyErr {
    ResponseFileNotAllowed::new_err(format!(
        "File path '{}' is not within allowed directories. Configure BOLT_ALLOWED_FILE_PATHS in Django settings.",
        path.display()
    ))
}

/// Resolve the path and check its type with one metadata lookup.
#[pyfunction]
pub fn resolve_response_file(path: PathBuf, allowed: Option<Vec<PathBuf>>) -> PyResult<OsString> {
    let roots = allowed.as_deref();
    let resolved =
        std::fs::canonicalize(&path).map_err(|error| file_lookup_error(error, &path, roots))?;
    // Check the roots before the type, so a directory out of them is a 403 as a file is.
    if roots.is_some_and(|roots| !within(&resolved, roots)) {
        return Err(not_allowed(&path));
    }
    let metadata = resolved
        .metadata()
        .map_err(|error| file_lookup_error(error, &path, roots))?;
    if !metadata.is_file() {
        return Err(file_not_found(&path));
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
