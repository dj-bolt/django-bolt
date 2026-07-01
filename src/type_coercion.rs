//! Type coercion for path and query parameters
//!
//! This module provides Rust-native type coercion for HTTP parameters,
//! eliminating the need for Python's convert_primitive() function.
//! Performance improvement: ~100-500µs per parameter.

use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyAnyMethods, PyDictMethods};
use pyo3::{IntoPyObject, Py, PyAny, Python};
use rust_decimal::Decimal;
use std::str::FromStr;
use uuid::Uuid;

// OPTIMIZATION: Cache Python classes for type construction (avoids repeated imports)
// Each import costs ~50-100ns, caching eliminates this overhead for repeated coercions
static UUID_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATETIME_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

#[inline]
fn get_uuid_class(py: Python<'_>) -> &Py<PyAny> {
    UUID_CLASS.get_or_init(py, || {
        py.import("uuid").unwrap().getattr("UUID").unwrap().unbind()
    })
}

#[inline]
fn get_decimal_class(py: Python<'_>) -> &Py<PyAny> {
    DECIMAL_CLASS.get_or_init(py, || {
        py.import("decimal")
            .unwrap()
            .getattr("Decimal")
            .unwrap()
            .unbind()
    })
}

#[inline]
fn get_datetime_class(py: Python<'_>) -> &Py<PyAny> {
    DATETIME_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("datetime")
            .unwrap()
            .unbind()
    })
}

#[inline]
fn get_date_class(py: Python<'_>) -> &Py<PyAny> {
    DATE_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("date")
            .unwrap()
            .unbind()
    })
}

#[inline]
fn get_time_class(py: Python<'_>) -> &Py<PyAny> {
    TIME_CLASS.get_or_init(py, || {
        py.import("datetime")
            .unwrap()
            .getattr("time")
            .unwrap()
            .unbind()
    })
}

/// Default maximum allowed length for parameter values (8KB).
/// Prevents memory exhaustion attacks from extremely long parameters.
pub const DEFAULT_MAX_PARAM_LENGTH: usize = 8192;

/// Hard upper bound for the configurable parameter length (1MB).
/// A misconfigured `DJANGO_BOLT_MAX_PARAM_LENGTH` (e.g. an enormous value) must
/// not be able to nullify the DoS guardrail, so any configured value is clamped
/// to this cap.
pub const MAX_ALLOWED_PARAM_LENGTH: usize = 1024 * 1024;

#[inline]
fn parse_max_param_length(value: Option<&str>) -> usize {
    value
        .and_then(|raw| raw.parse::<usize>().ok())
        .filter(|parsed| *parsed > 0)
        .map(|parsed| parsed.min(MAX_ALLOWED_PARAM_LENGTH))
        .unwrap_or(DEFAULT_MAX_PARAM_LENGTH)
}

/// Resolve the configured maximum parameter length from the
/// `DJANGO_BOLT_MAX_PARAM_LENGTH` environment variable.
///
/// Call this exactly ONCE at server startup and store the result in
/// `AppState.max_param_length`. The per-request hot path then reads that plain
/// field — no env access, no lock, no atomics. Missing, empty, non-integer, or
/// `0` values fall back to [`DEFAULT_MAX_PARAM_LENGTH`].
pub fn resolve_max_param_length() -> usize {
    parse_max_param_length(
        std::env::var("DJANGO_BOLT_MAX_PARAM_LENGTH")
            .ok()
            .as_deref(),
    )
}

/// Type hint constants (must match Python's get_type_hint_id() in compiler.py)
pub const TYPE_INT: u8 = 1;
pub const TYPE_FLOAT: u8 = 2;
pub const TYPE_BOOL: u8 = 3;
pub const TYPE_STRING: u8 = 4;
pub const TYPE_UUID: u8 = 5;
pub const TYPE_DATETIME: u8 = 6;
pub const TYPE_DECIMAL: u8 = 7;
pub const TYPE_DATE: u8 = 8;
pub const TYPE_TIME: u8 = 9;

/// Result of type coercion - can be converted to Python types
#[derive(Debug, Clone)]
pub enum CoercedValue {
    Int(i64),
    Float(f64),
    Bool(bool),
    String(String),
    Uuid(Uuid),
    DateTime(DateTime<Utc>),
    NaiveDateTime(NaiveDateTime),
    Date(NaiveDate),
    Time(NaiveTime),
    Decimal(Decimal),
    #[allow(dead_code)]
    Null,
}

