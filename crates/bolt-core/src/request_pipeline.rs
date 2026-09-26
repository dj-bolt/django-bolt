//! Shared request pipeline logic for production and test handlers.
//!
//! This module contains validation and processing logic that is common
//! between the production handler (handler.rs) and test handler (testing.rs).

use actix_web::{HttpRequest, HttpResponse};
use ahash::AHashMap;

use crate::form_parsing::ValidationError;
use crate::responses;
use crate::type_coercion::{
    coerce_param, coerced_value_to_py, CoercedValue, CoercedValues, TypeHints, TYPE_STRING,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Empty type-hint map for requests that have no route metadata.
pub static EMPTY_TYPES: std::sync::LazyLock<TypeHints> = std::sync::LazyLock::new(TypeHints::new);

/// Validate and pre-coerce path/query parameters against type hints.
///
/// Returns the pre-coerced non-string values of each source. String
/// parameters are validated for length but left as-is.
pub fn validate_and_cache_typed_params<'a>(
    path_params: Option<&'a AHashMap<String, String>>,
    query_params: Option<&'a AHashMap<String, String>>,
    param_types: &'a TypeHints,
    max_length: usize,
) -> Result<(CoercedValues<'a>, CoercedValues<'a>), HttpResponse> {
    let path_coerced = match path_params {
        Some(params) => {
            validate_and_cache_source(params, param_types, max_length, "Path parameter")?
        }
        None => Vec::new(),
    };
    let query_coerced = match query_params {
        Some(params) => {
            validate_and_cache_source(params, param_types, max_length, "Query parameter")?
        }
        None => Vec::new(),
    };
    Ok((path_coerced, query_coerced))
}

/// Validate the values of one request source and pre-coerce its typed values.
///
/// `label` names the source in the 422 detail, for example "Header".
/// The length limit applies to all values, including strings. `types` holds
/// the non-string type hints, keyed by the name in `values`. The result holds
/// only the coerced values. An empty result does not allocate.
///
/// The loop walks the smaller side: a request has many headers but a route
/// types few of them, while path and query maps are usually small.
pub fn validate_and_cache_source<'a>(
    values: &'a AHashMap<String, String>,
    types: &'a TypeHints,
    max_length: usize,
    label: &str,
) -> Result<CoercedValues<'a>, HttpResponse> {
    let mut coerced_values = CoercedValues::new();
    if types.len() < values.len() {
        for (name, value) in values {
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(too_long(label, name, value.len(), max_length));
            }
        }
        for (name, &type_hint) in types {
            if let Some(value) = values.get(name) {
                coerce_into(
                    &mut coerced_values,
                    name,
                    value,
                    type_hint,
                    max_length,
                    label,
                )?;
            }
        }
    } else {
        for (name, value) in values {
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(too_long(label, name, value.len(), max_length));
            }
            if let Some(&type_hint) = types.get(name) {
                coerce_into(
                    &mut coerced_values,
                    name,
                    value,
                    type_hint,
                    max_length,
                    label,
                )?;
            }
        }
    }
    Ok(coerced_values)
}

#[cold]
fn too_long(label: &str, name: &str, len: usize, max_length: usize) -> HttpResponse {
    responses::error_422_validation(&format!(
        "{} '{}': Parameter too long: {} bytes (max {} bytes)",
        label, name, len, max_length
    ))
}

#[inline]
fn coerce_into<'a>(
    coerced_values: &mut CoercedValues<'a>,
    name: &'a str,
    value: &str,
    type_hint: u8,
    max_length: usize,
    label: &str,
) -> Result<(), HttpResponse> {
    if type_hint == TYPE_STRING {
        return Ok(());
    }
    match coerce_param(value, type_hint, max_length) {
        Ok(coerced) => {
            coerced_values.push((name, coerced));
            Ok(())
        }
        Err(error) => Err(responses::error_422_validation(&format!(
            "{} '{}': {}",
            label, name, error
        ))),
    }
}

/// Coerce one value when `types` gives it a non-string type hint.
///
/// Returns `Ok(None)` for a value with no type hint or a string type hint.
/// The error is the 422 detail text, prefixed with `label` and `name`.
#[inline]
pub fn coerce_declared_value(
    name: &str,
    value: &str,
    types: &TypeHints,
    max_length: usize,
    label: &str,
) -> Result<Option<CoercedValue>, String> {
    match types.get(name) {
        Some(&type_hint) if type_hint != TYPE_STRING => coerce_param(value, type_hint, max_length)
            .map(Some)
            .map_err(|error| format!("{} '{}': {}", label, name, error)),
        _ => Ok(None),
    }
}

/// Set `name` in a WebSocket scope dict, coerced when `types` declares it.
///
/// A bad typed value is a `ValueError`, which rejects the upgrade. Values with
/// no type hint stay strings and get no length check, as before.
pub fn set_declared_item(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    name: &str,
    value: &str,
    types: &TypeHints,
    max_length: usize,
    label: &str,
) -> PyResult<()> {
    match coerce_declared_value(name, value, types, max_length, label) {
        Ok(Some(coerced)) => dict.set_item(name, coerced_value_to_py(py, &coerced)),
        Ok(None) => dict.set_item(name, value),
        Err(detail) => Err(pyo3::exceptions::PyValueError::new_err(detail)),
    }
}

