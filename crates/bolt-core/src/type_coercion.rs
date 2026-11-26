//! Type coercion for path and query parameters
//!
//! This module provides Rust-native type coercion for HTTP parameters,
//! eliminating the need for Python's convert_primitive() function.
//! Performance improvement: ~100-500µs per parameter.

use std::sync::atomic::{AtomicUsize, Ordering};

use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, Timelike, Weekday};
use pyo3::exceptions::PyValueError;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyAnyMethods, PyDateTime, PyDelta, PyDict, PyDictMethods, PyList, PyListMethods, PyString,
    PyStringMethods, PyTime, PyTzInfo,
};
use pyo3::{Bound, IntoPyObject, Py, PyAny, PyResult, Python, pyfunction};
use uuid::Uuid;

// OPTIMIZATION: Cache Python classes for type construction (avoids repeated imports)
// Each import costs ~50-100ns, caching eliminates this overhead for repeated coercions
static UUID_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static OBJECT_NEW: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SAFE_UUID_UNKNOWN: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

#[inline]
pub fn get_uuid_class(py: Python<'_>) -> &Py<PyAny> {
    UUID_CLASS.get_or_init(py, || {
        py.import("uuid").unwrap().getattr("UUID").unwrap().unbind()
    })
}

#[inline]
pub fn get_decimal_class(py: Python<'_>) -> &Py<PyAny> {
    DECIMAL_CLASS.get_or_init(py, || {
        py.import("decimal")
            .unwrap()
            .getattr("Decimal")
            .unwrap()
            .unbind()
    })
}

/// Default maximum allowed length for parameter values (8KB).
/// Prevents memory exhaustion attacks from extremely long parameters.
pub const DEFAULT_MAX_PARAM_LENGTH: usize = 8192;

/// Hard upper bound for the configurable parameter length (1MB).
/// A misconfigured `DJANGO_BOLT_MAX_PARAM_LENGTH` (e.g. an enormous value) must
/// not be able to nullify the DoS guardrail, so a larger value is an error.
pub const MAX_ALLOWED_PARAM_LENGTH: usize = 1024 * 1024;

#[inline]
fn parse_max_param_length(value: Option<&str>) -> Result<usize, String> {
    Ok(crate::settings::parse_env(
        "DJANGO_BOLT_MAX_PARAM_LENGTH",
        value,
        "an int from 1 to 1048576",
        |parsed: &usize| (1..=MAX_ALLOWED_PARAM_LENGTH).contains(parsed),
    )?
    .unwrap_or(DEFAULT_MAX_PARAM_LENGTH))
}

/// Resolve the configured maximum parameter length from the
/// `DJANGO_BOLT_MAX_PARAM_LENGTH` environment variable.
///
/// Call this exactly ONCE at server startup and store the result in
/// `AppState.max_param_length`. The per-request hot path then reads that plain
/// field — no env access, no lock, no atomics. An unset or empty value gives
/// [`DEFAULT_MAX_PARAM_LENGTH`]. Any other value that is not an int from 1 to
/// [`MAX_ALLOWED_PARAM_LENGTH`] raises `ImproperlyConfigured`.
pub fn resolve_max_param_length(py: Python<'_>) -> PyResult<usize> {
    let raw = crate::settings::read_env(
        py,
        "DJANGO_BOLT_MAX_PARAM_LENGTH",
        "an int from 1 to 1048576",
    )?;
    parse_max_param_length(raw.as_deref())
        .map_err(|message| crate::settings::improperly_configured(py, message))
}

/// Validate a decimal literal without parsing it into a numeric value.
///
/// Grammar (a strict subset of what Python's `decimal.Decimal` accepts, so any
/// accepted string constructs cleanly on the Python side):
///     [+|-] ( digits [. digits] | digits . | . digits ) [ (e|E) [+|-] digits ]
///
/// NaN/Infinity are rejected (HTTP parameters should be finite numbers), as are
/// whitespace and underscores. Validation-only keeps this zero-alloc and lets
/// the exponent stay textual: "1e999999999" is never expanded into its digit
/// string, and Python's Decimal stores the exponent compactly.
#[inline]
fn is_valid_decimal_literal(s: &str) -> bool {
    let b = s.as_bytes();
    let mut i = 0;
    if let Some(&c) = b.first()
        && (c == b'+' || c == b'-')
    {
        i = 1;
    }
    let mut mantissa_digits = 0;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
        mantissa_digits += 1;
    }
    if i < b.len() && b[i] == b'.' {
        i += 1;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
            mantissa_digits += 1;
        }
    }
    if mantissa_digits == 0 {
        return false;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let exponent_start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == exponent_start {
            return false;
        }
    }
    i == b.len()
}

/// Largest adjusted exponent that Python's `decimal` module accepts (`MAX_EMAX`
/// of libmpdec on a 64-bit build). The mantissa has at most `max_length` digits,
/// so the bound leaves room for it.
const PYTHON_DECIMAL_MAX_EMAX: u64 = 999_999_999_999_999_999;

/// Check that Python can build a `Decimal` from a valid literal.
///
/// `Decimal("1E999999999999999999999")` raises `InvalidOperation`, because the
/// exponent does not fit. Such a value must be a 422, not a Python error
/// under the GIL.
fn decimal_exponent_fits(s: &str) -> bool {
    let Some(pos) = s.find(['e', 'E']) else {
        return true;
    };
    let exponent = &s[pos + 1..];
    let digits = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
    match digits.parse::<u64>() {
        Ok(magnitude) => magnitude.saturating_add(s.len() as u64) <= PYTHON_DECIMAL_MAX_EMAX,
        Err(_) => false,
    }
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
    /// The text of an integer beyond `i64`: an optional `-` and ASCII digits,
    /// at most the digit limit of [`resolve_int_max_str_digits`].
    BigInt(String),
    Float(f64),
    Bool(bool),
    String(String),
    Uuid(Uuid),
    /// A datetime with the UTC offset of its text.
    DateTime(NaiveDateTime, UtcOffset),
    NaiveDateTime(NaiveDateTime),
    Date(NaiveDate),
    Time(NaiveTime),
    /// A time with the UTC offset of its text.
    TimeTz(NaiveTime, UtcOffset),
    // Holds the original input string, validated by is_valid_decimal_literal().
    // Python's decimal.Decimal is constructed from this string directly, so a
    // large exponent (e.g. "1e999999999") is never expanded into its full digit
    // string in Rust.
    Decimal(String),
}

/// Error returned by [`coerce_param`].
///
/// * [`CoerceError::TooLong`] is a hard security limit. Never pass the
///   oversized value through.
/// * [`CoerceError::Invalid`] is a normal type-coercion failure.
///
/// Callers reject both variants: a 422 for HTTP, a rejected upgrade for
/// WebSockets. No caller gives the raw string to the handler.
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

/// Coerce a string value to the specified type, with the startup-resolved
/// length limit (`AppState.max_param_length`) so the hot path never
/// re-resolves config.
pub fn coerce_param(
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
        TYPE_INT => parse_int(value),

        TYPE_FLOAT => parse_float(value),

        TYPE_BOOL => {
            // Compare without case. This does not allocate a lowercase copy of the value.
            let is_true = ["true", "1", "yes", "on"]
                .iter()
                .any(|token| value.eq_ignore_ascii_case(token));
            let is_false = !is_true
                && ["false", "0", "no", "off"]
                    .iter()
                    .any(|token| value.eq_ignore_ascii_case(token));
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

        TYPE_DATETIME => parse_datetime(value).ok_or_else(|| {
            format!(
                "Invalid datetime '{}': expected ISO 8601 format (e.g., 2024-01-15T10:30:00Z)",
                value
            )
        }),

        TYPE_DECIMAL => {
            if !is_valid_decimal_literal(value) {
                Err(format!("Invalid decimal '{}'", value))
            } else if !decimal_exponent_fits(value) {
                Err(format!(
                    "Invalid decimal '{}': exponent is out of range",
                    value
                ))
            } else {
                Ok(CoercedValue::Decimal(value.to_string()))
            }
        }

        TYPE_DATE => parse_date(value.as_bytes())
            .map(CoercedValue::Date)
            .ok_or_else(|| {
                format!(
                    "Invalid date '{}': expected ISO 8601 format (e.g., 2024-01-15)",
                    value
                )
            }),

        TYPE_TIME => parse_time(value.as_bytes()).ok_or_else(|| {
            format!(
                "Invalid time '{}': expected ISO 8601 format (e.g., 10:30 or 10:30:00+05:30)",
                value
            )
        }),

        _ => Ok(CoercedValue::String(value.to_string())),
    }
}

// The parsers below follow Python: `int()` and `float()` for the number
// texts, and `fromisoformat()` of `date`, `time` and `datetime` for the ISO
// 8601 texts. Python 3.12, 3.13 and 3.14 must each accept a text and give the
// same value. Thus Bolt refuses hour 24 (only 3.14 reads it) and a fraction
// of an hour or a minute (3.14 refuses it). Bolt also refuses some texts that
// Python reads but that are not ISO 8601: whitespace, `_` or other digits than
// ASCII in a number, a date and time separator other than `T`, `t` or a space,
// text between the time and the offset, an offset field out of range, seconds
// in an offset, and a NUL character. Each parser reads the text once, with no
// Python call.