impl CoercedValue {
    /// Convert to string representation (for future use with typed PyRequest)
    #[allow(dead_code)]
    pub fn to_string_repr(&self) -> String {
        match self {
            CoercedValue::Int(v) => v.to_string(),
            CoercedValue::Float(v) => v.to_string(),
            CoercedValue::Bool(v) => v.to_string(),
            CoercedValue::String(v) => v.clone(),
            CoercedValue::Uuid(v) => v.to_string(),
            CoercedValue::DateTime(v) => v.to_rfc3339(),
            CoercedValue::NaiveDateTime(v) => v.to_string(),
            CoercedValue::Date(v) => v.to_string(),
            CoercedValue::Time(v) => v.to_string(),
            CoercedValue::Decimal(v) => v.to_string(),
            CoercedValue::Null => "null".to_string(),
        }
    }
}

/// Error returned by [`coerce_param`] / [`coerce_param_with_limit`].
///
/// Callers distinguish the two variants structurally:
/// * [`CoerceError::TooLong`] is a hard security limit — it MUST always be
///   rejected (422 for HTTP, upgrade rejection for WebSockets). Never fall back
///   to passing the oversized value through.
/// * [`CoerceError::Invalid`] is a normal type-coercion failure. Some callers
///   (WebSocket scope building) intentionally fall back to the raw string here.
///
/// `Display` reproduces the exact same messages the old `String` errors used,
/// so existing `format!("{e}")` / `.to_string()` consumers are unchanged.
#[derive(Debug, Clone)]
pub enum CoerceError {
    /// Value exceeded the configured `max_param_length` (in bytes).
    TooLong { len: usize, max: usize },
    /// Value could not be parsed into the requested type.
    Invalid(String),
}

impl std::fmt::Display for CoerceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CoerceError::TooLong { len, max } => {
                write!(f, "Parameter too long: {} bytes (max {} bytes)", len, max)
            }
            CoerceError::Invalid(msg) => f.write_str(msg),
        }
    }
}

/// Coerce a string value to the specified type
///
/// # Arguments
/// * `value` - The string value to coerce
/// * `type_hint` - Type hint constant (TYPE_INT, TYPE_FLOAT, etc.)
///
/// # Returns
/// * `Ok(CoercedValue)` - Successfully coerced value
/// * `Err(CoerceError)` - Structured error (`TooLong` vs `Invalid`)
pub fn coerce_param(value: &str, type_hint: u8) -> Result<CoercedValue, CoerceError> {
    coerce_param_with_limit(value, type_hint, DEFAULT_MAX_PARAM_LENGTH)
}

/// Same as [`coerce_param`] but takes the startup-resolved length limit
/// (`AppState.max_param_length`) so the hot path never re-resolves config.
pub(crate) fn coerce_param_with_limit(
    value: &str,
    type_hint: u8,
    max_length: usize,
) -> Result<CoercedValue, CoerceError> {
    // Security: Reject excessively long parameters to prevent memory exhaustion.
    // Surfaced as a distinct variant so callers can enforce it as a hard limit
    // without inspecting the error message text.
    if value.len() > max_length {
        return Err(CoerceError::TooLong {
            len: value.len(),
            max: max_length,
        });
    }

    coerce_typed(value, type_hint).map_err(CoerceError::Invalid)
}

/// Parse `value` into `type_hint` without the length check. Plain-message errors
/// are wrapped in [`CoerceError::Invalid`] by the public entry points above.
fn coerce_typed(value: &str, type_hint: u8) -> Result<CoercedValue, String> {
    match type_hint {
        TYPE_INT => value
            .parse::<i64>()
            .map(CoercedValue::Int)
            .map_err(|e| format!("Invalid integer '{}': {}", value, e)),

        TYPE_FLOAT => value
            .parse::<f64>()
            .map(CoercedValue::Float)
            .map_err(|e| format!("Invalid float '{}': {}", value, e)),

        TYPE_BOOL => {
            let lower = value.to_lowercase();
            let is_true = matches!(lower.as_str(), "true" | "1" | "yes" | "on");
            let is_false = matches!(lower.as_str(), "false" | "0" | "no" | "off");
            if is_true {
                Ok(CoercedValue::Bool(true))
            } else if is_false {
                Ok(CoercedValue::Bool(false))
            } else {
                // Empty string or invalid value - reject (don't silently convert to false)
                Err(format!(
                    "Invalid boolean '{}': expected true/false/1/0/yes/no/on/off",
                    value
                ))
            }
        }

        TYPE_STRING => Ok(CoercedValue::String(value.to_string())),

        TYPE_UUID => Uuid::parse_str(value)
            .map(CoercedValue::Uuid)
            .map_err(|e| format!("Invalid UUID '{}': {}", value, e)),

        TYPE_DATETIME => parse_datetime(value),

        TYPE_DECIMAL => Decimal::from_str(value)
            .map(CoercedValue::Decimal)
            .map_err(|e| format!("Invalid decimal '{}': {}", value, e)),

        TYPE_DATE => NaiveDate::parse_from_str(value, "%Y-%m-%d")
            .map(CoercedValue::Date)
            .map_err(|e| format!("Invalid date '{}': {}", value, e)),

        TYPE_TIME => parse_time(value),

        _ => Ok(CoercedValue::String(value.to_string())),
    }
}

