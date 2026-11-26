//! Type coercion for path and query parameters
//!
//! This module provides Rust-native type coercion for HTTP parameters,
//! eliminating the need for Python's convert_primitive() function.
//! Performance improvement: ~100-500µs per parameter.

use chrono::{
    DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Timelike,
};
use pyo3::exceptions::PyValueError;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyAnyMethods, PyDateTime, PyDelta, PyDict, PyDictMethods, PyTime, PyTzInfo};
use pyo3::{Bound, IntoPyObject, Py, PyAny, PyResult, Python};
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
    /// The text of an integer beyond `i64`: an optional `-` and at most
    /// [`MAX_INT_TEXT_LEN`] characters of ASCII digits.
    BigInt(String),
    Float(f64),
    Bool(bool),
    String(String),
    Uuid(Uuid),
    /// A datetime with the UTC offset of its text.
    DateTime(DateTime<FixedOffset>),
    NaiveDateTime(NaiveDateTime),
    Date(NaiveDate),
    Time(NaiveTime),
    /// A time with the UTC offset of its text.
    TimeTz(NaiveTime, FixedOffset),
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

        TYPE_DATETIME => parse_datetime(value),

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

        TYPE_DATE => parse_ymd(value.as_bytes())
            .map(CoercedValue::Date)
            .ok_or_else(|| format!("Invalid date '{}': expected format YYYY-MM-DD", value)),

        TYPE_TIME => match parse_clock(value.as_bytes()) {
            Some((time, None)) => Ok(CoercedValue::Time(time)),
            Some((time, Some(offset))) => Ok(CoercedValue::TimeTz(time, offset)),
            None => Err(format!(
                "Invalid time '{}': expected format HH:MM:SS[.ffffff][Z|±HH:MM]",
                value
            )),
        },

        _ => Ok(CoercedValue::String(value.to_string())),
    }
}

// The parsers below accept a text if and only if `msgspec.convert(text, T,
// strict=False)` accepts it, and give the value that msgspec gives. A
// `list[T]` parameter converts each value with that call, so a scalar and a
// list parameter of one type agree. msgspec gives the same result on each
// supported Python version; `fromisoformat` does not. The parsers read fixed
// field widths and skip no whitespace. A NUL character is refused: msgspec
// stops at it and ignores the rest of the text.

/// Longest integer text that becomes a Python `int`, with its sign. msgspec
/// refuses a longer one. This is also the default of
/// `sys.get_int_max_str_digits()`.
pub const MAX_INT_TEXT_LEN: usize = 4300;

/// Largest magnitude of a float text that msgspec gives as an `int`: 2**53.
const MAX_INT_FROM_FLOAT: f64 = 9_007_199_254_740_992.0;

/// The Unix timestamps of 0001-01-01T00:00:00Z and 10000-01-01T00:00:00Z, the
/// bounds that msgspec checks before it rounds the microseconds.
const EPOCH_SECS_MIN: i64 = -62_135_596_800;
const EPOCH_SECS_MAX: i64 = 253_402_300_800;

/// The form of a number text in msgspec's grammar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NumberForm {
    /// `-?(0|[1-9][0-9]*)`
    Integer,
    /// An integer part, then `\.[0-9]+` or `[eE][+-]?[0-9]+` or both.
    Real,
    /// `-?` and `nan`, `inf` or `infinity`, in any case.
    NonFinite,
}