/// Extract headers from request with validation
/// OPTIMIZATION: HeaderName::as_str() already returns lowercase (http crate canonical form)
/// so we skip the redundant to_ascii_lowercase() call (~50ns saved per header)
/// OPTIMIZATION: #[inline] on hot path - called on every request
#[inline]
pub fn extract_headers(
    req: &HttpRequest,
    max_header_size: usize,
) -> Result<AHashMap<String, String>, HttpResponse> {
    const MAX_HEADERS: usize = 100;
    let mut headers: AHashMap<String, String> = AHashMap::with_capacity(16);
    let mut header_count = 0;

    for (name, value) in req.headers().iter() {
        header_count += 1;
        if header_count > MAX_HEADERS {
            return Err(responses::error_400_too_many_headers());
        }
        if let Ok(v) = value.to_str() {
            if v.len() > max_header_size {
                return Err(responses::error_400_header_too_large(max_header_size));
            }
            // HeaderName::as_str() returns lowercase already (http crate stores canonically)
            headers.insert(name.as_str().to_owned(), v.to_owned());
        }
    }
    Ok(headers)
}

/// Build HTTP 422 response for validation errors
pub fn build_validation_error_response(error: &ValidationError) -> HttpResponse {
    let body = serde_json::json!({
        "detail": [error.to_json()]
    });
    HttpResponse::UnprocessableEntity()
        .content_type("application/json")
        .body(body.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::type_coercion::{TYPE_BOOL, TYPE_INT};
    use actix_web::body::MessageBody;

    fn values(pairs: &[(&str, &str)]) -> AHashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn types(pairs: &[(&str, u8)]) -> TypeHints {
        pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
    }

    fn detail(response: HttpResponse) -> String {
        assert_eq!(response.status(), 422);
        let bytes = response.into_body().try_into_bytes().unwrap();
        String::from_utf8(bytes.to_vec()).unwrap()
    }

    #[test]
    fn source_coerces_only_typed_values() {
        // Fewer types than values: the loop walks the types.
        let headers = values(&[
            ("x-count", "5"),
            ("x-debug", "yes"),
            ("accept", "text/html"),
        ]);
        let hints = types(&[("x-count", TYPE_INT), ("x-debug", TYPE_BOOL)]);
        let mut coerced = validate_and_cache_source(&headers, &hints, 64, "Header").unwrap();
        coerced.sort_by_key(|(name, _)| *name);
        assert_eq!(coerced.len(), 2);
        assert!(matches!(coerced[0], ("x-count", CoercedValue::Int(5))));
        assert!(matches!(coerced[1], ("x-debug", CoercedValue::Bool(true))));
    }

    #[test]
    fn source_walks_values_when_types_are_more() {
        let query = values(&[("page", "2")]);
        let hints = types(&[("page", TYPE_INT), ("limit", TYPE_INT), ("q", TYPE_STRING)]);
        let coerced = validate_and_cache_source(&query, &hints, 64, "Query parameter").unwrap();
        assert!(matches!(coerced[..], [("page", CoercedValue::Int(2))]));
        let bad = values(&[("limit", "x")]);
        let response = validate_and_cache_source(&bad, &hints, 64, "Query parameter").unwrap_err();
        assert!(detail(response).contains("Query parameter 'limit'"));
    }

    #[test]
    fn source_with_no_types_does_not_allocate() {
        let headers = values(&[("accept", "*/*")]);
        let hints = types(&[]);
        let coerced = validate_and_cache_source(&headers, &hints, 64, "Header").unwrap();
        assert!(coerced.is_empty());
        assert_eq!(coerced.capacity(), 0);
    }

    #[test]
    fn source_rejects_invalid_value_with_label_and_name() {
        let headers = values(&[("x-count", "abc")]);
        let hints = types(&[("x-count", TYPE_INT)]);
        let response = validate_and_cache_source(&headers, &hints, 64, "Header").unwrap_err();
        let body = detail(response);
        assert!(body.contains("Header 'x-count'"), "{body}");
        assert!(body.contains("abc"), "{body}");
    }

    #[test]
    fn source_rejects_long_untyped_value_in_both_loop_orders() {
        let long = "a".repeat(65);
        // More values than types: the length loop runs on its own.
        let cookies = values(&[("a", "1"), ("b", "2"), ("session", &long)]);
        let response =
            validate_and_cache_source(&cookies, &types(&[("a", TYPE_INT)]), 64, "Cookie")
                .unwrap_err();
        assert!(detail(response).contains("Cookie 'session': Parameter too long"));
        // No more values than types: the length check runs in the value loop.
        let single = values(&[("session", &long)]);
        let response = validate_and_cache_source(&single, &types(&[("a", TYPE_INT)]), 64, "Cookie")
            .unwrap_err();
        assert!(detail(response).contains("Cookie 'session': Parameter too long"));
    }

    #[test]
    fn declared_value_skips_string_and_unknown_names() {
        let hints = types(&[("x-name", TYPE_STRING)]);
        assert!(coerce_declared_value("x-name", "1", &hints, 64, "Header")
            .unwrap()
            .is_none());
        assert!(coerce_declared_value("other", "1", &hints, 64, "Header")
            .unwrap()
            .is_none());
    }

    #[test]
    fn path_and_query_keep_their_labels() {
        let hints = types(&[("id", TYPE_INT)]);
        let bad = values(&[("id", "x")]);
        let response = validate_and_cache_typed_params(Some(&bad), None, &hints, 64).unwrap_err();
        assert!(detail(response).contains("Path parameter 'id'"));
        let response = validate_and_cache_typed_params(None, Some(&bad), &hints, 64).unwrap_err();
        assert!(detail(response).contains("Query parameter 'id'"));
    }
}