/// Parse datetime string supporting multiple formats
fn parse_datetime(value: &str) -> Result<CoercedValue, String> {
    // Try RFC 3339 / ISO 8601 with timezone (most common)
    if let Ok(dt) = DateTime::parse_from_rfc3339(value) {
        return Ok(CoercedValue::DateTime(dt.with_timezone(&Utc)));
    }

    // Try ISO 8601 with Z suffix
    if value.ends_with('Z') {
        let without_z = &value[..value.len() - 1];
        if let Ok(ndt) = NaiveDateTime::parse_from_str(without_z, "%Y-%m-%dT%H:%M:%S%.f") {
            return Ok(CoercedValue::DateTime(ndt.and_utc()));
        }
        if let Ok(ndt) = NaiveDateTime::parse_from_str(without_z, "%Y-%m-%dT%H:%M:%S") {
            return Ok(CoercedValue::DateTime(ndt.and_utc()));
        }
    }

    // Try naive datetime formats (without timezone)
    let naive_formats = [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ];

    for fmt in &naive_formats {
        if let Ok(ndt) = NaiveDateTime::parse_from_str(value, fmt) {
            return Ok(CoercedValue::NaiveDateTime(ndt));
        }
    }

    // Try date-only (will be converted to datetime at midnight)
    if let Ok(date) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        let ndt = date.and_hms_opt(0, 0, 0).unwrap();
        return Ok(CoercedValue::NaiveDateTime(ndt));
    }

    Err(format!(
        "Invalid datetime '{}': expected ISO 8601 format (e.g., 2024-01-15T10:30:00Z)",
        value
    ))
}

/// Parse time string supporting multiple formats
fn parse_time(value: &str) -> Result<CoercedValue, String> {
    let formats = ["%H:%M:%S%.f", "%H:%M:%S", "%H:%M"];

    for fmt in &formats {
        if let Ok(time) = NaiveTime::parse_from_str(value, fmt) {
            return Ok(CoercedValue::Time(time));
        }
    }

    Err(format!(
        "Invalid time '{}': expected format HH:MM:SS or HH:MM",
        value
    ))
}