/// The most digits that an `int` text can have: the default of
/// `sys.get_int_max_str_digits()`. A higher limit, or no limit, keeps this
/// bound, so the build of a Python `int` stays short under the GIL.
pub const MAX_INT_DIGITS: usize = 4300;

/// The lowest digit limit that Python allows, other than no limit. Each limit
/// accepts a text of this length.
const MIN_INT_DIGITS_LIMIT: usize = 640;

/// The digit limit of an `int` text. [`resolve_int_max_str_digits`] sets it.
static INT_MAX_DIGITS: AtomicUsize = AtomicUsize::new(MAX_INT_DIGITS);

/// Read the digit limit of `int()` from `sys.get_int_max_str_digits()`.
///
/// The server and the test client call this once, when they start. A later
/// call of `sys.set_int_max_str_digits()` does not change the limit of Bolt.
/// A process has one limit, as Python has one for each interpreter. Thus the
/// last server or test client that starts sets the limit for all of them.
pub fn resolve_int_max_str_digits(py: Python<'_>) -> PyResult<()> {
    let limit: usize = py
        .import("sys")?
        .call_method0("get_int_max_str_digits")?
        .extract()?;
    let limit = if limit == 0 {
        MAX_INT_DIGITS
    } else {
        limit.min(MAX_INT_DIGITS)
    };
    INT_MAX_DIGITS.store(limit, Ordering::Relaxed);
    Ok(())
}

/// Parse an `int` as `int()` parses ASCII text: an optional sign, then digits.
/// An integer beyond `i64` keeps its text for Python.
fn parse_int(value: &str) -> Result<CoercedValue, String> {
    let parsed = value.parse::<i64>();
    if let Ok(int) = parsed
        && value.len() <= MIN_INT_DIGITS_LIMIT
    {
        return Ok(CoercedValue::Int(int));
    }
    let digits = value.strip_prefix(['+', '-']).unwrap_or(value);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return Err(format!(
            "Invalid integer '{}': expected a whole number such as 42 or -7",
            value
        ));
    }
    let limit = INT_MAX_DIGITS.load(Ordering::Relaxed);
    if digits.len() > limit {
        return Err(format!(
            "Invalid integer '{}': more than {} digits",
            value, limit
        ));
    }
    Ok(match parsed {
        Ok(int) => CoercedValue::Int(int),
        // Only an overflow gets here: each character is a digit.
        Err(_) => CoercedValue::BigInt(value.strip_prefix('+').unwrap_or(value).to_owned()),
    })
}

/// Parse a `float` with the Rust parser. It reads the ASCII texts that
/// `float()` reads and gives the same value: an optional sign, digits with an
/// optional point and exponent, or `inf`, `infinity` or `nan` in any case.
fn parse_float(value: &str) -> Result<CoercedValue, String> {
    value.parse::<f64>().map(CoercedValue::Float).map_err(|_| {
        format!(
            "Invalid float '{}': expected a number such as 1.5, -2e3, inf or nan",
            value
        )
    })
}

/// A UTC offset in whole minutes, less than 24 hours. ISO 8601 and RFC 3339
/// give an offset no seconds field, so Bolt refuses one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UtcOffset {
    pub minutes: i32,
}

impl UtcOffset {
    /// The offset of `Z`, `+00:00` and `-00:00`. Python gives `timezone.utc` for it.
    pub const UTC: Self = Self { minutes: 0 };
}

fn digit(c: u8) -> Option<u32> {
    c.is_ascii_digit().then(|| u32::from(c - b'0'))
}

fn pair(high: u8, low: u8) -> Option<u32> {
    Some(digit(high)? * 10 + digit(low)?)
}

/// Read two ASCII digits at `at`.
fn two_digits(text: &[u8], at: usize) -> Option<u32> {
    match *text.get(at..at + 2)? {
        [high, low] => pair(high, low),
        _ => None,
    }
}

/// Parse a date: `YYYY-MM-DD`, `YYYYMMDD`, or a week date `YYYY-Www`,
/// `YYYY-Www-D`, `YYYYWww` or `YYYYWwwD`. The year is from 1 to 9999.
fn parse_date(text: &[u8]) -> Option<NaiveDate> {
    let year = i32::try_from(two_digits(text, 0)? * 100 + two_digits(text, 2)?).ok()?;
    let date = match text[4..] {
        [b'-', b'W', w0, w1] | [b'W', w0, w1] => week_date(year, pair(w0, w1)?, 1),
        [b'-', b'W', w0, w1, b'-', day] | [b'W', w0, w1, day] => {
            week_date(year, pair(w0, w1)?, digit(day)?)
        }
        [b'-', m0, m1, b'-', d0, d1] | [m0, m1, d0, d1] => {
            NaiveDate::from_ymd_opt(year, pair(m0, m1)?, pair(d0, d1)?)
        }
        _ => None,
    }?;
    (1..=9999).contains(&date.year()).then_some(date)
}

/// The date of day `day` (1 is Monday) of ISO week `week` of `year`.
fn week_date(year: i32, week: u32, day: u32) -> Option<NaiveDate> {
    let weekday = Weekday::try_from(u8::try_from(day.checked_sub(1)?).ok()?).ok()?;
    NaiveDate::from_isoywd_opt(year, week, weekday)
}

/// Parse `HH`, `HH:MM`, `HH:MM:SS`, `HHMM` or `HHMMSS`, then an optional
/// fraction of the second: `.` or `,` and one digit or more. Python drops the
/// digits after the sixth. Give the hours, minutes, seconds and microseconds.
/// The caller checks the range of each field.
fn parse_hms(text: &[u8]) -> Option<(u32, u32, u32, u32)> {
    let hour = two_digits(text, 0)?;
    let (minute_at, extended) = match text.get(2) {
        None => return Some((hour, 0, 0, 0)),
        Some(b':') => (3, true),
        Some(_) => (2, false),
    };
    let minute = two_digits(text, minute_at)?;
    let second_at = match text.get(minute_at + 2) {
        None => return Some((hour, minute, 0, 0)),
        Some(b':') if extended => minute_at + 3,
        Some(_) if !extended => minute_at + 2,
        Some(_) => return None,
    };
    let second = two_digits(text, second_at)?;
    let micro = match text[second_at + 2..] {
        [] => 0,
        [b'.' | b',', ref fraction @ ..]
            if !fraction.is_empty() && fraction.iter().all(u8::is_ascii_digit) =>
        {
            fraction
                .iter()
                .chain(std::iter::repeat(&b'0'))
                .take(6)
                .fold(0, |total, &c| total * 10 + u32::from(c - b'0'))
        }
        _ => return None,
    };
    Some((hour, minute, second, micro))
}

/// Parse a time of day with an optional UTC offset: `Z`, or a sign and the
/// form of [`parse_offset`].
fn parse_clock(text: &[u8]) -> Option<(NaiveTime, Option<UtcOffset>)> {
    let zone_at = text
        .iter()
        .position(|c| matches!(c, b'Z' | b'+' | b'-'))
        .unwrap_or(text.len());
    let (hour, minute, second, micro) = parse_hms(&text[..zone_at])?;
    // chrono refuses hour 24 and second 60, as Python 3.12 and 3.13 do.
    let time = NaiveTime::from_hms_micro_opt(hour, minute, second, micro)?;
    let offset = match text[zone_at..] {
        [] => None,
        [b'Z'] => Some(UtcOffset::UTC),
        [sign @ (b'+' | b'-'), ref zone @ ..] => Some(parse_offset(sign, zone)?),
        _ => return None,
    };
    Some((time, offset))
}

/// Parse an offset after its sign: `HH`, `HHMM` or `HH:MM`. Python also reads
/// seconds and a fraction, and `+05:60` as six hours. Bolt refuses them: the
/// JSON encoder writes an offset in whole minutes, so a second would change
/// the instant in a response.
fn parse_offset(sign: u8, zone: &[u8]) -> Option<UtcOffset> {
    let (hours, minutes) = match *zone {
        [h0, h1] => (pair(h0, h1)?, 0),
        [h0, h1, m0, m1] | [h0, h1, b':', m0, m1] => (pair(h0, h1)?, pair(m0, m1)?),
        _ => return None,
    };
    if hours > 23 || minutes > 59 {
        return None;
    }
    let minutes = (hours * 60 + minutes) as i32;
    Some(UtcOffset {
        minutes: if sign == b'-' { -minutes } else { minutes },
    })
}

/// Parse a `time`: an optional `T`, then the form of [`parse_clock`].
fn parse_time(text: &[u8]) -> Option<CoercedValue> {
    let (time, offset) = parse_clock(text.strip_prefix(b"T").unwrap_or(text))?;
    Some(match offset {
        None => CoercedValue::Time(time),
        Some(offset) => CoercedValue::TimeTz(time, offset),
    })
}