/// Classify `text` by msgspec's number grammar. None: the text is not a number.
fn number_form(text: &[u8]) -> Option<NumberForm> {
    let digits = text.strip_prefix(b"-").unwrap_or(text);
    let integer_len = match digits {
        [b'0', ..] => 1,
        [b'1'..=b'9', ..] => digits.iter().take_while(|c| c.is_ascii_digit()).count(),
        _ => {
            return [&b"nan"[..], b"inf", b"infinity"]
                .iter()
                .any(|word| digits.eq_ignore_ascii_case(word))
                .then_some(NumberForm::NonFinite);
        }
    };
    let mut rest = &digits[integer_len..];
    if rest.is_empty() {
        return Some(NumberForm::Integer);
    }
    if let Some(fraction) = rest.strip_prefix(b".") {
        let count = fraction.iter().take_while(|c| c.is_ascii_digit()).count();
        if count == 0 {
            return None;
        }
        rest = &fraction[count..];
    }
    if let Some(exponent) = rest.strip_prefix(b"e").or_else(|| rest.strip_prefix(b"E")) {
        let exponent = exponent
            .strip_prefix(b"+")
            .or_else(|| exponent.strip_prefix(b"-"))
            .unwrap_or(exponent);
        if exponent.is_empty() || !exponent.iter().all(u8::is_ascii_digit) {
            return None;
        }
        rest = &[];
    }
    rest.is_empty().then_some(NumberForm::Real)
}

/// Parse an `int` as msgspec does. An integer beyond `i64` keeps its text
/// for Python. A whole number in float form, such as `5.0` or `1e3`, is an
/// int up to 2**53. A leading `+` or zero, `_` and whitespace are refused.
fn parse_int(value: &str) -> Result<CoercedValue, String> {
    // One pass for the common case: Rust's parser reads the same digits once
    // the text has no leading `+` and no leading zero.
    let unsigned = value.strip_prefix('-').unwrap_or(value).as_bytes();
    if matches!(unsigned, [b'1'..=b'9', ..] | [b'0'])
        && let Ok(int) = value.parse::<i64>()
    {
        return Ok(CoercedValue::Int(int));
    }
    match number_form(value.as_bytes()) {
        // The pass above took each integer in the i64 range.
        Some(NumberForm::Integer) if value.len() <= MAX_INT_TEXT_LEN => {
            Ok(CoercedValue::BigInt(value.to_owned()))
        }
        Some(NumberForm::Integer) => Err(format!(
            "Invalid integer '{}': more than {} characters",
            value, MAX_INT_TEXT_LEN
        )),
        Some(NumberForm::Real) => match value.parse::<f64>() {
            Ok(real) if real.fract() == 0.0 && real.abs() <= MAX_INT_FROM_FLOAT => {
                Ok(CoercedValue::Int(real as i64))
            }
            _ => Err(format!(
                "Invalid integer '{}': not a whole number from -2**53 to 2**53",
                value
            )),
        },
        _ => Err(format!(
            "Invalid integer '{}': expected a whole number such as 42 or -7",
            value
        )),
    }
}

/// Parse a `float` as msgspec does. A finite text that overflows is refused.
fn parse_float(value: &str) -> Result<CoercedValue, String> {
    let form = number_form(value.as_bytes()).ok_or_else(|| {
        format!(
            "Invalid float '{}': expected a number such as 1.5, -2e3, inf or nan",
            value
        )
    })?;
    match value.parse::<f64>() {
        // msgspec reads `-0` as the int 0, so the float is 0.0, not -0.0.
        Ok(float) if form == NumberForm::Integer && float == 0.0 => Ok(CoercedValue::Float(0.0)),
        Ok(float) if float.is_finite() || form == NumberForm::NonFinite => {
            Ok(CoercedValue::Float(float))
        }
        _ => Err(format!("Invalid float '{}': out of range", value)),
    }
}

/// Parse a `datetime` as msgspec does: a Unix timestamp in UTC, or
/// `YYYY-MM-DD(T|t| )HH:MM:SS[.f+][Z|z|±HH[:]MM]`.
fn parse_datetime(value: &str) -> Result<CoercedValue, String> {
    let bytes = value.as_bytes();
    let parsed = match number_form(bytes) {
        Some(NumberForm::Integer) => value
            .parse::<i64>()
            .ok()
            .and_then(|secs| datetime_from_epoch(secs, 0))
            .map(CoercedValue::DateTime),
        Some(_) => value
            .parse::<f64>()
            .ok()
            .and_then(datetime_from_timestamp)
            .map(CoercedValue::DateTime),
        None => parse_datetime_text(bytes),
    };
    parsed.ok_or_else(|| {
        format!(
            "Invalid datetime '{}': expected format YYYY-MM-DDTHH:MM:SS[.ffffff][Z|±HH:MM] \
             (e.g., 2024-01-15T10:30:00Z) or a Unix timestamp",
            value
        )
    })
}

