use actix_web::HttpResponse;
use pyo3::prelude::*;
use std::borrow::Cow;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::settings::DjangoSettings;

/// What a framework error response is, for the error formatter of the app.
///
/// The builders of the error responses put it in the response extensions.
/// The handler gives it to the formatter, if the app has one. Without a
/// formatter, the default body of the response goes to the client.
pub enum ErrorInfo {
    /// An error that the Rust core finds, for example a failed guard.
    Core {
        /// A stable name, for example "not_authenticated" or "rate_limited".
        kind: &'static str,
        /// A message for a human.
        detail: Cow<'static, str>,
        /// The field errors, as a JSON list of objects with "loc", "msg" and "type".
        errors: Option<serde_json::Value>,
        /// More data, for example `{"retry_after": 3}` for a rate limit.
        extra: Option<serde_json::Value>,
        /// The cause of a server error, for the log. The formatter does not get it.
        reason: Option<String>,
    },
    /// A Python exception that reached Rust. Python describes it.
    Exception(Py<PyAny>),
}

impl ErrorInfo {
    pub fn core(kind: &'static str, detail: impl Into<Cow<'static, str>>) -> Self {
        Self::Core {
            kind,
            detail: detail.into(),
            errors: None,
            extra: None,
            reason: None,
        }
    }

    /// A 500 of the Rust core. The client gets no detail, and `reason` goes to the log.
    pub fn internal(reason: String) -> Self {
        Self::Core {
            kind: "internal_error",
            detail: Cow::Borrowed("Internal Server Error"),
            errors: None,
            extra: None,
            reason: Some(reason),
        }
    }
}

/// True when an app in this process has an error formatter. Startup sets it.
static ERROR_INFO_ENABLED: AtomicBool = AtomicBool::new(false);

/// Make the error builders record `ErrorInfo`. Startup calls this when an app
/// has an error formatter.
pub fn enable_error_info() {
    ERROR_INFO_ENABLED.store(true, Ordering::Relaxed);
}

/// Put the `ErrorInfo` that `info` makes in the extensions of an error response.
///
/// `info` runs only when an app in this process has an error formatter.
/// Without one, an error response costs one load of a flag more than before.
#[inline]
pub fn with_error_info(response: HttpResponse, info: impl FnOnce() -> ErrorInfo) -> HttpResponse {
    with_error_info_if(ERROR_INFO_ENABLED.load(Ordering::Relaxed), response, info)
}

#[inline]
fn with_error_info_if(
    enabled: bool,
    response: HttpResponse,
    info: impl FnOnce() -> ErrorInfo,
) -> HttpResponse {
    if enabled {
        insert_error_info(response, info())
    } else {
        response
    }
}

#[cold]
fn insert_error_info(mut response: HttpResponse, info: ErrorInfo) -> HttpResponse {
    response.extensions_mut().insert(info);
    response
}

/// A 500 with a JSON `detail` message.
pub fn internal_error_response(detail: &str) -> HttpResponse {
    HttpResponse::InternalServerError()
        .content_type("application/json")
        .body(format!(r#"{{"detail":"{}"}}"#, escape_json(detail)))
}

/// Handle Python exception and convert to HTTP response
pub fn handle_python_exception(
    py: Python,
    exc: &Bound<PyAny>,
    path: &str,
    method: &str,
    debug: bool,
) -> HttpResponse {
    let response = default_exception_response(py, exc, path, method, debug);
    with_error_info(response, || ErrorInfo::Exception(exc.clone().unbind()))
}

fn default_exception_response(
    py: Python,
    exc: &Bound<PyAny>,
    path: &str,
    method: &str,
    debug: bool,
) -> HttpResponse {
    // Override debug flag with Django's DEBUG setting (for dynamic checking).
    // Startup rejects a DEBUG that is not a bool, but a test can change DEBUG
    // later. Then report the error and keep the DEBUG value of startup.
    let debug =
        match DjangoSettings::load(py).and_then(|settings| settings.truth_flag("DEBUG", debug)) {
            Ok(debug) => debug,
            Err(err) => {
                eprintln!("[django-bolt] Warning: {err} Using DEBUG={debug} from startup.");
                debug
            }
        };
    // Generic exception handling
    let exc_str = exc.to_string();
    let exc_type = exc
        .get_type()
        .name()
        .ok()
        .map(|s| s.to_string())
        .unwrap_or_else(|| "Exception".to_string());

    let (detail, extra_info) = if debug {
        // Include traceback in debug mode - already in GIL context via py parameter
        let traceback = (|| -> PyResult<String> {
            let traceback_module = py.import("traceback")?;
            let format_exception = traceback_module.getattr("format_exception")?;
            let exc_type = exc.get_type();
            let tb_attr = exc.getattr("__traceback__")?;
            let tb = format_exception.call1((exc_type, exc, tb_attr))?;
            let tb_list: Vec<String> = tb.extract()?;
            Ok(tb_list.join(""))
        })()
        .unwrap_or_else(|_| "Traceback unavailable".to_string());

        (
            format!("{}: {}", exc_type, exc_str),
            Some(format!(
                r#"{{"exception":"{}","exception_type":"{}","traceback":"{}","path":"{}","method":"{}"}}"#,
                escape_json(&exc_str),
                escape_json(&exc_type),
                escape_json(&traceback),
                escape_json(path),
                escape_json(method),
            )),
        )
    } else {
        ("Internal Server Error".to_string(), None)
    };

    let body = if let Some(extra) = extra_info {
        format!(
            r#"{{"detail":"{}","extra":{}}}"#,
            escape_json(&detail),
            extra
        )
    } else {
        format!(r#"{{"detail":"{}"}}"#, escape_json(&detail))
    };

    HttpResponse::InternalServerError()
        .content_type("application/json")
        .body(body)
}

/// Escape JSON string
fn escape_json(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

/// Handle Python errors and convert to HTTP response
/// OPTIMIZATION: #[inline(never)] on error path - keeps hot path code smaller
#[inline(never)]
pub fn handle_python_error(
    py: Python<'_>,
    err: PyErr,
    path: &str,
    method: &str,
    debug: bool,
) -> HttpResponse {
    err.restore(py);
    if let Some(exc) = PyErr::take(py) {
        let exc_value = exc.value(py);
        handle_python_exception(py, exc_value, path, method, debug)
    } else {
        let response = internal_error_response("Handler execution error");
        with_error_info(response, || {
            ErrorInfo::internal("Handler execution error".to_string())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn without_an_error_formatter_the_error_info_is_not_made() {
        let response = with_error_info_if(false, HttpResponse::NotFound().finish(), || {
            panic!("no app has an error formatter, so the info must not be made")
        });
        assert!(response.extensions().get::<ErrorInfo>().is_none());
    }

    #[test]
    fn with_an_error_formatter_the_error_info_is_in_the_extensions() {
        let response = with_error_info_if(true, HttpResponse::NotFound().finish(), || {
            ErrorInfo::core("not_found", "Not Found")
        });
        assert!(matches!(
            response.extensions().get::<ErrorInfo>(),
            Some(ErrorInfo::Core {
                kind: "not_found",
                ..
            })
        ));
    }

    #[test]
    fn test_escape_json() {
        assert_eq!(escape_json(r#"Hello "World""#), r#"Hello \"World\""#);
        assert_eq!(escape_json("Line1\nLine2"), "Line1\\nLine2");
        assert_eq!(escape_json("Tab\there"), "Tab\\there");
    }
}
