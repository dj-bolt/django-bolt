//! Native operations used while constructing Python responses.

use std::borrow::Cow;
use std::ffi::OsString;
use std::io::ErrorKind;
#[cfg(windows)]
use std::path::Prefix;
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
/// This is also true when the server cannot search a directory there. A 404
/// there would tell a client which files exist. A server that cannot reach a
/// file in the allowed directories has a fault: a logged 500. This is also
/// true with no allowed directories, and for a directory that the server
/// cannot search on the way to an allowed directory. An allowed directory that
/// does not exist yet names no file, so a path in it is a 404.
fn file_lookup_error(error: std::io::Error, path: &Path, allowed: Option<&[PathBuf]>) -> PyErr {
    let names_no_file = matches!(
        error.kind(),
        ErrorKind::NotFound
            | ErrorKind::NotADirectory
            | ErrorKind::InvalidInput
            | ErrorKind::InvalidFilename
    ) || error.raw_os_error() == Some(SYMLINK_LOOP);
    let denied = error.kind() == ErrorKind::PermissionDenied;
    if !names_no_file && !denied {
        return PyErr::from(error);
    }
    // The OS looks up no part of an empty path, so it names no file anywhere.
    if let Some(roots) = allowed.filter(|_| !path.as_os_str().is_empty()) {
        let stop = lookup_stop(path);
        // The lookup failed on the way to an allowed directory: the directory is missing or the server cannot search it.
        let blocks_a_root = stop.blocked.is_some_and(|blocked| {
            roots
                .iter()
                .any(|root| comparable(root).starts_with(&blocked))
        });
        if !within(&stop.reached, roots) && !blocks_a_root {
            return not_allowed(path);
        }
    }
    if names_no_file {
        file_not_found(path)
    } else {
        PyErr::from(error)
    }
}

/// The maximum number of links in one lookup, as the kernel allows.
#[cfg(target_os = "macos")]
const MAX_LINKS: u32 = 32;
#[cfg(not(target_os = "macos"))]
const MAX_LINKS: u32 = 40;

/// A part of a path that `lookup_stop` has yet to resolve.
enum Part {
    Root(PathBuf),
    Parent,
    Name(OsString),
}

/// Return the parts of `path` in reverse order, so that `pop` gives the first part.
fn parts_of(path: &Path) -> Vec<Part> {
    let mut root = PathBuf::new();
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => root.push(component),
            Component::CurDir => {}
            Component::ParentDir => parts.push(Part::Parent),
            Component::Normal(name) => parts.push(Part::Name(name.to_os_string())),
        }
    }
    parts.reverse();
    if !root.as_os_str().is_empty() {
        parts.push(Part::Root(root));
    }
    parts
}

/// Where the lookup of a path stopped.
struct Stop {
    /// The real path that the lookup reached.
    reached: PathBuf,
    /// The path in `reached` that the lookup could not `lstat`.
    blocked: Option<PathBuf>,
    /// True when the lookup resolved each part of the path.
    finished: bool,
}

/// Return where the lookup of `path` stops, with the real names of the parts that it reached.
///
/// The walk keeps the spelling of the path, such as an 8.3 short name or another case.
/// The allowed roots have the real names, so give the path that the walk reached its real name too.
fn lookup_stop(path: &Path) -> Stop {
    let stop = walk(path);
    let Ok(real) = std::fs::canonicalize(&stop.reached) else {
        return stop;
    };
    let reached = comparable(&real).into_owned();
    let blocked = stop
        .blocked
        .as_deref()
        .and_then(Path::file_name)
        .map(|name| reached.join(name));
    Stop {
        reached,
        blocked,
        finished: stop.finished,
    }
}

/// Walk the parts of `path` in sequence, as `realpath` does on Linux.
///
/// The walk follows a link before it applies a later `..`, and it stops below
/// a file. Each part costs one `lstat`, and a link also costs one `readlink`.
/// The parts of a link target go in front of the remaining parts. The path that
/// the walk reached has no links, so `..` removes its last part.
fn walk(path: &Path) -> Stop {
    let mut stop = Stop {
        reached: PathBuf::new(),
        blocked: None,
        finished: false,
    };
    let Ok(absolute) = std::path::absolute(path) else {
        return stop;
    };
    let mut pending = parts_of(&absolute);
    let mut is_dir = true;
    let mut links = 0;
    while let Some(part) = pending.pop() {
        match part {
            Part::Root(root) => {
                stop.reached = root;
                is_dir = true;
            }
            // A file has no parts below it.
            _ if !is_dir => return stop,
            Part::Parent => {
                stop.reached.pop();
            }
            Part::Name(name) => {
                let next = stop.reached.join(name);
                let Ok(metadata) = next.symlink_metadata() else {
                    stop.blocked = Some(next);
                    return stop;
                };
                if !metadata.file_type().is_symlink() {
                    is_dir = metadata.is_dir();
                    stop.reached = next;
                    continue;
                }
                links += 1;
                if links > MAX_LINKS {
                    return stop;
                }
                let Ok(target) = std::fs::read_link(&next) else {
                    return stop;
                };
                pending.extend(parts_of(&target));
            }
        }
    }
    stop.finished = true;
    stop
}

/// Return `path` in the form of the allowed roots.
///
/// On Windows, `canonicalize` gives an extended-length path such as `\\?\C:\srv`.
/// Python gives `C:\srv` for the same directory, so remove the `\\?\` part.
#[cfg(windows)]
fn comparable(path: &Path) -> Cow<'_, Path> {
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return Cow::Borrowed(path);
    };
    let plain = match prefix.kind() {
        Prefix::VerbatimDisk(disk) => OsString::from(format!("{}:", char::from(disk))),
        Prefix::VerbatimUNC(server, share) => {
            let mut unc = OsString::from(r"\\");
            unc.push(server);
            unc.push(r"\");
            unc.push(share);
            unc
        }
        _ => return Cow::Borrowed(path),
    };
    let mut plain = PathBuf::from(plain);
    plain.extend(components);
    Cow::Owned(plain)
}

#[cfg(not(windows))]
fn comparable(path: &Path) -> Cow<'_, Path> {
    Cow::Borrowed(path)
}

fn within(resolved: &Path, roots: &[PathBuf]) -> bool {
    roots
        .iter()
        .any(|root| resolved.starts_with(comparable(root)))
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
    if let Some(roots) = roots {
        // macOS `realpath` accepts `..` after a file, where the lookup on Linux fails.
        // Walk such a path as on Linux, so that its status cannot show whether the file exists.
        if path.components().any(|part| part == Component::ParentDir) {
            let stop = lookup_stop(&path);
            if !stop.finished {
                return Err(if within(&stop.reached, roots) {
                    file_not_found(&path)
                } else {
                    not_allowed(&path)
                });
            }
        }
        // Check the roots before the type, so a directory out of them is a 403 as a file is.
        if !within(&comparable(&resolved), roots) {
            return Err(not_allowed(&path));
        }
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