/// Parse `YYYY-MM-DD(T|t| )HH:MM:SS[.f+][Z|z|±HH[:]MM]`. A round-up past 9999-12-31 is refused.
fn parse_datetime_text(text: &[u8]) -> Option<CoercedValue> {
    if text.len() < 19 || !matches!(text[10], b'T' | b't' | b' ') {
        return None;
    }
    let date = parse_ymd(&text[..10])?;
    let (time, round_up, offset) = parse_clock_fields(&text[11..])?;
    let mut local = date.and_time(time);
    if round_up {
        local = local.checked_add_signed(TimeDelta::microseconds(1))?;
        if local.year() > 9999 {
            return None;
        }
    }
    Some(match offset {
        None => CoercedValue::NaiveDateTime(local),
        Some(offset) => CoercedValue::DateTime(local.and_local_timezone(offset).single()?),
    })
}

/// Give the UTC datetime of a float Unix timestamp, as msgspec computes it.
fn datetime_from_timestamp(timestamp: f64) -> Option<DateTime<FixedOffset>> {
    // Each timestamp in range has a magnitude below 1e12. The bound keeps the
    // arithmetic below from overflow.
    if !timestamp.is_finite() || timestamp.abs() >= 1e12 {
        return None;
    }
    // The casts truncate toward zero, as the C conversions in msgspec do.
    let mut secs = timestamp.trunc() as i64;
    let mut nanos = (1_000_000_000.0 * (timestamp - secs as f64)) as i32;
    if nanos != 0 && timestamp < 0.0 {
        secs -= 1;
        nanos += 1_000_000_000;
    }
    datetime_from_epoch(secs, nanos as u32)
}

/// Give the UTC datetime of a Unix timestamp, rounded to the closest microsecond.
fn datetime_from_epoch(secs: i64, nanos: u32) -> Option<DateTime<FixedOffset>> {
    if !(EPOCH_SECS_MIN..=EPOCH_SECS_MAX).contains(&secs) {
        return None;
    }
    let micros = (nanos + 500) / 1000;
    let (secs, micros) = if micros == 1_000_000 {
        (secs + 1, 0)
    } else {
        (secs, micros)
    };
    let utc = DateTime::from_timestamp(secs, micros * 1000)?;
    (utc.year() <= 9999).then(|| utc.fixed_offset())
}

fn digits(field: &[u8]) -> Option<u32> {
    field.iter().try_fold(0u32, |total, &c| {
        c.is_ascii_digit().then(|| total * 10 + u32::from(c - b'0'))
    })
}

/// Parse exactly `YYYY-MM-DD`, year 1 to 9999.
fn parse_ymd(text: &[u8]) -> Option<NaiveDate> {
    let [y0, y1, y2, y3, b'-', m0, m1, b'-', d0, d1] = *text else {
        return None;
    };
    let year = digits(&[y0, y1, y2, y3])?;
    if year == 0 {
        return None;
    }
    NaiveDate::from_ymd_opt(year as i32, digits(&[m0, m1])?, digits(&[d0, d1])?)
}

/// Parse a `time`: `HH:MM:SS[.f+][Z|z|±HH[:]MM]`, rounded to the closest
/// microsecond. The round-up of 23:59:59.9999995 wraps to 00:00:00, as in msgspec.
fn parse_clock(text: &[u8]) -> Option<(NaiveTime, Option<FixedOffset>)> {
    let (time, round_up, offset) = parse_clock_fields(text)?;
    let time = if round_up {
        time.overflowing_add_signed(TimeDelta::microseconds(1)).0
    } else {
        time
    };
    Some((time, offset))
}