/// Parse a `datetime`: a date of [`parse_date`], then optionally a separator
/// and the form of [`parse_clock`]. A date alone is midnight. The separator is
/// `T`, `t` or one whitespace character, such as a space or a tab. Python
/// takes any one character; chrono took any whitespace, so a tab stays valid.
fn parse_datetime(text: &str) -> Option<CoercedValue> {
    let date_len = text
        .bytes()
        .position(|c| !matches!(c, b'0'..=b'9' | b'-' | b'W'))
        .unwrap_or(text.len());
    let date = parse_date(&text.as_bytes()[..date_len])?;
    // Each byte before `date_len` is ASCII, so a character starts there.
    let mut rest = text[date_len..].chars();
    match rest.next() {
        None => return Some(CoercedValue::NaiveDateTime(date.and_time(NaiveTime::MIN))),
        Some(separator) if separator == 'T' || separator == 't' || separator.is_whitespace() => {}
        Some(_) => return None,
    }
    let (time, offset) = parse_clock(rest.as_str().as_bytes())?;
    let local = date.and_time(time);
    Some(match offset {
        None => CoercedValue::NaiveDateTime(local),
        Some(offset) => CoercedValue::DateTime(local, offset),
    })
}

/// Build a Python `int` from the text of a [`CoercedValue::BigInt`].
///
/// `int(text)` refuses more digits than `sys.get_int_max_str_digits()`, and an
/// application can lower that limit after Bolt read it. Integer arithmetic on
/// chunks of 18 digits has no such limit, so a value that Rust accepted
/// always converts.
fn big_int_to_py<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    const CHUNK: usize = 18;
    let (negative, digits) = match text.strip_prefix('-') {
        Some(digits) => (true, digits),
        None => (false, text),
    };
    let mut value = 0u64.into_pyobject(py)?.into_any();
    let mut start = 0;
    while start < digits.len() {
        let end = match (digits.len() - start) % CHUNK {
            0 => start + CHUNK,
            head => start + head,
        };
        let chunk = &digits[start..end];
        let chunk_value: u64 = chunk
            .parse()
            .map_err(|_| PyValueError::new_err(format!("invalid integer text '{text}'")))?;
        value = value.mul(10u64.pow(chunk.len() as u32))?.add(chunk_value)?;
        start = end;
    }
    if negative { value.neg() } else { Ok(value) }
}

/// The largest offset of whole minutes, in minutes: 23:59.
const MAX_OFFSET_MINUTES: i32 = 24 * 60 - 1;

/// One `timezone` for each offset, made at its first use.
/// A `timezone` costs a `timedelta` and a constructor call under the GIL.
static TZINFO_BY_MINUTE: [PyOnceLock<Py<PyTzInfo>>; 2 * MAX_OFFSET_MINUTES as usize + 1] =
    [const { PyOnceLock::new() }; 2 * MAX_OFFSET_MINUTES as usize + 1];

/// Give the Python `tzinfo` of an offset. UTC is the cached
/// `datetime.timezone.utc`, as `fromisoformat()` gives.
fn tzinfo_to_py(py: Python<'_>, offset: UtcOffset) -> PyResult<Bound<'_, PyTzInfo>> {
    if offset == UtcOffset::UTC {
        return Ok(PyTzInfo::utc(py)?.to_owned());
    }
    let slot = &TZINFO_BY_MINUTE[(offset.minutes + MAX_OFFSET_MINUTES) as usize];
    let tzinfo = slot.get_or_try_init(py, || {
        PyTzInfo::fixed_offset(py, PyDelta::new(py, 0, offset.minutes * 60, 0, true)?)
            .map(Bound::unbind)
    })?;
    Ok(tzinfo.bind(py).clone())
}

/// Microseconds of a time that [`parse_clock`] built. No leap second occurs.
fn micros(time: NaiveTime) -> u32 {
    time.nanosecond() / 1000
}

/// Convert the text items of a sequence parameter with the parser of one `T`
/// value, so that a sequence and a single value of one type accept the same text.
///
/// The extractors call this at most once for each parameter, with the type
/// hint and the length bounds that the route registration found. A length out
/// of `min_length..=max_length` gives `items` itself, and no item is read.
/// msgspec then refuses the list with its own error, as it checks the length
/// before it reads an item.
///
/// The items are read in order. The first text that does not convert raises
/// `ValueError(index, message)`. An item that is not text gives `items`
/// itself, for msgspec to refuse, but only when no earlier text was refused.
/// Only a form model with a file field gives such an item: a file part with
/// the name of the sequence field.
#[pyfunction]
#[pyo3(signature = (items, type_hint, min_length = 0, max_length = usize::MAX))]
pub fn parse_text_items<'py>(
    items: &Bound<'py, PyList>,
    type_hint: u8,
    min_length: usize,
    max_length: usize,
) -> PyResult<Bound<'py, PyList>> {
    let py = items.py();
    if !(min_length..=max_length).contains(&items.len()) {
        return Ok(items.clone());
    }
    let converted = PyList::empty(py);
    for (index, item) in items.iter().enumerate() {
        let Ok(text) = item.cast::<PyString>() else {
            return Ok(items.clone());
        };
        match coerce_typed(&text.to_string_lossy(), type_hint) {
            Ok(value) => converted.append(coerced_value_to_py(py, &value)?)?,
            Err(message) => return Err(PyValueError::new_err((index, message))),
        }
    }
    Ok(converted)
}

/// Non-string type hints of one request source, keyed by wire name.
pub type TypeHints = ahash::AHashMap<String, u8>;

/// Pre-coerced values of one request source. The names borrow from the
/// route metadata, so no name is copied per request and the request map
/// stays free to move.
pub type CoercedValues<'a> = Vec<(&'a str, CoercedValue)>;

/// Build a Python dict from a map of string values.
///
/// A value in `coerced` replaces the string for its name. The request pipeline
/// validates and coerces those values before the GIL is taken, so this step
/// cannot fail on bad input. Used for path, query, header and cookie values.
///
/// Two passes keep the cost linear: every string goes in first, then each
/// coerced value overwrites its string. The overwritten strings are short,
/// so this is cheaper than a lookup per value.
///
/// Names are not interned: interning costs a lookup per call, and interned
/// client-supplied names would never be freed.
///
/// `values` can own its strings (headers, cookies) or borrow them (query).
#[inline]
pub fn string_map_to_py_dict<'py, K, V>(
    py: Python<'py>,
    values: &ahash::AHashMap<K, V>,
    coerced: &[(&str, CoercedValue)],
) -> PyResult<pyo3::Bound<'py, PyDict>>
where
    K: std::borrow::Borrow<str>,
    V: AsRef<str>,
{
    let dict = PyDict::new(py);
    for (name, value) in values {
        let name: &str = name.borrow();
        dict.set_item(name, value.as_ref())?;
    }
    for (name, typed) in coerced {
        dict.set_item(name, coerced_value_to_py(py, typed)?)?;
    }
    Ok(dict)
}

/// Convert CoercedValue to Python object
///
/// Constructs actual Python typed objects (uuid.UUID, decimal.Decimal, datetime, etc.)
/// directly — datetime/date/time go through PyO3's chrono integration (C-API
/// construction, no ISO-string round trip) and UUID is built from its 128-bit
/// value instead of re-parsing a hex string in Python.
///
/// `coerce_param` rejects the values that Python cannot build, so this fails
/// only on a Python error such as memory exhaustion. The error is returned,
/// never unwrapped: a panic under the GIL would abort the worker.
pub fn coerced_value_to_py(py: Python<'_>, value: &CoercedValue) -> PyResult<Py<PyAny>> {
    Ok(match value {
        // Primitives - direct conversion
        CoercedValue::Int(v) => v.into_pyobject(py)?.into_any().unbind(),
        CoercedValue::Float(v) => v.into_pyobject(py)?.into_any().unbind(),
        CoercedValue::Bool(v) => v.into_pyobject(py)?.to_owned().unbind().into_any(),
        CoercedValue::String(v) => v.into_pyobject(py)?.into_any().unbind(),

        // UUID: built from the 128-bit integer, no hex string round trip.
        CoercedValue::Uuid(v) => uuid_to_py(py, *v)?,

        // Decimal: construct Python decimal.Decimal from the validated input
        // string (kept as-is so large exponents are never expanded into their
        // full digit string in Rust).
        CoercedValue::Decimal(v) => get_decimal_class(py).call1(py, (v.as_str(),))?,

        CoercedValue::BigInt(v) => big_int_to_py(py, v)?.unbind(),

        // Temporal types: direct C-API construction via pyo3's chrono feature.
        // An aware value keeps the fields and the offset of its text.
        CoercedValue::DateTime(local, offset) => PyDateTime::new(
            py,
            local.year(),
            local.month() as u8,
            local.day() as u8,
            local.hour() as u8,
            local.minute() as u8,
            local.second() as u8,
            micros(local.time()),
            Some(&tzinfo_to_py(py, *offset)?),
        )?
        .into_any()
        .unbind(),
        CoercedValue::NaiveDateTime(v) => v.into_pyobject(py)?.into_any().unbind(),
        CoercedValue::Date(v) => v.into_pyobject(py)?.into_any().unbind(),
        CoercedValue::Time(v) => v.into_pyobject(py)?.into_any().unbind(),
        CoercedValue::TimeTz(time, offset) => PyTime::new(
            py,
            time.hour() as u8,
            time.minute() as u8,
            time.second() as u8,
            micros(*time),
            Some(&tzinfo_to_py(py, *offset)?),
        )?
        .into_any()
        .unbind(),
    })
}

