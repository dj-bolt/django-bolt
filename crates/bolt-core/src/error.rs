use actix_web::{http::StatusCode, HttpResponse};
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Extract error information from a Python HTTPException
pub fn extract_http_exception(
    _py: Python,
    exc: &Bound<PyAny>,
) -> Option<(u16, String, Vec<(String, String)>, Option<Py<PyAny>>)> {
    // Try to extract HTTPException fields
    let status_code: u16 = exc
        .getattr("status_code")
        .ok()
        .and_then(|v| v.extract().ok())
        .unwrap_or(500);

    let detail: String = exc
        .getattr("detail")
        .ok()
        .and_then(|v| v.extract().ok())
        .unwrap_or_else(|| "Internal Server Error".to_string());

    let headers: Vec<(String, String)> = exc
        .getattr("headers")
        .ok()
        .and_then(|h| {
            if let Ok(dict) = h.cast::<PyDict>() {
                let mut result = Vec::new();
                for (k, v) in dict {
                    if let (Ok(key), Ok(value)) = (k.extract::<String>(), v.extract::<String>()) {
                        result.push((key, value));
                    }
                }
                Some(result)
            } else {
                Some(Vec::new())
            }
        })
        .unwrap_or_else(|| Vec::new());

    let extra: Option<Py<PyAny>> =
        exc.getattr("extra")
            .ok()
            .and_then(|e| if e.is_none() { None } else { Some(e.unbind()) });

    Some((status_code, detail, headers, extra))
}

/// Check if a Python exception is an HTTPException
pub fn is_http_exception(py: Python, exc: &Bound<PyAny>) -> bool {
    (|| -> PyResult<bool> {
        let exceptions_module = py.import("django_bolt.exceptions")?;
        let http_exc_class = exceptions_module.getattr("HTTPException")?;
        exc.is_instance(&http_exc_class)
    })()
    .unwrap_or(false)
}

/// Check if a Python exception is a ValidationError
pub fn is_validation_error(py: Python, exc: &Bound<PyAny>) -> bool {
    // Check for msgspec.ValidationError
    let is_msgspec = (|| -> PyResult<bool> {
        let msgspec = py.import("msgspec")?;
        let val_err_class = msgspec.getattr("ValidationError")?;
        exc.is_instance(&val_err_class)
    })()
    .unwrap_or(false);

    if is_msgspec {
        return true;
    }

    // Check for our ValidationException types
    (|| -> PyResult<bool> {
        let exceptions_module = py.import("django_bolt.exceptions")?;
        let val_exc_class = exceptions_module.getattr("ValidationException")?;
        exc.is_instance(&val_exc_class)
    })()
    .unwrap_or(false)
}

/// Build an error response from exception information
pub fn build_error_response(
    py: Python,
    status_code: u16,
    detail: String,
    headers: Vec<(String, String)>,
    extra: Option<Py<PyAny>>,
    _debug: bool,
) -> HttpResponse {
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    // Build JSON response body
    let body_json = if let Some(extra_obj) = extra {
        // Include extra data - use py parameter from function
        let error_dict = PyDict::new(py);
        error_dict.set_item("detail", detail).ok();

        let extra_bound = extra_obj.bind(py);
        error_dict.set_item("extra", extra_bound).ok();

        // Serialize to JSON
        serialize_error_dict(py, &error_dict)
    } else {
        // Simple error response
        format!(r#"{{"detail":"{}"}}"#, escape_json(&detail))
    };

    let mut builder = HttpResponse::build(status);
    builder.content_type("application/json");

    // Add custom headers
    for (key, value) in headers {
        builder.append_header((key, value));
    }

    builder.body(body_json)
}

/// Handle Python exception and convert to HTTP response
pub fn handle_python_exception(
    py: Python,
    exc: &Bound<PyAny>,
    path: &str,
    method: &str,
    debug: bool,
) -> HttpResponse {
    // Override debug flag with Django's DEBUG setting (for dynamic checking)
    let debug = (|| -> PyResult<bool> {
        let django_conf = py.import("django.conf")?;
        let settings = django_conf.getattr("settings")?;
        settings.getattr("DEBUG")?.extract::<bool>()
    })()
    .unwrap_or(debug);
    // Check if it's an HTTPException
    if is_http_exception(py, exc) {
        if let Some((status_code, detail, headers, extra)) = extract_http_exception(py, exc) {
            return build_error_response(py, status_code, detail, headers, extra, debug);
        }
    }

    // Check if it's a validation error
    if is_validation_error(py, exc) {
        // Try to use error_handlers module
        let result = (|| -> PyResult<HttpResponse> {
            let error_handlers = py.import("django_bolt.error_handlers")?;
            let handle_exception = error_handlers.getattr("handle_exception")?;
            let response_tuple = handle_exception.call1((exc, debug))?;

            // Extract (status_code, headers, body)
            if let Ok((status, headers, body)) =
                response_tuple.extract::<(u16, Vec<(String, String)>, Vec<u8>)>()
            {
                let status_code =
                    StatusCode::from_u16(status).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY);
                let mut response = HttpResponse::build(status_code);
                for (k, v) in headers {
                    response.append_header((k, v));
                }
                Ok(response.body(body))
            } else {
                Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                    "Invalid response format",
                ))
            }
        })();

        if let Ok(response) = result {
            return response;
        }
    }

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

/// Serialize Python dict to JSON string
fn serialize_error_dict(py: Python, dict: &Bound<PyDict>) -> String {
    // Try using msgspec for fast serialization
    let result = (|| -> PyResult<String> {
        let msgspec = py.import("msgspec.json")?;
        let encode = msgspec.getattr("encode")?;
        let encoded = encode.call1((dict,))?;
        let bytes: Vec<u8> = encoded.extract()?;
        Ok(String::from_utf8_lossy(&bytes).to_string())
    })();

    if let Ok(json_str) = result {
        return json_str;
    }

    // Fallback to manual JSON construction
    format!(r#"{{"detail":"Serialization error"}}"#)
}

/// Escape JSON string
fn escape_json(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_escape_json() {
        assert_eq!(escape_json(r#"Hello "World""#), r#"Hello \"World\""#);
        assert_eq!(escape_json("Line1\nLine2"), "Line1\\nLine2");
        assert_eq!(escape_json("Tab\there"), "Tab\\there");
    }
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
        build_error_response(
            py,
            500,
            "Handler execution error".to_string(),
            vec![],
            None,
            debug,
        )
    }
}