/// Parse `HH:MM:SS[.f+][Z|z|±HH[:]MM]`. Give the time truncated to the
/// microsecond, whether the seventh fraction digit is 5 or more, and the offset.
fn parse_clock_fields(text: &[u8]) -> Option<(NaiveTime, bool, Option<FixedOffset>)> {
    let [h0, h1, b':', m0, m1, b':', s0, s1, ref rest @ ..] = *text else {
        return None;
    };
    let (hour, minute, second) = (digits(&[h0, h1])?, digits(&[m0, m1])?, digits(&[s0, s1])?);
    let mut rest = rest;
    let mut micro = 0;
    let mut round_up = false;
    if let Some(fraction) = rest.strip_prefix(b".") {
        let count = fraction.iter().take_while(|c| c.is_ascii_digit()).count();
        if count == 0 {
            return None;
        }
        let kept = count.min(6);
        micro = digits(&fraction[..kept])? * 10u32.pow(6 - kept as u32);
        round_up = count > 6 && fraction[6] >= b'5';
        rest = &fraction[count..];
    }
    let offset = match rest {
        [] => None,
        [b'Z' | b'z'] => Some(FixedOffset::east_opt(0)?),
        [sign @ (b'+' | b'-'), h0, h1, b':', m0, m1] | [sign @ (b'+' | b'-'), h0, h1, m0, m1] => {
            let (hours, minutes) = (digits(&[*h0, *h1])?, digits(&[*m0, *m1])?);
            if hours > 23 || minutes > 59 {
                return None;
            }
            let seconds = (hours * 3600 + minutes * 60) as i32;
            Some(FixedOffset::east_opt(if *sign == b'-' {
                -seconds
            } else {
                seconds
            })?)
        }
        _ => return None,
    };
    // chrono refuses second 60 here. It makes a leap second only from a nanosecond value of 1e9 or more.
    let time = NaiveTime::from_hms_micro_opt(hour, minute, second, micro)?;
    Some((time, round_up, offset))
}

/// Build a Python `int` from the text of a [`CoercedValue::BigInt`].
///
/// `int(text)` refuses more digits than `sys.get_int_max_str_digits()`, which
/// an application can lower. Integer arithmetic on chunks of 18 digits has no
/// such limit, so a value that Rust accepted always converts.
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

/// Give the Python `tzinfo` of a fixed offset. UTC is the cached
/// `datetime.timezone.utc`, which `timezone(timedelta(0))` also gives.
fn tzinfo_to_py<'py>(py: Python<'py>, offset: FixedOffset) -> PyResult<Bound<'py, PyTzInfo>> {
    match offset.local_minus_utc() {
        0 => Ok(PyTzInfo::utc(py)?.to_owned()),
        seconds => PyTzInfo::fixed_offset(py, PyDelta::new(py, 0, seconds, 0, true)?),
    }
}