/// Convert a string value to Python object based on type hint.
/// Handles all supported types including datetime, uuid, and decimal.
/// Returns PyResult to properly handle validation errors.
/// `max_length` is the startup-resolved limit (`AppState.max_param_length`),
/// passed in so the `params_to_py_dict` hot loop never re-resolves config.
#[inline]
pub fn coerce_to_py(
    py: pyo3::Python<'_>,
    value: &str,
    type_hint: u8,
    max_length: usize,
) -> pyo3::PyResult<pyo3::Py<pyo3::PyAny>> {
    // Security: Validate length for ALL types (defense in depth)
    if value.len() > max_length {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Parameter too long: {} bytes (max {} bytes)",
            value.len(),
            max_length
        )));
    }

    match type_hint {
        TYPE_INT => Ok(value
            .parse::<i64>()
            .unwrap_or(0)
            .into_pyobject(py)
            .unwrap()
            .into_any()
            .unbind()),
        TYPE_FLOAT => Ok(value
            .parse::<f64>()
            .unwrap_or(0.0)
            .into_pyobject(py)
            .unwrap()
            .into_any()
            .unbind()),
        TYPE_BOOL => {
            let is_true = matches!(value.to_lowercase().as_str(), "true" | "1" | "yes" | "on");
            Ok(is_true
                .into_pyobject(py)
                .unwrap()
                .to_owned()
                .unbind()
                .into_any())
        }
        TYPE_UUID => {
            // Parse UUID in Rust, convert to Python uuid.UUID
            // OPTIMIZATION: Use cached UUID class (avoids py.import per call)
            match Uuid::parse_str(value) {
                Ok(uuid) => {
                    let py_uuid = get_uuid_class(py).call1(py, (uuid.to_string(),))?;
                    Ok(py_uuid)
                }
                Err(e) => Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Invalid UUID '{}': {}",
                    value, e
                ))),
            }
        }
        TYPE_DATETIME => {
            // Parse datetime in Rust, convert to Python datetime
            // OPTIMIZATION: Use cached datetime class (avoids py.import per call)
            match parse_datetime(value) {
                Ok(CoercedValue::DateTime(dt)) => {
                    // Use fromisoformat for RFC3339 strings
                    let iso_str = dt.to_rfc3339().replace('Z', "+00:00");
                    let py_dt =
                        get_datetime_class(py).call_method1(py, "fromisoformat", (iso_str,))?;
                    Ok(py_dt)
                }
                Ok(CoercedValue::NaiveDateTime(ndt)) => {
                    // Format as ISO string for fromisoformat
                    let py_dt = get_datetime_class(py).call_method1(
                        py,
                        "fromisoformat",
                        (ndt.format("%Y-%m-%dT%H:%M:%S%.f").to_string(),),
                    )?;
                    Ok(py_dt)
                }
                Ok(_) => {
                    // Shouldn't happen, but fallback to string
                    Ok(value
                        .to_string()
                        .into_pyobject(py)
                        .unwrap()
                        .into_any()
                        .unbind())
                }
                Err(e) => Err(pyo3::exceptions::PyValueError::new_err(e)),
            }
        }
        TYPE_DECIMAL => {
            // Validate decimal in Rust, convert to Python Decimal
            // OPTIMIZATION: Use cached Decimal class (avoids py.import per call)
            match Decimal::from_str(value) {
                Ok(d) => {
                    let py_decimal = get_decimal_class(py).call1(py, (d.to_string(),))?;
                    Ok(py_decimal)
                }
                Err(e) => Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Invalid decimal '{}': {}",
                    value, e
                ))),
            }
        }
        TYPE_DATE => {
            // Parse date in Rust, convert to Python date
            // OPTIMIZATION: Use cached date class (avoids py.import per call)
            match NaiveDate::parse_from_str(value, "%Y-%m-%d") {
                Ok(date) => {
                    let py_date = get_date_class(py).call_method1(
                        py,
                        "fromisoformat",
                        (date.to_string(),),
                    )?;
                    Ok(py_date)
                }
                Err(e) => Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Invalid date '{}': {}",
                    value, e
                ))),
            }
        }
        TYPE_TIME => {
            // Parse time in Rust, convert to Python time
            // OPTIMIZATION: Use cached time class (avoids py.import per call)
            match parse_time(value) {
                Ok(CoercedValue::Time(time)) => {
                    let py_time = get_time_class(py).call_method1(
                        py,
                        "fromisoformat",
                        (time.to_string(),),
                    )?;
                    Ok(py_time)
                }
                Ok(_) => {
                    // Shouldn't happen, but fallback to string
                    Ok(value
                        .to_string()
                        .into_pyobject(py)
                        .unwrap()
                        .into_any()
                        .unbind())
                }
                Err(e) => Err(pyo3::exceptions::PyValueError::new_err(e)),
            }
        }
        _ => Ok(value
            .to_string()
            .into_pyobject(py)
            .unwrap()
            .into_any()
            .unbind()),
    }
}