/// Build a Python `uuid.UUID` from the parsed 128-bit value.
///
/// `uuid.UUID(int=...)` runs the Python `__init__`, which checks its arguments
/// again. This function builds the object as CPython 3.14 `UUID._from_int` does:
/// `object.__new__(UUID)`, then it sets the `int` and `is_safe` slots.
/// `UUID.__setattr__` blocks assignment, so the slots are set with the generic
/// setter, as `object.__setattr__` does. The 128-bit value is always in range.
#[inline]
pub fn uuid_to_py(py: Python<'_>, v: uuid::Uuid) -> PyResult<Py<PyAny>> {
    let object_new = OBJECT_NEW.get_or_init(py, || {
        py.import("builtins")
            .unwrap()
            .getattr("object")
            .unwrap()
            .getattr("__new__")
            .unwrap()
            .unbind()
    });
    let safe_unknown = SAFE_UUID_UNKNOWN.get_or_init(py, || {
        py.import("uuid")
            .unwrap()
            .getattr("SafeUUID")
            .unwrap()
            .getattr("unknown")
            .unwrap()
            .unbind()
    });
    let value = object_new.bind(py).call1((get_uuid_class(py).bind(py),))?;
    let int = v.as_u128().into_pyobject(py)?;
    set_slot(&value, pyo3::intern!(py, "int"), int.as_any())?;
    set_slot(&value, pyo3::intern!(py, "is_safe"), safe_unknown.bind(py))?;
    Ok(value.unbind())
}