/// Microseconds of a time that [`parse_clock_fields`] or chrono built. No leap second occurs.
fn micros(time: NaiveTime) -> u32 {
    time.nanosecond() / 1000
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
        CoercedValue::DateTime(v) => {
            let local = v.naive_local();
            PyDateTime::new(
                py,
                local.year(),
                local.month() as u8,
                local.day() as u8,
                local.hour() as u8,
                local.minute() as u8,
                local.second() as u8,
                micros(local.time()),
                Some(&tzinfo_to_py(py, *v.offset())?),
            )?
            .into_any()
            .unbind()
        }
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
            // Debug keeps the sign of -0.0: Display gives "-0", the int 0 for msgspec.
            let Ok(CoercedValue::Float(parsed)) = coerce(&format!("{value:?}"), TYPE_FLOAT) else {
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

        #[test]
        fn an_integer_timestamp_is_a_utc_datetime(secs in EPOCH_SECS_MIN..EPOCH_SECS_MAX) {
            let Ok(CoercedValue::DateTime(value)) = coerce(&secs.to_string(), TYPE_DATETIME) else {
                return Err(TestCaseError::fail(format!("{secs} was refused")));
            };
            prop_assert_eq!(value.timestamp(), secs);
            prop_assert_eq!(value.offset().local_minus_utc(), 0);
        }

        /// A datetime of the grammar keeps its fields and its offset. A fraction
        /// rounds to the closest microsecond by its seventh digit.
        #[test]
        fn a_datetime_text_of_the_grammar_converts(
            days in 1i32..=3_652_059,
            seconds in 0u32..86_400,
            fraction in "(\\.[0-9]{1,9})?",
            separator in "[Tt ]",
            zone in "|[Zz]|[+-](0[0-9]|1[0-9]|2[0-3]):?[0-5][0-9]",
        ) {
            let date = NaiveDate::from_num_days_from_ce_opt(days).unwrap();
            let time = NaiveTime::from_num_seconds_from_midnight_opt(seconds, 0).unwrap();
            let text = format!("{}{separator}{}{fraction}{zone}", date.format("%Y-%m-%d"), time.format("%H:%M:%S"));
            let local = date.and_time(time) + fraction_delta(&fraction);
            match coerce(&text, TYPE_DATETIME) {
                Ok(CoercedValue::NaiveDateTime(value)) => {
                    prop_assert!(zone.is_empty(), "{}", text);
                    prop_assert_eq!(value, local);
                }
                Ok(CoercedValue::DateTime(value)) => {
                    prop_assert_eq!(Some(value.offset().local_minus_utc()), zone_seconds(&zone), "{}", text);
                    prop_assert_eq!(value.naive_local(), local);
                }
                Err(error) => prop_assert!(local.year() > 9999, "{}: {}", text, error),
                Ok(other) => prop_assert!(false, "{}: {:?}", text, other),
            }
        }

        #[test]
        fn a_time_text_of_the_grammar_converts(
            seconds in 0u32..86_400,
            fraction in "(\\.[0-9]{1,9})?",
            zone in "|[Zz]|[+-](0[0-9]|1[0-9]|2[0-3]):?[0-5][0-9]",
        ) {
            let time = NaiveTime::from_num_seconds_from_midnight_opt(seconds, 0).unwrap();
            let text = format!("{}{fraction}{zone}", time.format("%H:%M:%S"));
            let expected = time.overflowing_add_signed(fraction_delta(&fraction)).0;
            match coerce(&text, TYPE_TIME) {
                Ok(CoercedValue::Time(value)) => {
                    prop_assert!(zone.is_empty(), "{}", text);
                    prop_assert_eq!(value, expected);
                }
                Ok(CoercedValue::TimeTz(value, offset)) => {
                    prop_assert_eq!(Some(offset.local_minus_utc()), zone_seconds(&zone), "{}", text);
                    prop_assert_eq!(value, expected);
                }
                other => prop_assert!(false, "{}: {:?}", text, other),
            }
        }

        /// No field of another width, no sign, no whitespace and no other
        /// separator converts (F2).
        #[test]
        fn an_accepted_text_is_of_the_grammar(
            value in concat!(
                "[ \\t+-]?[0-9]{1,5}-[0-9]{1,3}-[0-9]{1,3}",
                "([Tt _]?[0-9]{1,3}:[0-9]{1,3}(:[0-9]{1,3})?([.,][0-9]{0,8})?([Zz]|[+-][0-9]{1,3}:?[0-9]{0,3})?)?[ \\t]?",
                "|[ \\t+T-]?[0-9]{1,3}:[0-9]{1,3}(:[0-9]{1,3})?([.,][0-9]{0,8})?([Zz]|[+-][0-9]{1,3}:?[0-9]{0,3})?[ \\t]?",
                "|[ \\t+-]?[0-9_]{1,6}([.,][0-9]{0,3})?([eE][+-]?[0-9]{0,3})?[ \\t]?",
                "|.{0,24}",
            ),
        ) {
            let date = format!("^{DATE_GRAMMAR}$");
            let clock = format!("^{CLOCK_GRAMMAR}$");
            let datetime = format!("^(?:{DATE_GRAMMAR}[Tt ]{CLOCK_GRAMMAR}|{NUMBER_GRAMMAR})$");
            let number = format!("^(?:{NUMBER_GRAMMAR})$");
            for (type_hint, grammar) in [
                (TYPE_DATE, date),
                (TYPE_TIME, clock),
                (TYPE_DATETIME, datetime),
                (TYPE_INT, number.clone()),
                (TYPE_FLOAT, number),
            ] {
                if coerce(&value, type_hint).is_ok() {
                    let grammar = regex::Regex::new(&grammar).unwrap();
                    prop_assert!(grammar.is_match(&value), "{:?} as {}", value, type_hint);
                }
            }
        }
    }

    /// The grammar of msgspec's date, clock and number texts, as regexes.
    const DATE_GRAMMAR: &str = "[0-9]{4}-[0-9]{2}-[0-9]{2}";
    const CLOCK_GRAMMAR: &str =
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:?[0-9]{2})?";
    const NUMBER_GRAMMAR: &str =
        r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|-?(?i:nan|inf|infinity)";

    /// The time of a fraction text such as ".1234565": its first six digits,
    /// plus one microsecond when the seventh digit is 5 or more.
    fn fraction_delta(fraction: &str) -> TimeDelta {
        let digits = fraction.trim_start_matches('.');
        let kept = &digits[..digits.len().min(6)];
        let micro: i64 = format!("{kept:0<6}").parse().unwrap();
        let round_up = digits.len() > 6 && digits.as_bytes()[6] >= b'5';
        TimeDelta::microseconds(micro + i64::from(round_up))
    }

    /// The UTC offset in seconds of a zone text such as "Z", "+05:30" or "-0530".
    fn zone_seconds(zone: &str) -> Option<i32> {
        let digits: String = zone.chars().filter(char::is_ascii_digit).collect();
        match zone.as_bytes().first()? {
            b'Z' | b'z' => Some(0),
            sign => {
                let seconds = digits[..2].parse::<i32>().unwrap() * 3600
                    + digits[2..].parse::<i32>().unwrap() * 60;
                Some(if *sign == b'-' { -seconds } else { seconds })
            }
        }
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
        for value in ["0000-01-01", "-0001-01-01", "+10000-01-01", "10000-01-01"] {
            let error = coerce_param(value, TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert_eq!(
                error.to_string(),
                format!("Invalid date '{value}': expected format YYYY-MM-DD")
            );
        }
        for value in [
            "0000-01-01T00:00:00",
            "0000-01-01T00:00:00Z",
            "+10000-01-01T00:00:00",
            "10000-01-01T00:00:00",
        ] {
            let error = coerce_param(value, TYPE_DATETIME, DEFAULT_MAX_PARAM_LENGTH).unwrap_err();
            assert!(
                error
                    .to_string()
                    .contains("expected format YYYY-MM-DDTHH:MM:SS"),
                "{value}: {error}"
            );
        }
        assert!(coerce_param("0001-01-01", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
        assert!(coerce_param("9999-12-31", TYPE_DATE, DEFAULT_MAX_PARAM_LENGTH).is_ok());
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
                error.to_string().contains("expected format"),
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
            Ok(CoercedValue::DateTime(_))
        ));

        // ISO 8601 with timezone offset
        assert!(matches!(
            coerce_param(
                "2024-01-15T10:30:00+00:00",
                TYPE_DATETIME,
                DEFAULT_MAX_PARAM_LENGTH
            ),
            Ok(CoercedValue::DateTime(_))
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
        let (repr, utcoffset) = python_repr("2024-01-15T10:30:00", TYPE_DATETIME).unwrap();
        assert_eq!(repr, "datetime.datetime(2024, 1, 15, 10, 30)");
        assert_eq!(utcoffset, "None");
    }

    #[test]
    fn f2_text_that_is_not_iso_8601_is_refused() {
        for (value, type_hint) in [
            ("24-01-15", TYPE_DATE),
            ("2024-1-5", TYPE_DATE),
            ("+2024-01-15", TYPE_DATE),
            (" 2024-01-15", TYPE_DATE),
            ("2024-01-15 ", TYPE_DATE),
            ("24-01-15", TYPE_DATETIME),
            ("2024-1-5", TYPE_DATETIME),
            ("+2024-01-15", TYPE_DATETIME),
            ("2024-01-15", TYPE_DATETIME),
            ("2024-01-1510:30:00", TYPE_DATETIME),
            ("2024-10-31T9:34:5Z", TYPE_DATETIME),
            ("2024-01-15T1:2:3", TYPE_DATETIME),
            ("\t2024-01-15T10:30:00", TYPE_DATETIME),
            ("2024-01-15T 10:30:00", TYPE_DATETIME),
            ("2024-01-15T10:30:0", TYPE_DATETIME),
            ("\t10:30:00", TYPE_TIME),
            ("1:2:3", TYPE_TIME),
            ("10:3", TYPE_TIME),
            ("10:30:0", TYPE_TIME),
            ("10:30", TYPE_TIME),
            ("10:30:00 ", TYPE_TIME),
        ] {
            assert!(
                coerce(value, type_hint).is_err(),
                "{value:?} converted to {:?}",
                coerce(value, type_hint)
            );
        }
    }

    #[test]
    fn f3_an_int_beyond_i64_converts_as_python_does() {
        let longest = "9".repeat(4300);
        for value in [
            "9223372036854775808",
            "-9223372036854775809",
            "18446744073709551616",
            "-340282366920938463463374607431768211457",
            &longest,
        ] {
            let (repr, _) =
                python_repr(value, TYPE_INT).unwrap_or_else(|| panic!("{value} was refused"));
            assert_eq!(repr, value);
        }
        // msgspec, and so the list[int] path, refuses these forms.
        let too_long = "9".repeat(4301);
        let negative_too_long = format!("-{longest}");
        for value in [
            "+5",
            "007",
            "-007",
            "1_000",
            " 5",
            "5 ",
            "\u{0663}",
            "\u{ff15}",
            &too_long,
            &negative_too_long,
        ] {
            assert!(coerce(value, TYPE_INT).is_err(), "{value:?}");
        }
        // msgspec reads a whole number in float form as an int.
        for (value, expected) in [
            ("5.0", "5"),
            ("1e3", "1000"),
            ("-0.0", "0"),
            ("1e-999", "0"),
            ("9007199254740992.0", "9007199254740992"),
        ] {
            assert_eq!(python_repr(value, TYPE_INT).unwrap().0, expected, "{value}");
        }
        for value in ["1.5", "9007199254740994.0", "1e999", "nan", "inf"] {
            assert!(coerce(value, TYPE_INT).is_err(), "{value:?}");
        }
    }

    #[test]
    fn f4_iso_8601_forms_that_python_accepts() {
        for (value, expected) in [
            (
                "2024-01-15T10:30:00+0530",
                "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))",
            ),
            (
                "2024-01-15T10:30:00.123-0530",
                "datetime.datetime(2024, 1, 15, 10, 30, 0, 123000, tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=66600)))",
            ),
            (
                "2024-01-15t10:30:00",
                "datetime.datetime(2024, 1, 15, 10, 30)",
            ),
            (
                "2024-01-15T10:30:00z",
                "datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)",
            ),
            // msgspec rounds to the closest microsecond by the seventh digit.
            (
                "2024-01-15T10:30:00.1234565",
                "datetime.datetime(2024, 1, 15, 10, 30, 0, 123457)",
            ),
            (
                "2024-01-15T10:30:00.12345649",
                "datetime.datetime(2024, 1, 15, 10, 30, 0, 123456)",
            ),
            (
                "2024-02-29T23:59:59.9999999",
                "datetime.datetime(2024, 3, 1, 0, 0)",
            ),
            // msgspec reads a number as a Unix timestamp in UTC.
            (
                "1700000000",
                "datetime.datetime(2023, 11, 14, 22, 13, 20, tzinfo=datetime.timezone.utc)",
            ),
            (
                "-1.5",
                "datetime.datetime(1969, 12, 31, 23, 59, 58, 500000, tzinfo=datetime.timezone.utc)",
            ),
        ] {
            assert_eq!(
                python_repr(value, TYPE_DATETIME)
                    .unwrap_or_else(|| panic!("{value} was refused"))
                    .0,
                expected,
                "{value}"
            );
        }
        // msgspec, and so the list[datetime] path, refuses these forms.
        for value in [
            "2024-01-15T10:30",
            "2024-01-15T10",
            "20240115T103000",
            "2024-W03-1T10:30:00",
            "2024-01-15T10:30:00,5",
            "2024-01-15T10:30:00+05",
            "2024-01-15T10:30:00+05:30:15",
            "2024-01-15T10:30:00+24:00",
            "2024-01-15T24:00:00",
            "9999-12-31T23:59:59.9999995",
            "253402300800",
            "-62135596800.5",
            "-16964e15",
            "9.3e18",
            "1e300",
            "-1e300",
            "nan",
            "2024-01-15T10:30:00\0",
        ] {
            assert!(coerce(value, TYPE_DATETIME).is_err(), "{value:?}");
        }
        for value in [
            "20240115",
            "2024-W03-1",
            "2024-015",
            "0000-01-01",
            "2023-02-29",
        ] {
            assert!(coerce(value, TYPE_DATE).is_err(), "{value:?}");
        }
    }

    #[test]
    fn f5_a_time_with_an_offset_converts() {
        for (value, expected) in [
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
            ("10:30:00", "datetime.time(10, 30)"),
            ("23:59:59.9999995", "datetime.time(0, 0)"),
        ] {
            assert_eq!(
                python_repr(value, TYPE_TIME)
                    .unwrap_or_else(|| panic!("{value} was refused"))
                    .0,
                expected,
                "{value}"
            );
        }
        for value in [
            "10",
            "1030",
            "10:30:00,5",
            "10:30:00+05",
            "T10:30:00",
            "24:00:00",
        ] {
            assert!(coerce(value, TYPE_TIME).is_err(), "{value:?}");
        }
    }

    #[test]
    fn a_float_is_read_as_msgspec_reads_it() {
        for value in [
            "+1.5", ".5", "5.", "+inf", "+nan", "1e999", "-1e999", "1_000.5", " 1.5",
        ] {
            assert!(coerce(value, TYPE_FLOAT).is_err(), "{value:?}");
        }
        assert!(matches!(coerce("-0", TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v.to_bits() == 0));
        assert!(
            matches!(coerce("-0.0", TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v.to_bits() == (-0.0f64).to_bits())
        );
        for (value, expected) in [
            ("1.5", 1.5),
            ("1e-999", 0.0),
            ("Infinity", f64::INFINITY),
            ("-INF", f64::NEG_INFINITY),
            ("18446744073709551616", 18446744073709551616.0),
        ] {
            assert!(
                matches!(coerce(value, TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v == expected),
                "{value}"
            );
        }
        assert!(matches!(coerce("-nAn", TYPE_FLOAT), Ok(CoercedValue::Float(v)) if v.is_nan()));
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
