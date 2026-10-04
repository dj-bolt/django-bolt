//! Typed readers for Django settings and `DJANGO_BOLT_*` environment variables.
//!
//! Bolt reads its settings once at startup. A missing setting gives the
//! default. A present setting with the wrong type raises `ImproperlyConfigured`
//! that names the setting. A wrong type must never become the default without
//! an error: `BOLT_MAX_UPLOAD_SIZE = "10485760"` would otherwise give 413 for a
//! 2 MB upload.
//!
//! An environment variable follows the same rule. An unset or empty variable
//! gives the default. A value that does not parse raises `ImproperlyConfigured`.

use pyo3::conversion::FromPyObjectOwned;
use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyType};
use std::env::VarError;
use std::str::FromStr;
use std::time::Duration;

/// The Django settings object, with typed readers.
pub struct DjangoSettings<'py> {
    settings: Bound<'py, PyAny>,
}

impl<'py> DjangoSettings<'py> {
    /// Get `django.conf.settings`.
    pub fn load(py: Python<'py>) -> PyResult<Self> {
        let settings = py.import("django.conf")?.getattr("settings")?;
        Ok(Self { settings })
    }

    pub fn py(&self) -> Python<'py> {
        self.settings.py()
    }

    /// Get the value of a setting, or None when the setting is missing.
    ///
    /// Any other error while Django reads the value raises
    /// `ImproperlyConfigured` that names the setting.
    pub fn get(&self, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = self.settings.py();
        match self.settings.getattr(name) {
            Ok(value) => Ok(Some(value)),
            Err(err) if is_missing(py, &err, name) => Ok(None),
            Err(err) => {
                let wrapped = improperly_configured(
                    py,
                    format!("Cannot read the Django setting {name}: {err}"),
                );
                wrapped.set_cause(py, Some(err));
                Err(wrapped)
            }
        }
    }

    /// Read a bool setting. An int is not a bool.
    pub fn bool(&self, name: &str, default: bool) -> PyResult<bool> {
        let Some(value) = self.get(name)? else {
            return Ok(default);
        };
        value
            .cast::<PyBool>()
            .map(|flag| flag.is_true())
            .map_err(|_| wrong_type(name, "a bool", &value))
    }

    /// Read an int setting that is 0 or more.
    pub fn non_negative_int<T: FromPyObjectOwned<'py>>(
        &self,
        name: &str,
        default: T,
    ) -> PyResult<T> {
        Ok(self
            .int(name, "an int of 0 or more", |_| true)?
            .unwrap_or(default))
    }

    /// Read an int setting that is 1 or more.
    pub fn positive_int<T>(&self, name: &str, default: T) -> PyResult<T>
    where
        T: FromPyObjectOwned<'py> + PartialOrd + From<u8>,
    {
        Ok(self
            .int(name, "an int of 1 or more", |value: &T| {
                *value >= T::from(1)
            })?
            .unwrap_or(default))
    }

    /// Read an int setting from 0 to `u32::MAX`, or None when it is missing.
    pub fn optional_u32(&self, name: &str) -> PyResult<Option<u32>> {
        self.int(name, "an int from 0 to 4294967295", |_| true)
    }

    /// Read a number of seconds that is more than 0. An int or a float is valid.
    pub fn positive_seconds(&self, name: &str, default: Duration) -> PyResult<Duration> {
        let Some(value) = self.get(name)? else {
            return Ok(default);
        };
        if !value.is_instance_of::<PyBool>() {
            if let Ok(seconds) = value.extract::<f64>() {
                if seconds > 0.0 {
                    // try_from_secs_f64 also rejects NaN, infinity and overflow.
                    if let Ok(duration) = Duration::try_from_secs_f64(seconds) {
                        return Ok(duration);
                    }
                }
            }
        }
        Err(wrong_type(name, "a number more than 0", &value))
    }

    /// Read a list of str, or None when the setting is missing.
    /// A str is not a list of str.
    pub fn str_list(&self, name: &str) -> PyResult<Option<Vec<String>>> {
        let Some(value) = self.get(name)? else {
            return Ok(None);
        };
        value
            .extract::<Vec<String>>()
            .map(Some)
            .map_err(|_| wrong_type(name, "a list of str", &value))
    }

    /// Read a str setting, or None when the setting is missing or None.
    pub fn optional_str(&self, name: &str) -> PyResult<Option<String>> {
        let Some(value) = self.get(name)? else {
            return Ok(None);
        };
        if value.is_none() {
            return Ok(None);
        }
        value
            .extract::<String>()
            .map(Some)
            .map_err(|_| wrong_type(name, "a str or None", &value))
    }

    /// Read an int setting. A bool, a value that is not an int, and a value
    /// that `T` cannot hold or that `valid` rejects raise.
    fn int<T: FromPyObjectOwned<'py>>(
        &self,
        name: &str,
        expected: &str,
        valid: impl Fn(&T) -> bool,
    ) -> PyResult<Option<T>> {
        let Some(value) = self.get(name)? else {
            return Ok(None);
        };
        if !value.is_instance_of::<PyBool>() {
            if let Ok(number) = value.extract::<T>() {
                if valid(&number) {
                    return Ok(Some(number));
                }
            }
        }
        Err(wrong_type(name, expected, &value))
    }
}

/// Parse the value of a `DJANGO_BOLT_*` environment variable.
///
/// No value, an empty value or only spaces gives None. A value that does not
/// parse, or that `valid` rejects, gives an error message that names the
/// variable.
pub fn parse_env<T: FromStr>(
    name: &str,
    raw: Option<&str>,
    expected: &str,
    valid: impl Fn(&T) -> bool,
) -> Result<Option<T>, String> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let value = raw.trim();
    if value.is_empty() {
        return Ok(None);
    }
    match value.parse::<T>() {
        Ok(parsed) if valid(&parsed) => Ok(Some(parsed)),
        _ => Err(format!("{name} must be {expected}, got '{raw}'.")),
    }
}