/// Convert a map of string params to a Python dict with type coercion.
/// Used by both production handler and test handler.
/// Returns PyResult to properly handle coercion errors.
pub fn params_to_py_dict<'py>(
    py: pyo3::Python<'py>,
    params: &ahash::AHashMap<String, String>,
    param_types: &std::collections::HashMap<String, u8>,
    max_param_length: usize,
) -> pyo3::PyResult<pyo3::Bound<'py, pyo3::types::PyDict>> {
    let dict = pyo3::types::PyDict::new(py);
    for (name, value) in params {
        let type_hint = param_types.get(name).copied().unwrap_or(TYPE_STRING);
        let py_value = coerce_to_py(py, value, type_hint, max_param_length)?;
        let _ = dict.set_item(name, py_value);
    }
    Ok(dict)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_coerce_int() {
        assert!(matches!(
            coerce_param("42", TYPE_INT),
            Ok(CoercedValue::Int(42))
        ));
        assert!(matches!(
            coerce_param("-123", TYPE_INT),
            Ok(CoercedValue::Int(-123))
        ));
        assert!(coerce_param("abc", TYPE_INT).is_err());
    }

    #[test]
    fn test_coerce_float() {
        assert!(
            matches!(coerce_param("3.14", TYPE_FLOAT), Ok(CoercedValue::Float(f)) if (f - 3.14).abs() < 0.001)
        );
        assert!(
            matches!(coerce_param("-2.5", TYPE_FLOAT), Ok(CoercedValue::Float(f)) if (f + 2.5).abs() < 0.001)
        );
        assert!(coerce_param("not_a_float", TYPE_FLOAT).is_err());
    }

    #[test]
    fn test_coerce_bool() {
        assert!(matches!(
            coerce_param("true", TYPE_BOOL),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("True", TYPE_BOOL),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("1", TYPE_BOOL),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("yes", TYPE_BOOL),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("on", TYPE_BOOL),
            Ok(CoercedValue::Bool(true))
        ));

        assert!(matches!(
            coerce_param("false", TYPE_BOOL),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("False", TYPE_BOOL),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("0", TYPE_BOOL),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("no", TYPE_BOOL),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("off", TYPE_BOOL),
            Ok(CoercedValue::Bool(false))
        ));
    }

    #[test]
    fn test_coerce_uuid() {
        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";
        assert!(matches!(
            coerce_param(uuid_str, TYPE_UUID),
            Ok(CoercedValue::Uuid(_))
        ));
        assert!(coerce_param("not-a-uuid", TYPE_UUID).is_err());
    }

    #[test]
    fn test_coerce_datetime() {
        // ISO 8601 with Z suffix
        assert!(matches!(
            coerce_param("2024-01-15T10:30:00Z", TYPE_DATETIME),
            Ok(CoercedValue::DateTime(_))
        ));

        // ISO 8601 with timezone offset
        assert!(matches!(
            coerce_param("2024-01-15T10:30:00+00:00", TYPE_DATETIME),
            Ok(CoercedValue::DateTime(_))
        ));

        // Naive datetime
        assert!(matches!(
            coerce_param("2024-01-15T10:30:00", TYPE_DATETIME),
            Ok(CoercedValue::NaiveDateTime(_))
        ));

        assert!(coerce_param("not-a-datetime", TYPE_DATETIME).is_err());
    }

    #[test]
    fn test_coerce_decimal() {
        assert!(matches!(
            coerce_param("123.45", TYPE_DECIMAL),
            Ok(CoercedValue::Decimal(_))
        ));
        assert!(matches!(
            coerce_param("-99.99", TYPE_DECIMAL),
            Ok(CoercedValue::Decimal(_))
        ));
        assert!(coerce_param("not_decimal", TYPE_DECIMAL).is_err());
    }

    #[test]
    fn test_param_length_limit() {
        let max_length = DEFAULT_MAX_PARAM_LENGTH;
        // Valid length should work
        let valid = "a".repeat(max_length);
        assert!(coerce_param(&valid, TYPE_STRING).is_ok());

        // Exceeding limit should fail
        let too_long = "a".repeat(max_length + 1);
        let result = coerce_param(&too_long, TYPE_STRING);
        assert!(matches!(result, Err(CoerceError::TooLong { .. })));
    }

    #[test]
    fn test_parse_max_param_length_defaults_for_invalid_values() {
        assert_eq!(parse_max_param_length(None), DEFAULT_MAX_PARAM_LENGTH);
        assert_eq!(parse_max_param_length(Some("")), DEFAULT_MAX_PARAM_LENGTH);
        assert_eq!(parse_max_param_length(Some("0")), DEFAULT_MAX_PARAM_LENGTH);
        assert_eq!(
            parse_max_param_length(Some("not-a-number")),
            DEFAULT_MAX_PARAM_LENGTH
        );
    }

    #[test]
    fn test_parse_max_param_length_accepts_valid_positive_value() {
        assert_eq!(parse_max_param_length(Some("16384")), 16384);
    }

    #[test]
    fn test_parse_max_param_length_clamps_to_upper_bound() {
        // A misconfigured oversized value must be clamped to the hard cap so it
        // cannot nullify the DoS guardrail.
        assert_eq!(
            parse_max_param_length(Some("999999999999")),
            MAX_ALLOWED_PARAM_LENGTH
        );
        // The cap itself is accepted as-is.
        assert_eq!(
            parse_max_param_length(Some(&MAX_ALLOWED_PARAM_LENGTH.to_string())),
            MAX_ALLOWED_PARAM_LENGTH
        );
    }
}