/// Set an attribute with the generic setter, as `object.__setattr__` does.
#[inline]
fn set_slot(
    object: &pyo3::Bound<'_, PyAny>,
    name: &pyo3::Bound<'_, pyo3::types::PyString>,
    value: &pyo3::Bound<'_, PyAny>,
) -> PyResult<()> {
    // SAFETY: the three pointers are valid for the call: the Bound references keep them alive.
    let status = unsafe {
        pyo3::ffi::PyObject_GenericSetAttr(object.as_ptr(), name.as_ptr(), value.as_ptr())
    };
    if status == 0 {
        Ok(())
    } else {
        Err(pyo3::PyErr::fetch(object.py()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeDelta;
    use proptest::prelude::*;

    /// The decimal grammar of `is_valid_decimal_literal`, as a regex.
    const DECIMAL_LITERAL: &str = r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?";

    fn coerce(value: &str, type_hint: u8) -> Result<CoercedValue, CoerceError> {
        coerce_param(value, type_hint, DEFAULT_MAX_PARAM_LENGTH)
    }

    proptest! {
        #[test]
        fn coercion_never_panics(value in any::<String>(), type_hint in 0u8..=10) {
            let _ = coerce(&value, type_hint);
        }

        #[test]
        fn a_value_over_the_limit_is_too_long(value in ".{9,40}", type_hint in 0u8..=10) {
            let result = coerce_param(&value, type_hint, 8);
            prop_assert!(matches!(result, Err(CoerceError::TooLong { max: 8, .. })), "{:?}", result);
        }

        #[test]
        fn an_int_round_trips(value in any::<i64>()) {
            prop_assert!(matches!(coerce(&value.to_string(), TYPE_INT), Ok(CoercedValue::Int(v)) if v == value));
        }

        #[test]
        fn a_float_round_trips(value in any::<f64>()) {
            let Ok(CoercedValue::Float(parsed)) = coerce(&value.to_string(), TYPE_FLOAT) else {
                return Err(TestCaseError::fail(format!("{value} was refused")));
            };
            prop_assert!(parsed.to_bits() == value.to_bits() || (parsed.is_nan() && value.is_nan()));
        }

        #[test]
        fn a_uuid_round_trips_in_each_form(bits in any::<u128>()) {
            let value = Uuid::from_u128(bits);
            for text in [
                value.hyphenated().to_string(),
                value.simple().to_string(),
                value.urn().to_string(),
                value.braced().to_string(),
                value.hyphenated().to_string().to_uppercase(),
            ] {
                prop_assert!(matches!(coerce(&text, TYPE_UUID), Ok(CoercedValue::Uuid(v)) if v == value), "{}", text);
            }
        }

        #[test]
        fn a_date_round_trips_or_is_out_of_python_range(days in -800_000i32..4_500_000) {
            let date = NaiveDate::from_num_days_from_ce_opt(days).unwrap();
            let text = date.format("%Y-%m-%d").to_string();
            match coerce(&text, TYPE_DATE) {
                Ok(CoercedValue::Date(parsed)) => prop_assert_eq!(parsed, date),
                Err(error) => prop_assert!(!(1..=9999).contains(&date.year()), "{}: {}", text, error),
                Ok(other) => prop_assert!(false, "{:?}", other),
            }
        }

        #[test]
        fn a_time_round_trips(hour in 0u32..24, minute in 0u32..60, second in 0u32..60, micro in 0u32..1_000_000) {
            let time = NaiveTime::from_hms_micro_opt(hour, minute, second, micro).unwrap();
            let text = time.format("%H:%M:%S%.6f").to_string();
            prop_assert!(matches!(coerce(&text, TYPE_TIME), Ok(CoercedValue::Time(v)) if v == time));
        }

        #[test]
        fn a_naive_datetime_round_trips(
            days in 365i32..3_652_000,
            seconds in 0u32..86_400,
            micro in 0u32..1_000_000,
        ) {
            let date = NaiveDate::from_num_days_from_ce_opt(days).unwrap();
            let time = NaiveTime::from_num_seconds_from_midnight_opt(seconds, micro * 1000).unwrap();
            let value = date.and_time(time);
            let text = value.format("%Y-%m-%dT%H:%M:%S%.6f").to_string();
            prop_assert!(matches!(coerce(&text, TYPE_DATETIME), Ok(CoercedValue::NaiveDateTime(v)) if v == value));
        }

        #[test]
        fn a_literal_of_the_grammar_is_a_decimal(value in DECIMAL_LITERAL) {
            let result = coerce(&value, TYPE_DECIMAL);
            if decimal_exponent_fits(&value) {
                prop_assert!(matches!(result, Ok(CoercedValue::Decimal(ref v)) if *v == value));
            } else {
                prop_assert!(result.is_err());
            }
        }

        /// Python `Decimal` must accept each value that Bolt accepts.
        #[test]
        fn an_accepted_decimal_is_of_the_grammar(value in "[0-9eE+.-]{0,12}|.{0,12}") {
            let grammar = regex::Regex::new(&format!("^(?:{DECIMAL_LITERAL})$")).unwrap();
            if coerce(&value, TYPE_DECIMAL).is_ok() {
                prop_assert!(grammar.is_match(&value), "{}", value);
            }
        }

        #[test]
        fn an_int_text_round_trips(value in any::<i128>()) {
            let text = value.to_string();
            match coerce(&text, TYPE_INT) {
                Ok(CoercedValue::Int(int)) => prop_assert_eq!(i128::from(int), value),
                Ok(CoercedValue::BigInt(big)) => {
                    prop_assert!(i64::try_from(value).is_err());
                    prop_assert_eq!(big, text);
                }
                other => prop_assert!(false, "{}: {:?}", text, other),
            }
        }

        /// A date converts in each form that `date.fromisoformat()` reads.
        #[test]
        fn a_date_converts_in_each_form(days in 1i32..=3_652_059) {
            let date = NaiveDate::from_num_days_from_ce_opt(days).unwrap();
            let week = date.iso_week();
            let day = date.weekday().number_from_monday();
            let monday = date - TimeDelta::days(i64::from(day) - 1);
            for (text, expected) in [
                (date.format("%Y-%m-%d").to_string(), date),
                (date.format("%Y%m%d").to_string(), date),
                (format!("{:04}-W{:02}-{day}", week.year(), week.week()), date),
                (format!("{:04}W{:02}{day}", week.year(), week.week()), date),
                (format!("{:04}-W{:02}", week.year(), week.week()), monday),
                (format!("{:04}W{:02}", week.year(), week.week()), monday),
            ] {
                prop_assert!(
                    matches!(coerce(&text, TYPE_DATE), Ok(CoercedValue::Date(v)) if v == expected),
                    "{}: {:?}", text, coerce(&text, TYPE_DATE)
                );
                let midnight = expected.and_time(NaiveTime::MIN);
                prop_assert!(
                    matches!(coerce(&text, TYPE_DATETIME), Ok(CoercedValue::NaiveDateTime(v)) if v == midnight),
                    "{}", text
                );
            }
        }

        /// A time converts in each form that `time.fromisoformat()` reads, alone,
        /// after `T`, and after a date and a separator. A fraction keeps its
        /// first six digits. An offset keeps its fields. An offset with
        /// seconds is refused.
        #[test]
        fn a_time_converts_in_each_form(
            (hour, minute, second) in (0u32..24, 0u32..60, 0u32..60),
            precision in 0usize..3,
            extended in any::<bool>(),
            fraction in "([.,][0-9]{1,9})?",
            zone in concat!(
                "|Z|[+-]([01][0-9]|2[0-3])(:[0-5][0-9](:[0-5][0-9]([.,][0-9]{1,9})?)?)?",
                "|[+-]([01][0-9]|2[0-3])([0-5][0-9]([0-5][0-9]([.,][0-9]{1,9})?)?)?",
            ),
            separator in "[Tt \\t\\n\u{a0}\u{3000}]",
            days in 1i32..=3_652_059,
        ) {
            let colon = if extended { ":" } else { "" };
            let fields = [hour, minute, second];
            let mut text = format!("{hour:02}");
            for field in &fields[1..=precision] {
                text.push_str(&format!("{colon}{field:02}"));
            }
            let fraction = if precision == 2 { fraction } else { String::new() };
            text.push_str(&fraction);
            text.push_str(&zone);
            let kept = |index: usize| if index <= precision { fields[index] } else { 0 };
            let time = NaiveTime::from_hms_micro_opt(kept(0), kept(1), kept(2), micros_of(&fraction)).unwrap();
            let offset = offset_of(&zone);
            let date = NaiveDate::from_num_days_from_ce_opt(days).unwrap();
            let datetime_text = format!("{}{separator}{text}", date.format("%Y-%m-%d"));
            let timed = [coerce(&text, TYPE_TIME), coerce(&format!("T{text}"), TYPE_TIME)];
            let dated = coerce(&datetime_text, TYPE_DATETIME);
            match offset {
                // An offset with seconds is refused.
                None if !zone.is_empty() => {
                    prop_assert!(timed.iter().all(Result::is_err), "{}", text);
                    prop_assert!(dated.is_err(), "{}", datetime_text);
                }
                None => {
                    for result in timed {
                        prop_assert!(matches!(result, Ok(CoercedValue::Time(v)) if v == time), "{}", text);
                    }
                    prop_assert!(
                        matches!(dated, Ok(CoercedValue::NaiveDateTime(v)) if v == date.and_time(time)),
                        "{}", datetime_text
                    );
                }
                Some(offset) => {
                    for result in timed {
                        prop_assert!(
                            matches!(result, Ok(CoercedValue::TimeTz(v, o)) if v == time && o == offset),
                            "{}", text
                        );
                    }
                    prop_assert!(
                        matches!(dated, Ok(CoercedValue::DateTime(v, o)) if v == date.and_time(time) && o == offset),
                        "{}", datetime_text
                    );
                }
            }
        }

        /// No field of another width, no sign before a year, no whitespace and
        /// no other separator converts (F2).
        #[test]
        fn an_accepted_text_is_of_the_grammar(
            value in concat!(
                "[ \\t+-]?[0-9]{1,5}-?W?[0-9]{0,3}-?[0-9]{0,3}",
                "([Tt _x\\t\u{3000}]?[0-9]{1,3}:?[0-9]{0,3}(:?[0-9]{0,3})?([.,][0-9]{0,8})?[ x]?([Zz]|[+-][0-9]{1,3}:?[0-9]{0,3}(:?[0-9]{0,2}([.,][0-9]{0,3})?)?)?)?[ \\t]?",
                "|[ \\t+Tt-]?[0-9]{1,3}:?[0-9]{0,3}(:?[0-9]{0,3})?([.,][0-9]{0,8})?[ x]?([Zz]|[+-][0-9]{1,3}:?[0-9]{0,3})?[ \\t]?",
                "|[ \\t+-]?[0-9_]{1,6}([.,][0-9]{0,3})?([eE][+-]?[0-9]{0,3})?[ \\t]?",
                "|.{0,24}",
            ),
        ) {
            let date = format!("^{DATE_GRAMMAR}$");
            let time = format!("^T?{CLOCK_GRAMMAR}$");
            let datetime = format!("^{DATE_GRAMMAR}([Tt\\s]{CLOCK_GRAMMAR})?$");
            for (type_hint, grammar) in [
                (TYPE_DATE, date),
                (TYPE_TIME, time),
                (TYPE_DATETIME, datetime),
                (TYPE_INT, format!("^{INT_GRAMMAR}$")),
                (TYPE_FLOAT, format!("^{FLOAT_GRAMMAR}$")),
            ] {
                if coerce(&value, type_hint).is_ok() {
                    let grammar = regex::Regex::new(&grammar).unwrap();
                    prop_assert!(grammar.is_match(&value), "{:?} as {}", value, type_hint);
                }
            }
        }
    }

    /// The grammar of the texts that Bolt reads, as regexes.
    const DATE_GRAMMAR: &str =
        "[0-9]{4}(-[0-9]{2}-[0-9]{2}|[0-9]{4}|-?W[0-9]{2}|-W[0-9]{2}-[0-9]|W[0-9]{3})";
    const CLOCK_GRAMMAR: &str = concat!(
        "[0-9]{2}(:[0-9]{2}(:[0-9]{2}([.,][0-9]+)?)?|[0-9]{2}([0-9]{2}([.,][0-9]+)?)?)?",
        "(Z|[+-][0-9]{2}(:?[0-9]{2})?)?",
    );
    const INT_GRAMMAR: &str = "[+-]?[0-9]+";
    const FLOAT_GRAMMAR: &str =
        r"[+-]?(([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?|(?i:inf|infinity|nan))";

    /// The microseconds of a fraction text such as ".1234567": its first six digits.
    fn micros_of(fraction: &str) -> u32 {
        let digits = fraction.get(1..).unwrap_or("");
        let kept = &digits[..digits.len().min(6)];
        format!("{kept:0<6}").parse().unwrap()
    }

    /// The offset of a zone text such as "Z", "+05:30", "-0530" or "+05".
    /// None for no zone, and for a zone with seconds, such as "+05:30:15".
    fn offset_of(zone: &str) -> Option<UtcOffset> {
        let sign = match zone.as_bytes().first()? {
            b'Z' => return Some(UtcOffset::UTC),
            b'-' => -1,
            _ => 1,
        };
        let digits: String = zone[1..].chars().filter(char::is_ascii_digit).collect();
        if digits.len() > 4 {
            return None;
        }
        let field = |at: usize| {
            digits
                .get(at..at + 2)
                .map_or(0, |d| d.parse::<i32>().unwrap())
        };
        Some(UtcOffset {
            minutes: sign * (field(0) * 60 + field(2)),
        })
    }

    #[test]
    fn test_coerce_int() {
        assert!(matches!(
            coerce_param("42", TYPE_INT, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Int(42))
        ));
        assert!(matches!(
            coerce_param("-123", TYPE_INT, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Int(-123))
        ));
        assert!(coerce_param("abc", TYPE_INT, DEFAULT_MAX_PARAM_LENGTH).is_err());
    }

    #[test]
    fn test_coerce_float() {
        assert!(
            matches!(coerce_param("1.25", TYPE_FLOAT, DEFAULT_MAX_PARAM_LENGTH), Ok(CoercedValue::Float(f)) if (f - 1.25).abs() < 0.001)
        );
        assert!(
            matches!(coerce_param("-2.5", TYPE_FLOAT, DEFAULT_MAX_PARAM_LENGTH), Ok(CoercedValue::Float(f)) if (f + 2.5).abs() < 0.001)
        );
        assert!(coerce_param("not_a_float", TYPE_FLOAT, DEFAULT_MAX_PARAM_LENGTH).is_err());
    }

    #[test]
    fn python_year_range_is_enforced() {
        for value in [
            "0000-01-01",
            "-0001-01-01",
            "+10000-01-01",
            "10000-01-01",
            "0000-W01-1",
            "9999-W52-6",
        ] {
            let error = coerce_param(value, TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert_eq!(
                error.to_string(),
                format!("Invalid date '{value}': expected ISO 8601 format (e.g., 2024-01-15)")
            );
        }
        for value in [
            "0000-01-01T00:00:00",
            "0000-01-01T00:00:00Z",
            "+10000-01-01T00:00:00",
            "10000-01-01T00:00:00",
        ] {
            let error = coerce_param(value, TYPE_DATETIME, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert_eq!(
                error.to_string(),
                format!(
                    "Invalid datetime '{value}': expected ISO 8601 format (e.g., 2024-01-15T10:30:00Z)"
                )
            );
        }
        assert!(coerce_param("0001-01-01", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
        assert!(coerce_param("0001-W01-1", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
        assert!(coerce_param("9999-12-31", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
        assert!(coerce_param("9999-W52-5", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
    }

    #[test]
    fn leap_seconds_are_refused() {
        for (value, type_hint) in [
            ("23:59:60", TYPE_TIME),
            ("23:59:60.5", TYPE_TIME),
            ("2016-12-31T23:59:60Z", TYPE_DATETIME),
            ("2016-12-31T23:59:60+00:00", TYPE_DATETIME),
            ("2016-12-31T23:59:60", TYPE_DATETIME),
        ] {
            let error = coerce_param(value, type_hint, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert!(
                error.to_string().contains("expected ISO 8601 format"),
                "{value}: {error}"
            );
        }
        assert!(coerce_param("23:59:59.999999", TYPE_TIME, DEFAULT_MAX_PARAM_LENGTH).is_ok());
    }

    #[test]
    fn python_decimal_exponent_range_is_enforced() {
        for value in ["1E999999999999999999999", "1e-99999999999999999999"] {
            let error = coerce_param(value, TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert!(error.to_string().contains("exponent"), "{value}: {error}");
        }
        for value in ["1E+2147483648", "1E999999999999999", "-0.5e-7", "12.5"] {
            assert!(
                coerce_param(value, TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH).is_ok(),
                "{value}"
            );
        }
    }

    #[test]
    fn test_coerce_bool() {
        for (value, expected) in [
            ("TRUE", true),
            ("Yes", true),
            ("ON", true),
            ("OFF", false),
            ("No", false),
        ] {
            assert!(
                matches!(coerce_param(value, TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH), Ok(CoercedValue::Bool(b)) if b == expected),
                "{value}"
            );
        }
        for value in ["tru", "yess", "", "on "] {
            assert!(
                coerce_param(value, TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH).is_err(),
                "{value}"
            );
        }
        assert!(matches!(
            coerce_param("true", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("True", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("1", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("yes", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(true))
        ));
        assert!(matches!(
            coerce_param("on", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(true))
        ));

        assert!(matches!(
            coerce_param("false", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("False", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("0", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("no", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(false))
        ));
        assert!(matches!(
            coerce_param("off", TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Bool(false))
        ));
    }

    #[test]
    fn test_coerce_uuid() {
        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";
        assert!(matches!(
            coerce_param(uuid_str, TYPE_UUID, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Uuid(_))
        ));
        assert!(coerce_param("not-a-uuid", TYPE_UUID, DEFAULT_MAX_PARAM_LENGTH).is_err());
    }

    /// `uuid_to_py` sets the slots directly, with no `UUID.__init__` call.
    #[test]
    fn uuid_to_py_builds_the_same_object_as_the_uuid_class() {
        Python::initialize();
        Python::attach(|py| {
            let text = "550e8400-e29b-41d4-a716-446655440000";
            let built = uuid_to_py(py, Uuid::parse_str(text).unwrap()).unwrap();
            let built = built.bind(py);
            let expected = get_uuid_class(py).bind(py).call1((text,)).unwrap();

            assert_eq!(built.str().unwrap().to_string(), text);
            assert!(built.eq(&expected).unwrap());
            let is_safe = built.getattr("is_safe").unwrap();
            assert!(is_safe.is(expected.getattr("is_safe").unwrap()));
        });
    }

    #[test]
    fn test_coerce_datetime() {
        // ISO 8601 with Z suffix
        assert!(matches!(
            coerce_param(
                "2024-01-15T10:30:00Z",
                TYPE_DATETIME,
                DEFAULT_MAX_PARAM_LENGTH
            ),
            Ok(CoercedValue::DateTime(_, UtcOffset::UTC))
        ));

        // ISO 8601 with timezone offset
        assert!(matches!(
            coerce_param(
                "2024-01-15T10:30:00+00:00",
                TYPE_DATETIME,
                DEFAULT_MAX_PARAM_LENGTH
            ),
            Ok(CoercedValue::DateTime(_, UtcOffset::UTC))
        ));

        // Naive datetime
        assert!(matches!(
            coerce_param(
                "2024-01-15T10:30:00",
                TYPE_DATETIME,
                DEFAULT_MAX_PARAM_LENGTH
            ),
            Ok(CoercedValue::NaiveDateTime(_))
        ));

        assert!(coerce_param("not-a-datetime", TYPE_DATETIME, DEFAULT_MAX_PARAM_LENGTH).is_err());
    }

    /// Build the Python object of a coerced value and give `repr(value)` and
    /// `repr(value.utcoffset())` (None for a value with no offset).
    fn python_repr(value: &str, type_hint: u8) -> Option<(String, String)> {
        let coerced = coerce(value, type_hint).ok()?;
        Python::initialize();
        Some(Python::attach(|py| {
            let object = coerced_value_to_py(py, &coerced).unwrap();
            let object = object.bind(py);
            let offset = if object.hasattr("utcoffset").unwrap() {
                object
                    .call_method0("utcoffset")
                    .unwrap()
                    .repr()
                    .unwrap()
                    .to_string()
            } else {
                "None".to_string()
            };
            (object.repr().unwrap().to_string(), offset)
        }))
    }

    /// Check `repr()` of the Python value of each text, as Python 3.12, 3.13
    /// and 3.14 give it for `fromisoformat()`, `int()` or `float()`.
    fn assert_python_values(type_hint: u8, cases: &[(&str, &str)]) {
        for (value, expected) in cases {
            let repr = python_repr(value, type_hint)
                .unwrap_or_else(|| panic!("{value:?} was refused: {:?}", coerce(value, type_hint)))
                .0;
            assert_eq!(repr, *expected, "{value:?}");
        }
    }

    fn assert_refused(type_hint: u8, values: &[&str]) {
        for value in values {
            assert!(
                coerce(value, type_hint).is_err(),
                "{value:?} converted to {:?}",
                coerce(value, type_hint)
            );
        }
    }

    #[test]
    fn f1_an_aware_datetime_keeps_its_utc_offset() {
        for (value, fields, offset) in [
            (
                "2024-01-15T10:30:00+05:30",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(seconds=19800)",
            ),
            (
                "2024-01-15T10:30:00.5-08:00",
                "2024, 1, 15, 10, 30, 0, 500000",
                "datetime.timedelta(days=-1, seconds=57600)",
            ),
            (
                "2024-01-15T10:30:00+23:59",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(seconds=86340)",
            ),
            (
                "2024-01-15T10:30:00-23:59",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(days=-1, seconds=60)",
            ),
            (
                "9999-12-31T23:59:59-01:00",
                "9999, 12, 31, 23, 59, 59",
                "datetime.timedelta(days=-1, seconds=82800)",
            ),
            (
                "0001-01-01T00:00:00+01:00",
                "1, 1, 1, 0, 0",
                "datetime.timedelta(seconds=3600)",
            ),
            (
                "2024-01-15T10:30:00Z",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(0)",
            ),
            (
                "2024-01-15T10:30:00+00:00",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(0)",
            ),
            (
                "2024-01-15T10:30:00-00:00",
                "2024, 1, 15, 10, 30",
                "datetime.timedelta(0)",
            ),
        ] {
            let (repr, utcoffset) =
                python_repr(value, TYPE_DATETIME).unwrap_or_else(|| panic!("{value} was refused"));
            assert!(
                repr.starts_with(&format!("datetime.datetime({fields}, tzinfo=")),
                "{value}: {repr}"
            );
            assert_eq!(utcoffset, offset, "{value}");
        }
        assert_python_values(
            TYPE_DATETIME,
            &[
                (
                    "2024-01-15T10:30:00Z",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)",
                ),
                (
                    "2024-01-15T10:30:00-00:00",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)",
                ),
                (
                    "2024-01-15T10:30:00",
                    "datetime.datetime(2024, 1, 15, 10, 30)",
                ),
            ],
        );
    }

    #[test]
    fn f2_text_that_is_not_iso_8601_is_refused() {
        assert_refused(
            TYPE_DATE,
            &[
                "24-01-15",
                "2024-1-5",
                "+2024-01-15",
                " 2024-01-15",
                "2024-01-15 ",
                "2024-1-15",
                "2024-01-5",
            ],
        );
        assert_refused(
            TYPE_DATETIME,
            &[
                "24-01-15",
                "2024-1-5",
                "+2024-01-15",
                "2024-01-1510:30:00",
                "2024-10-31T9:34:5Z",
                "2024-01-15T1:2:3",
                "\t2024-01-15T10:30:00",
                "2024-01-15T 10:30:00",
                "2024-01-15T10:30:0",
                " 2024-01-15",
            ],
        );
        assert_refused(
            TYPE_TIME,
            &["\t10:30:00", "1:2:3", "10:3", "10:30:0", "10:30:00 "],
        );
    }

    /// Python reads these texts, but they are not ISO 8601. Bolt refuses them.
    #[test]
    fn python_texts_that_are_not_iso_8601_are_refused() {
        assert_refused(
            TYPE_DATETIME,
            &[
                // fromisoformat() takes any one character between the date and the time.
                "2024-01-15x10:30:00",
                "2024-01-15_10:30:00",
                "2024-01-15-10:30:00",
                "2024-01-15  10:30:00",
                "2024-01-15\u{e9}10:30:00",
                "2024-01-15010:30:00",
                "2024-W03-10:30",
                // Python skips the text between the time and the offset.
                "2024-01-15T10:30:00 +05:30",
                "2024-01-15T10:30:00.Z",
                // Python reads a minute of 60 in an offset as one hour.
                "2024-01-15T10:30:00+05:60",
                "2024-01-15T10:30:00\0",
                "2024-01-15\u{0}10:30:00",
            ],
        );
        assert_refused(
            TYPE_TIME,
            &[
                "10:30:00 +05:00",
                "10:30:00x+05:00",
                "10:30:+05:00",
                "10:+05:00",
                "10:30:00.1234567abc+05:00",
                "10:30:00.123456 +05:00",
                "10:30:00+00:99",
                "10:30:00+05:30:60",
                "10:30:00\0",
                "10:30:00Z\0abc",
                "10:30:00+05:00:00.123456\0junk",
            ],
        );
        // date.fromisoformat() reads a text of ten characters with text after the date.
        assert_refused(TYPE_DATE, &["20240115xx", "2024W031xx", "2024-01-15\0"]);
    }

    /// Python 3.12, 3.13 and 3.14 give different results for these texts.
    #[test]
    fn texts_that_python_versions_read_differently_are_refused() {
        // Only Python 3.14 reads hour 24 as midnight of the next day.
        assert_refused(TYPE_TIME, &["24:00", "24:00:00", "24:00:00Z"]);
        assert_refused(TYPE_DATETIME, &["2024-01-15T24:00", "2024-12-31T24:00:00"]);
        // Python 3.14 refuses a fraction of an hour or a minute; 3.12 reads it.
        assert_refused(
            TYPE_TIME,
            &["10.5", "10:30.5", "1030.5", "10:30:00+05:30.5"],
        );
        assert_refused(TYPE_DATETIME, &["2024-01-15T10:30.5", "2024-01-15T10.5"]);
        // Python 3.13 keeps the fraction of an offset of zero seconds; 3.12 and 3.14 give UTC.
        assert_refused(
            TYPE_TIME,
            &["10:30:00+00:00:00.5", "10:30:00-00:00:00.000001"],
        );
        // Python 3.13 refuses a point with no digit before the offset; 3.12 and 3.14 skip it.
        assert_refused(TYPE_TIME, &["10.+05:00", "10:30:00.+05:00"]);
    }

    /// The expected values are Python expressions, not digit texts: `repr()`
    /// of an `int` of more digits than `sys.get_int_max_str_digits()` fails,
    /// and the limit of the test process can be lower than 4300.
    #[test]
    fn f3_an_int_beyond_i64_converts_as_python_does() {
        let longest = "9".repeat(MAX_INT_DIGITS);
        let negative_longest = format!("-{longest}");
        let zeros = format!("{}1", "0".repeat(MAX_INT_DIGITS - 1));
        for (value, expected) in [
            ("9223372036854775808", "2**63"),
            ("-9223372036854775809", "-(2**63) - 1"),
            ("+18446744073709551616", "2**64"),
            ("-340282366920938463463374607431768211457", "-(2**128) - 1"),
            (longest.as_str(), "10**4300 - 1"),
            (negative_longest.as_str(), "1 - 10**4300"),
            (zeros.as_str(), "1"),
        ] {
            let coerced = coerce(value, TYPE_INT).unwrap_or_else(|e| panic!("{value}: {e}"));
            Python::initialize();
            Python::attach(|py| {
                let object = coerced_value_to_py(py, &coerced).unwrap();
                let expected = py
                    .eval(&std::ffi::CString::new(expected).unwrap(), None, None)
                    .unwrap();
                assert!(object.bind(py).eq(expected).unwrap(), "{value}");
            });
        }
        // int() refuses more digits than sys.get_int_max_str_digits(), 4300 by default.
        let too_long = "9".repeat(MAX_INT_DIGITS + 1);
        let zeros_too_long = "0".repeat(MAX_INT_DIGITS + 1);
        let error = coerce(&too_long, TYPE_INT).unwrap_err().to_string();
        assert_eq!(
            error,
            format!("Invalid integer '{too_long}': more than 4300 digits")
        );
        assert_refused(TYPE_INT, &[&zeros_too_long, &format!("-{too_long}")]);
    }

    /// `int()` reads a sign and leading zeros. It refuses a fraction, an
    /// exponent and a Unix timestamp form. `_`, whitespace and digits that are
    /// not ASCII stay refused, as on master.
    #[test]
    fn an_int_is_read_as_python_reads_it() {
        assert_python_values(
            TYPE_INT,
            &[
                ("+5", "5"),
                ("007", "7"),
                ("-007", "-7"),
                ("-0", "0"),
                ("+0", "0"),
                ("0009223372036854775807", "9223372036854775807"),
            ],
        );
        assert_refused(
            TYPE_INT,
            &[
                "5.0", "1e3", "5.", "1.5", "nan", "inf", "", "+", "-", "+-5", "--5", "0x10", "0b1",
                "1_000", " 5", "5 ", "\t5", "5\n", "\u{0663}", "\u{ff15}",
            ],
        );
        let error = coerce("abc", TYPE_INT).unwrap_err().to_string();
        assert_eq!(
            error,
            "Invalid integer 'abc': expected a whole number such as 42 or -7"
        );
    }

    #[test]
    fn f4_iso_8601_forms_that_python_accepts() {
        assert_python_values(
            TYPE_DATETIME,
            &[
                // An HTML <input type="datetime-local"> sends this form.
                ("2024-01-15T10:30", "datetime.datetime(2024, 1, 15, 10, 30)"),
                ("2024-01-15T10", "datetime.datetime(2024, 1, 15, 10, 0)"),
                ("2024-01-15 10:30", "datetime.datetime(2024, 1, 15, 10, 30)"),
                // chrono took any whitespace here, and Python takes any one character.
                (
                    "2024-01-15\t10:30",
                    "datetime.datetime(2024, 1, 15, 10, 30)",
                ),
                (
                    "2024-01-15\u{3000}10:30:00Z",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)",
                ),
                (
                    "2024-01-15t10:30:00",
                    "datetime.datetime(2024, 1, 15, 10, 30)",
                ),
                (
                    "2024-01-15T10:30:00+0530",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))",
                ),
                (
                    "2024-01-15T10:30:00.123-0530",
                    "datetime.datetime(2024, 1, 15, 10, 30, 0, 123000, tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=66600)))",
                ),
                (
                    "2024-01-15T10:30:00+05",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=18000)))",
                ),
                (
                    "2024-01-15T10:30+05:30",
                    "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))",
                ),
                (
                    "2024-01-15T10Z",
                    "datetime.datetime(2024, 1, 15, 10, 0, tzinfo=datetime.timezone.utc)",
                ),
                // Basic format, and week dates.
                ("20240115T103000", "datetime.datetime(2024, 1, 15, 10, 30)"),
                ("20240115T1030", "datetime.datetime(2024, 1, 15, 10, 30)"),
                ("20240115", "datetime.datetime(2024, 1, 15, 0, 0)"),
                (
                    "20240115T10:30:00",
                    "datetime.datetime(2024, 1, 15, 10, 30)",
                ),
                (
                    "2024-W03-1T10:30:00",
                    "datetime.datetime(2024, 1, 15, 10, 30)",
                ),
                ("2024W031T103000", "datetime.datetime(2024, 1, 15, 10, 30)"),
                ("2024-W03", "datetime.datetime(2024, 1, 15, 0, 0)"),
                ("2020-W53-7", "datetime.datetime(2021, 1, 3, 0, 0)"),
                // A comma before the fraction. Python drops the digits after the sixth.
                (
                    "2024-01-15T10:30:00,5",
                    "datetime.datetime(2024, 1, 15, 10, 30, 0, 500000)",
                ),
                (
                    "2024-01-15T10:30:00.1234567",
                    "datetime.datetime(2024, 1, 15, 10, 30, 0, 123456)",
                ),
                (
                    "2024-01-15T10:30:00.9999995",
                    "datetime.datetime(2024, 1, 15, 10, 30, 0, 999999)",
                ),
                // A date alone is midnight.
                ("2024-01-15", "datetime.datetime(2024, 1, 15, 0, 0)"),
            ],
        );
        assert_python_values(
            TYPE_DATE,
            &[
                ("20240115", "datetime.date(2024, 1, 15)"),
                ("2024-W03-1", "datetime.date(2024, 1, 15)"),
                ("2024W031", "datetime.date(2024, 1, 15)"),
                ("2024-W03", "datetime.date(2024, 1, 15)"),
                ("2024W03", "datetime.date(2024, 1, 15)"),
                ("0001-W01-1", "datetime.date(1, 1, 1)"),
            ],
        );
        // Python refuses these forms too.
        assert_refused(
            TYPE_DATETIME,
            &[
                "2024-01-15T10:30:00z",
                "2024-015",
                "2024015",
                "2024-01-15T",
                "2024-01-15T10:3",
                "2024-01-15T10:30:00+24:00",
                "2024-01-15T10:30:00+5:30",
                "2024-01-15T10:30:00UTC",
                "1700000000",
                "-1.5",
                "２０２４-01-15T10:30:00",
            ],
        );
        assert_refused(
            TYPE_DATE,
            &[
                "2024-015",
                "0000-01-01",
                "2023-02-29",
                "2024-W53-1",
                "2024-W00-1",
                "2024-W01-0",
                "2024-W01-8",
                "2024-W031",
                "2024W03-1",
                "2024-0115",
                "202401-15",
                "2024-01-15Z",
            ],
        );
    }

    #[test]
    fn f5_a_time_with_an_offset_converts() {
        assert_python_values(
            TYPE_TIME,
            &[
                (
                    "10:30:00+05:00",
                    "datetime.time(10, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=18000)))",
                ),
                (
                    "10:30:00.123+0530",
                    "datetime.time(10, 30, 0, 123000, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))",
                ),
                (
                    "10:30:00Z",
                    "datetime.time(10, 30, tzinfo=datetime.timezone.utc)",
                ),
                (
                    "10:30:00-00:00",
                    "datetime.time(10, 30, tzinfo=datetime.timezone.utc)",
                ),
                (
                    "10+05:30",
                    "datetime.time(10, 0, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))",
                ),
                ("10:30:00", "datetime.time(10, 30)"),
                ("10:30", "datetime.time(10, 30)"),
                ("10", "datetime.time(10, 0)"),
                ("1030", "datetime.time(10, 30)"),
                ("103000.5", "datetime.time(10, 30, 0, 500000)"),
                ("T10:30:00", "datetime.time(10, 30)"),
                ("10:30:00,5", "datetime.time(10, 30, 0, 500000)"),
                ("23:59:59.9999995", "datetime.time(23, 59, 59, 999999)"),
            ],
        );
        assert_refused(
            TYPE_TIME,
            &[
                "10:30:00z",
                "t10:30:00",
                "10:30:00+24:00",
                "10:3000",
                "1030:00",
                "10:30:00.",
                "10:30:00+",
                "10:30:00Z+05:00",
                "10:30:00+05:3",
                "10:30:00+0530:15",
                "１０:30:00",
            ],
        );
    }

    /// Python reads seconds in an offset, but RFC 3339 has none, and the JSON
    /// encoder writes the offset in whole minutes. Master refused these texts too.
    #[test]
    fn an_offset_with_seconds_is_refused() {
        let zones = [
            "+05:30:15",
            "-05:30:15.5",
            "+05:30:00",
            "+00:00:00",
            "-00:00:00",
            "+053015",
            "+053000",
            "+05:30:15,5",
            "-23:59:59.999999",
        ];
        for zone in zones {
            assert_refused(TYPE_DATETIME, &[&format!("2024-01-15T10:30:00{zone}")]);
            assert_refused(TYPE_DATETIME, &[&format!("20240115T1030{zone}")]);
            assert_refused(
                TYPE_TIME,
                &[&format!("10:30:00{zone}"), &format!("10{zone}")],
            );
        }
        assert_python_values(
            TYPE_TIME,
            &[
                (
                    "10:30:00-23:59",
                    "datetime.time(10, 30, tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=60)))",
                ),
                (
                    "10:30:00+0000",
                    "datetime.time(10, 30, tzinfo=datetime.timezone.utc)",
                ),
            ],
        );
    }

    /// Rust reads the ASCII texts that `float()` reads, with the same value, as on master.
    #[test]
    fn a_float_is_read_as_python_reads_it() {
        for (value, expected) in [
            ("1.5", 1.5),
            ("+1.5", 1.5),
            (".5", 0.5),
            ("5.", 5.0),
            ("1e5", 100000.0),
            ("1E+5", 100000.0),
            ("1e-999", 0.0),
            ("1e999", f64::INFINITY),
            ("-1e999", f64::NEG_INFINITY),
            ("+inf", f64::INFINITY),
            ("Infinity", f64::INFINITY),
            ("-INF", f64::NEG_INFINITY),
            ("18446744073709551616", 18446744073709551616.0),
        ] {
            assert!(
                matches!(coerce(value, TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v == expected),
                "{value}"
            );
        }
        assert!(
            matches!(coerce("-0", TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v.to_bits() == (-0.0f64).to_bits())
        );
        // float() gives inf for 1e90000 written with 10,000 fraction digits. msgspec gave 1.0.
        let long = format!("0.{}1e100000", "0".repeat(9999));
        assert!(
            matches!(coerce_param(&long, TYPE_FLOAT, 16384), Ok(CoercedValue::Float(v)) if v == f64::INFINITY)
        );
        for value in ["nan", "-nAn", "+NaN"] {
            assert!(matches!(coerce(value, TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v.is_nan()));
        }
        assert_refused(
            TYPE_FLOAT,
            &[
                "",
                ".",
                "e5",
                "1e",
                "1e+",
                "0x1p3",
                "1.5f",
                "infinit",
                "infinityy",
                "1_000.5",
                " 1.5",
                "1.5 ",
                "1.5\n",
                "\u{0663}.5",
            ],
        );
        let error = coerce("abc", TYPE_FLOAT).unwrap_err().to_string();
        assert_eq!(
            error,
            "Invalid float 'abc': expected a number such as 1.5, -2e3, inf or nan"
        );
    }

    /// A sequence parameter converts each text item with the parser of one `T`.
    #[test]
    fn parse_text_items_converts_each_text_and_names_the_first_refused_item() {
        Python::initialize();
        Python::attach(|py| {
            let items = PyList::new(py, ["7", "+5", "9223372036854775808"]).unwrap();
            let converted = parse_text_items(&items, TYPE_INT, 0, usize::MAX).unwrap();
            assert_eq!(
                converted.repr().unwrap().to_string(),
                "[7, 5, 9223372036854775808]"
            );
            assert!(!converted.is(&items));

            let refused = PyList::new(py, ["1", "2", "5.0"]).unwrap();
            let error = parse_text_items(&refused, TYPE_INT, 0, usize::MAX).unwrap_err();
            assert_eq!(
                error
                    .value(py)
                    .getattr("args")
                    .unwrap()
                    .repr()
                    .unwrap()
                    .to_string(),
                "(2, \"Invalid integer '5.0': expected a whole number such as 42 or -7\")"
            );
        });
    }

    /// A length out of bounds gives the list itself, and no item is read:
    /// msgspec gives the error. An item that is not text gives the list
    /// itself when no earlier text was refused.
    #[test]
    fn parse_text_items_gives_the_list_itself_out_of_bounds_or_for_an_item_that_is_not_text() {
        Python::initialize();
        Python::attach(|py| {
            let items = PyList::new(py, ["x", "2", "3"]).unwrap();
            for (min_length, max_length) in [(0, 2), (4, 9)] {
                let given = parse_text_items(&items, TYPE_INT, min_length, max_length).unwrap();
                assert!(given.is(&items), "{min_length}..={max_length}");
            }
            let bounded = PyList::new(py, ["1", "2", "3"]).unwrap();
            let converted = parse_text_items(&bounded, TYPE_INT, 3, 3).unwrap();
            assert_eq!(converted.repr().unwrap().to_string(), "[1, 2, 3]");

            let mixed = PyList::new(py, ["2024-01-15T10:30"]).unwrap();
            mixed.append(1i64).unwrap();
            let given = parse_text_items(&mixed, TYPE_DATETIME, 0, usize::MAX).unwrap();
            assert!(given.is(&mixed));

            // A refused text before the item that is not text raises its error.
            let refused_first = PyList::new(py, ["5.0"]).unwrap();
            refused_first.append(2i64).unwrap();
            let error = parse_text_items(&refused_first, TYPE_INT, 0, usize::MAX).unwrap_err();
            let (index, _): (usize, String) =
                error.value(py).getattr("args").unwrap().extract().unwrap();
            assert_eq!(index, 0);
        });
    }

    #[test]
    fn test_coerce_decimal() {
        assert!(matches!(
            coerce_param("123.45", TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Decimal(_))
        ));
        assert!(matches!(
            coerce_param("-99.99", TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH),
            Ok(CoercedValue::Decimal(_))
        ));
        assert!(coerce_param("not_decimal", TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH).is_err());
    }

    #[test]
    fn test_decimal_literal_grammar() {
        for valid in [
            "0",
            "5.",
            ".5",
            "+1.5",
            "-99.99",
            "1e5",
            "1E+5",
            "1.5e-3",
            "+.5e2",
            // Arbitrary precision and huge exponents are valid (Python Decimal
            // semantics); the value is validated textually, never expanded.
            "999999999999999999999999999999999999999999",
            "1e999999999",
        ] {
            assert!(
                matches!(
                    coerce_param(valid, TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH),
                    Ok(CoercedValue::Decimal(_))
                ),
                "expected '{valid}' to be a valid decimal"
            );
        }
        for invalid in [
            "", ".", "+", "-", "e5", "1e", "1e+", "--1", "1..2", "1.2.3", "1_000", "NaN", "inf",
            " 1", "1 ", "0x10",
        ] {
            assert!(
                coerce_param(invalid, TYPE_DECIMAL, DEFAULT_MAX_PARAM_LENGTH).is_err(),
                "expected '{invalid}' to be rejected"
            );
        }
    }

    #[test]
    fn test_param_length_limit() {
        let max_length = DEFAULT_MAX_PARAM_LENGTH;
        // Valid length should work
        let valid = "a".repeat(max_length);
        assert!(coerce_param(&valid, TYPE_STRING, DEFAULT_MAX_PARAM_LENGTH).is_ok());

        // Exceeding limit should fail
        let too_long = "a".repeat(max_length + 1);
        let result = coerce_param(&too_long, TYPE_STRING, DEFAULT_MAX_PARAM_LENGTH);
        assert!(matches!(result, Err(CoerceError::TooLong { .. })));
    }

    #[test]
    fn test_parse_max_param_length_defaults_when_unset_or_empty() {
        assert_eq!(parse_max_param_length(None), Ok(DEFAULT_MAX_PARAM_LENGTH));
        assert_eq!(
            parse_max_param_length(Some("")),
            Ok(DEFAULT_MAX_PARAM_LENGTH)
        );
    }

    #[test]
    fn test_parse_max_param_length_rejects_invalid_values() {
        // A value above the cap is an error too, so it cannot nullify the DoS
        // guardrail.
        for raw in ["0", "-1", "not-a-number", "999999999999"] {
            assert_eq!(
                parse_max_param_length(Some(raw)),
                Err(format!(
                    "DJANGO_BOLT_MAX_PARAM_LENGTH must be an int from 1 to 1048576, got '{raw}'."
                )),
                "{raw}"
            );
        }
    }

    #[test]
    fn test_parse_max_param_length_accepts_valid_values() {
        assert_eq!(parse_max_param_length(Some("16384")), Ok(16384));
        // The cap itself is accepted as-is.
        assert_eq!(
            parse_max_param_length(Some(&MAX_ALLOWED_PARAM_LENGTH.to_string())),
            Ok(MAX_ALLOWED_PARAM_LENGTH)
        );
    }
}