/// Parse a flag variable: 1, 0, true or false, in any case.
pub fn parse_env_flag(name: &str, raw: Option<&str>) -> Result<Option<bool>, String> {
    let flag = parse_env::<String>(name, raw, "1, 0, true or false", |value| {
        matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "0" | "true" | "false"
        )
    })?;
    Ok(flag.map(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true")))
}

/// Read a `DJANGO_BOLT_*` environment variable at startup. See `parse_env`.
/// An invalid value raises `ImproperlyConfigured`.
pub fn env_var<T: FromStr>(
    py: Python<'_>,
    name: &str,
    expected: &str,
    valid: impl Fn(&T) -> bool,
) -> PyResult<Option<T>> {
    let raw = read_env(py, name, expected)?;
    parse_env(name, raw.as_deref(), expected, valid)
        .map_err(|message| improperly_configured(py, message))
}

/// Read an int variable that is 1 or more.
pub fn env_positive_int<T>(py: Python<'_>, name: &str, default: T) -> PyResult<T>
where
    T: FromStr + PartialOrd + From<u8>,
{
    Ok(env_var(py, name, "an int of 1 or more", |value: &T| {
        *value >= T::from(1)
    })?
    .unwrap_or(default))
}

/// Read an int variable that is 0 or more.
pub fn env_non_negative_int<T>(py: Python<'_>, name: &str, default: T) -> PyResult<T>
where
    T: FromStr + PartialOrd + From<u8>,
{
    Ok(env_var(py, name, "an int of 0 or more", |value: &T| {
        *value >= T::from(0)
    })?
    .unwrap_or(default))
}

/// Read a flag variable: 1, 0, true or false.
pub fn env_flag(py: Python<'_>, name: &str, default: bool) -> PyResult<bool> {
    let raw = read_env(py, name, "1, 0, true or false")?;
    Ok(parse_env_flag(name, raw.as_deref())
        .map_err(|message| improperly_configured(py, message))?
        .unwrap_or(default))
}

/// Read the raw value of an environment variable. A value that is not UTF-8
/// raises `ImproperlyConfigured`.
fn read_env(py: Python<'_>, name: &str, expected: &str) -> PyResult<Option<String>> {
    match std::env::var(name) {
        Ok(raw) => Ok(Some(raw)),
        Err(VarError::NotPresent) => Ok(None),
        Err(VarError::NotUnicode(_)) => Err(improperly_configured(
            py,
            format!("{name} must be {expected}, got a value that is not UTF-8."),
        )),
    }
}

/// Return true when `err` tells that the setting `name` is not defined.
///
/// Django raises AttributeError for a setting that is not defined, and Python
/// sets the `name` of that error to the setting. An AttributeError for another
/// name comes from code that reads the value. For example, Django calls
/// `str.startswith` on STATIC_URL. That is an error.
fn is_missing(py: Python<'_>, err: &PyErr, name: &str) -> bool {
    err.is_instance_of::<PyAttributeError>(py)
        && err
            .value(py)
            .getattr("name")
            .and_then(|attr| attr.extract::<String>())
            .is_ok_and(|attr| attr == name)
}

/// Make the `ImproperlyConfigured` error for a setting with the wrong type.
fn wrong_type(name: &str, expected: &str, value: &Bound<'_, PyAny>) -> PyErr {
    let type_name = value
        .get_type()
        .name()
        .map(|type_name| type_name.to_string())
        .unwrap_or_else(|_| "?".to_string());
    let repr = value
        .repr()
        .map(|repr| repr.to_string())
        .unwrap_or_else(|_| "?".to_string());
    improperly_configured(
        value.py(),
        format!("{name} must be {expected}, got {type_name} {repr}."),
    )
}

/// Make a Django `ImproperlyConfigured` error.
pub fn improperly_configured(py: Python<'_>, message: String) -> PyErr {
    let class = py
        .import("django.core.exceptions")
        .and_then(|module| module.getattr("ImproperlyConfigured"))
        .and_then(|class| class.cast_into::<PyType>().map_err(PyErr::from));
    match class {
        Ok(class) => PyErr::from_type(class, message),
        Err(err) => err,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_env_reads_a_valid_value() {
        assert_eq!(
            parse_env::<u32>("X", Some(" 4 "), "an int", |_| true),
            Ok(Some(4))
        );
    }

    #[test]
    fn parse_env_treats_unset_and_empty_as_missing() {
        for raw in [None, Some(""), Some("  ")] {
            assert_eq!(parse_env::<u32>("X", raw, "an int", |_| true), Ok(None));
        }
    }

    #[test]
    fn parse_env_names_the_variable_of_an_invalid_value() {
        assert_eq!(
            parse_env::<u32>("X", Some("4x"), "an int", |_| true),
            Err("X must be an int, got '4x'.".to_string())
        );
        assert_eq!(
            parse_env::<u32>("X", Some("0"), "an int of 1 or more", |n| *n >= 1),
            Err("X must be an int of 1 or more, got '0'.".to_string())
        );
    }

    #[test]
    fn parse_env_flag_reads_1_0_true_and_false() {
        for (raw, expected) in [
            ("1", true),
            ("true", true),
            ("TRUE", true),
            ("0", false),
            ("False", false),
        ] {
            assert_eq!(parse_env_flag("X", Some(raw)), Ok(Some(expected)), "{raw}");
        }
        assert_eq!(parse_env_flag("X", None), Ok(None));
        assert_eq!(
            parse_env_flag("X", Some("yes")),
            Err("X must be 1, 0, true or false, got 'yes'.".to_string())
        );
    }
}
