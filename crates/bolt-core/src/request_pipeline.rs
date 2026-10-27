//! Shared request pipeline logic for production and test handlers.
//!
//! This module contains validation and processing logic that is common
//! between the production handler (handler.rs) and test handler (testing.rs).

// A validator here returns the error response of the request as its `Err`.
// Since actix-web 4.15, `HttpResponse` is larger than the 128-byte limit of
// this lint. A `Box` would make the `Result` smaller, but it would add an
// allocation to each error response. The size costs the success path
// nothing, because a call writes only the `Ok` value.
#![allow(clippy::result_large_err)]

use actix_web::http::header::HeaderValue;
use actix_web::{HttpRequest, HttpResponse};
use ahash::{AHashMap, AHashSet};
use std::borrow::{Borrow, Cow};
use std::collections::hash_map::Entry;
use std::hash::Hash;

use crate::form_parsing::ValidationError;
use crate::responses;
use crate::router::{QueryParams, QuerySequences};
use crate::type_coercion::{
    coerce_param, coerced_value_to_py, CoercedValue, CoercedValues, TypeHints, TYPE_STRING,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Empty type-hint map for requests that have no route metadata.
pub static EMPTY_TYPES: std::sync::LazyLock<TypeHints> = std::sync::LazyLock::new(TypeHints::new);

/// Validate and pre-coerce path/query parameters against type hints.
///
/// Each source uses its own type hints, so a query key with the name of a
/// typed path parameter keeps its string. Returns the pre-coerced non-string
/// values of each source. String parameters are validated for length but left as-is.
pub fn validate_and_cache_typed_params<'t>(
    path_params: Option<&AHashMap<String, String>>,
    query_params: Option<&QueryParams<'_>>,
    path_types: &'t TypeHints,
    query_types: &'t TypeHints,
    max_length: usize,
) -> Result<(CoercedValues<'t>, CoercedValues<'t>), HttpResponse> {
    let path_coerced = match path_params {
        Some(params) => {
            validate_and_cache_source(params, path_types, max_length, "Path parameter")?
        }
        None => Vec::new(),
    };
    let query_coerced = match query_params {
        Some(params) => {
            validate_and_cache_source(params, query_types, max_length, "Query parameter")?
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
///
/// The names in the result borrow from `types`, the route metadata, not from
/// `values`. The caller can thus move the request map while it holds the result.
///
/// `values` can own its strings (headers, cookies) or borrow them (query).
pub fn validate_and_cache_source<'t, K, V>(
    values: &AHashMap<K, V>,
    types: &'t TypeHints,
    max_length: usize,
    label: &str,
) -> Result<CoercedValues<'t>, HttpResponse>
where
    K: Borrow<str> + Eq + Hash,
    V: AsRef<str>,
{
    let mut coerced_values = CoercedValues::new();
    if types.len() < values.len() {
        for (name, value) in values {
            let value = value.as_ref();
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(too_long(label, name.borrow(), value.len(), max_length));
            }
        }
        for (name, &type_hint) in types {
            if let Some(value) = values.get(name.as_str()) {
                coerce_into(
                    &mut coerced_values,
                    name,
                    value.as_ref(),
                    type_hint,
                    max_length,
                    label,
                )?;
            }
        }
    } else {
        for (name, value) in values {
            let (name, value): (&str, &str) = (name.borrow(), value.as_ref());
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(too_long(label, name, value.len(), max_length));
            }
            if let Some((typed_name, &type_hint)) = types.get_key_value(name) {
                coerce_into(
                    &mut coerced_values,
                    typed_name,
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

/// The values of each sequence query key of a route, in the order of the query.
///
/// A route with no sequence keys, or a request with no query, gives an empty
/// result that does not allocate. The length limit applies to each value: the
/// scalar parser keeps the last value of a key, so it does not check the others.
/// The error is the rejected parameter, for HTTP and WebSocket.
pub fn query_sequences<'a>(
    query: Option<&'a str>,
    keys: &AHashSet<String>,
    max_length: usize,
) -> Result<QuerySequences<'a>, ParamError> {
    let query = match query {
        Some(query) if !keys.is_empty() && !query.is_empty() => query,
        _ => return Ok(Vec::new()),
    };
    let sequences = crate::router::collect_query_sequences(query, keys);
    for (name, values) in &sequences {
        if let Some(value) = values.iter().find(|value| value.len() > max_length) {
            return Err(ParamError {
                label: "Query parameter",
                name: name.to_string(),
                msg: too_long_msg(value.len(), max_length),
            });
        }
    }
    Ok(sequences)
}

/// Put each sequence query key in the Python query dict as a list of its values.
pub fn set_query_sequences(
    py: Python<'_>,
    query_dict: &Bound<'_, PyDict>,
    sequences: &[(Cow<'_, str>, Vec<Cow<'_, str>>)],
) -> PyResult<()> {
    for (name, values) in sequences {
        query_dict.set_item(name, pyo3::types::PyList::new(py, values)?)?;
    }
    Ok(())
}

/// A request value that fails validation: its source label, its name and the reason.
#[derive(Debug)]
pub struct ParamError {
    pub label: &'static str,
    pub name: String,
    pub msg: String,
}

impl ParamError {
    /// The 422 response for this error.
    #[cold]
    pub fn into_response(self) -> HttpResponse {
        responses::error_422_param(self.label, &self.name, &self.msg)
    }
}

/// The detail text, for example "Query parameter 'tag': Parameter too long: ...".
impl std::fmt::Display for ParamError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} '{}': {}", self.label, self.name, self.msg)
    }
}

impl From<ParamError> for PyErr {
    fn from(error: ParamError) -> Self {
        pyo3::exceptions::PyValueError::new_err(error.to_string())
    }
}

#[cold]
fn too_long(label: &str, name: &str, len: usize, max_length: usize) -> HttpResponse {
    responses::error_422_param(label, name, &too_long_msg(len, max_length))
}

#[cold]
fn too_long_detail(label: &str, name: &str, len: usize, max_length: usize) -> String {
    format!("{} '{}': {}", label, name, too_long_msg(len, max_length))
}

#[cold]
fn too_long_msg(len: usize, max_length: usize) -> String {
    format!(
        "Parameter too long: {} bytes (max {} bytes)",
        len, max_length
    )
}

#[inline]
fn coerce_into<'t>(
    coerced_values: &mut CoercedValues<'t>,
    name: &'t str,
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
        Err(error) => Err(responses::error_422_param(label, name, &error.to_string())),
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

/// Coerce one path or query value as `types` declares, and check its length.
///
/// Unlike [`coerce_declared_value`], the length limit applies to all values,
/// including strings. `coerce_param` checks a typed value, so this function
/// checks only a value that it does not coerce. The error is the detail text.
#[inline]
pub fn coerce_param_value(
    name: &str,
    value: &str,
    types: &TypeHints,
    max_length: usize,
    label: &str,
) -> Result<Option<CoercedValue>, String> {
    let coerced = coerce_declared_value(name, value, types, max_length, label)?;
    if coerced.is_none() && value.len() > max_length {
        return Err(too_long_detail(label, name, value.len(), max_length));
    }
    Ok(coerced)
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
    let coerced = coerce_declared_value(name, value, types, max_length, label);
    set_coerced_item(py, dict, name, value, coerced)
}

/// Set a path or query `name` in a WebSocket scope dict, coerced when `types` declares it.
///
/// A value that is too long or a bad typed value is a `ValueError`.
/// The error rejects the upgrade, as a header or cookie error does.
pub fn set_param_item(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    name: &str,
    value: &str,
    types: &TypeHints,
    max_length: usize,
    label: &str,
) -> PyResult<()> {
    let coerced = coerce_param_value(name, value, types, max_length, label);
    set_coerced_item(py, dict, name, value, coerced)
}

#[inline]
fn set_coerced_item(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    name: &str,
    value: &str,
    coerced: Result<Option<CoercedValue>, String>,
) -> PyResult<()> {
    match coerced {
        Ok(Some(coerced)) => dict.set_item(name, coerced_value_to_py(py, &coerced)?),
        Ok(None) => dict.set_item(name, value),
        Err(detail) => Err(pyo3::exceptions::PyValueError::new_err(detail)),
    }
}

/// Return the text of a header value, or None. It accepts the same values as
/// `HeaderValue::to_str`: visible ASCII and tab. actix-web uses http 0.2.
/// Its `to_str` stops at the first bad byte, so the loop does not vectorize.
/// This loop has no early exit, as in http 1.
#[inline]
pub fn header_str(value: &HeaderValue) -> Option<&str> {
    let bytes = value.as_bytes();
    let mut invalid = false;
    for &byte in bytes {
        invalid |= !((32..127).contains(&byte) || byte == b'\t');
    }
    if invalid {
        return None;
    }
    std::str::from_utf8(bytes).ok()
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
        if let Some(v) = header_str(value) {
            if v.len() > max_header_size {
                return Err(responses::error_400_header_too_large(max_header_size));
            }
            // HeaderName::as_str() returns lowercase already (http crate stores canonically)
            insert_header_line(&mut headers, name.as_str(), v);
        }
    }
    Ok(headers)
}

/// Validate every header but copy only repeated Cookie lines.
pub fn extract_cookie_header(
    req: &HttpRequest,
    max_header_size: usize,
) -> Result<Option<Cow<'_, str>>, HttpResponse> {
    let mut cookie: Option<Cow<'_, str>> = None;
    for (index, (name, value)) in req.headers().iter().enumerate() {
        if index >= 100 {
            return Err(responses::error_400_too_many_headers());
        }
        if let Some(value) = header_str(value) {
            if value.len() > max_header_size {
                return Err(responses::error_400_header_too_large(max_header_size));
            }
            if name == actix_web::http::header::COOKIE {
                if let Some(joined) = cookie.as_mut() {
                    joined.to_mut().push_str("; ");
                    joined.to_mut().push_str(value);
                } else {
                    cookie = Some(Cow::Borrowed(value));
                }
            }
        }
    }
    Ok(cookie)
}

/// Add one header line to `headers`. `name` must be lowercase.
///
/// Django joins repeated `Cookie` lines with `"; "`, so each line counts.
/// Another repeated header keeps its last line.
#[inline]
pub fn insert_header_line(headers: &mut AHashMap<String, String>, name: &str, value: &str) {
    match headers.entry(name.to_owned()) {
        // The common request: the first line of a name takes the vacant slot.
        // One hash and the same two copies as a plain insert. No join.
        Entry::Vacant(slot) => {
            slot.insert(value.to_owned());
        }
        // Only a second `Cookie` line gets here and grows the joined value.
        Entry::Occupied(mut joined) if name == "cookie" => {
            let joined = joined.get_mut();
            joined.push_str("; ");
            joined.push_str(value);
        }
        Entry::Occupied(mut last) => {
            last.insert(value.to_owned());
        }
    }
}

/// Build HTTP 422 response for validation errors
pub fn build_validation_error_response(error: &ValidationError) -> HttpResponse {
    let body = serde_json::json!({ "detail": [error.to_json()] });
    let response = HttpResponse::UnprocessableEntity()
        .content_type("application/json")
        .body(body.to_string());
    crate::error::with_error_info(response, || {
        responses::validation_info(serde_json::json!([error.to_json()]))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::type_coercion::{TYPE_BOOL, TYPE_INT};
    use actix_web::body::MessageBody;
    use proptest::prelude::*;

    proptest! {
        #[test]
        fn header_str_accepts_what_to_str_accepts(
            bytes in prop::collection::vec(
                prop_oneof![8 => 0x20u8..0x7f, 1 => Just(b'\t'), 1 => 0x80u8..=0xff],
                0..300,
            ),
        ) {
            let value = HeaderValue::from_bytes(&bytes).unwrap();
            prop_assert_eq!(header_str(&value), value.to_str().ok());
        }

        // Random high bytes are almost never valid UTF-8, so the test above
        // seldom gives a value that `from_utf8` accepts. This one sends whole
        // characters, also non-ASCII ones.
        #[test]
        fn header_str_rejects_non_ascii_text(text in "[ -~\t\u{80}-\u{10FFFF}]{0,40}") {
            let value = HeaderValue::from_bytes(text.as_bytes()).unwrap();
            prop_assert_eq!(header_str(&value), value.to_str().ok());
        }
    }

    #[test]
    fn header_str_rejects_utf8_text_that_is_not_ascii() {
        let value = HeaderValue::from_bytes("café".as_bytes()).unwrap();
        assert_eq!(header_str(&value), None);
        let value = HeaderValue::from_static("cafe\tbar");
        assert_eq!(header_str(&value), Some("cafe\tbar"));
    }

    #[test]
    fn query_sequences_checks_the_length_of_each_value() {
        let keys: AHashSet<String> = ["tag".to_string()].into_iter().collect();
        let sequences = query_sequences(Some("tag=abc&tag=de"), &keys, 3).unwrap();
        assert_eq!(
            sequences,
            vec![(Cow::from("tag"), vec![Cow::from("abc"), Cow::from("de")])]
        );
        let detail = query_sequences(Some("tag=abcd&tag=a"), &keys, 3)
            .unwrap_err()
            .to_string();
        assert_eq!(
            detail,
            "Query parameter 'tag': Parameter too long: 4 bytes (max 3 bytes)"
        );
        assert!(query_sequences(None, &keys, 3).unwrap().is_empty());
        assert!(query_sequences(Some("tag=abcd"), &Default::default(), 3)
            .unwrap()
            .is_empty());
    }

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
    fn repeated_cookie_lines_join_as_django_joins_them() {
        let request = actix_web::test::TestRequest::default()
            .append_header(("cookie", "a=1"))
            .append_header(("cookie", "b=2"))
            .append_header(("cookie", "c=3"))
            .append_header(("x-tag", "first"))
            .append_header(("x-tag", "last"))
            .to_http_request();
        let headers = extract_headers(&request, 1024).unwrap();
        assert_eq!(headers["cookie"], "a=1; b=2; c=3");
        // Only `Cookie` lines join. Another repeated header keeps its last line.
        assert_eq!(headers["x-tag"], "last");
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
    fn param_value_rejects_bad_typed_value_with_label_and_name() {
        let hints = types(&[("limit", TYPE_INT)]);
        let detail = coerce_param_value("limit", "abc", &hints, 64, "Query parameter").unwrap_err();
        assert!(
            detail.starts_with("Query parameter 'limit': Invalid integer 'abc'"),
            "{detail}"
        );
        assert!(matches!(
            coerce_param_value("limit", "7", &hints, 64, "Query parameter"),
            Ok(Some(CoercedValue::Int(7)))
        ));
        let detail = coerce_param_value("limit", &"1".repeat(65), &hints, 64, "Query parameter")
            .unwrap_err();
        assert_eq!(
            detail,
            "Query parameter 'limit': Parameter too long: 65 bytes (max 64 bytes)"
        );
    }

    #[test]
    fn param_value_checks_length_of_untyped_value() {
        let hints = types(&[]);
        assert!(
            coerce_param_value("q", "short", &hints, 64, "Path parameter")
                .unwrap()
                .is_none()
        );
        let detail =
            coerce_param_value("q", &"a".repeat(65), &hints, 64, "Path parameter").unwrap_err();
        assert_eq!(
            detail,
            "Path parameter 'q': Parameter too long: 65 bytes (max 64 bytes)"
        );
    }

    #[test]
    fn path_and_query_keep_their_labels() {
        let hints = types(&[("id", TYPE_INT)]);
        let none = types(&[]);
        let bad = values(&[("id", "x")]);
        let response =
            validate_and_cache_typed_params(Some(&bad), None, &hints, &none, 64).unwrap_err();
        assert!(detail(response).contains("Path parameter 'id'"));
        let bad_query = crate::router::parse_query_string("id=x");
        let response =
            validate_and_cache_typed_params(None, Some(&bad_query), &none, &hints, 64).unwrap_err();
        assert!(detail(response).contains("Query parameter 'id'"));
    }

    #[test]
    fn a_query_key_with_the_name_of_a_typed_path_parameter_keeps_its_string() {
        let path_hints = types(&[("id", TYPE_INT)]);
        let query_hints = types(&[]);
        let path = values(&[("id", "5")]);
        let query = crate::router::parse_query_string("id=x");
        let (path_coerced, query_coerced) = validate_and_cache_typed_params(
            Some(&path),
            Some(&query),
            &path_hints,
            &query_hints,
            64,
        )
        .unwrap();
        assert!(matches!(path_coerced[..], [("id", CoercedValue::Int(5))]));
        assert!(query_coerced.is_empty());
    }

    #[test]
    fn source_accepts_a_value_of_max_length_in_both_loop_orders() {
        let max = "a".repeat(64);
        // More values than types.
        let cookies = values(&[("a", "1"), ("session", &max)]);
        assert!(
            validate_and_cache_source(&cookies, &types(&[("a", TYPE_INT)]), 64, "Cookie").is_ok()
        );
        // No more values than types.
        let single = values(&[("session", &max)]);
        assert!(
            validate_and_cache_source(&single, &types(&[("a", TYPE_INT)]), 64, "Cookie").is_ok()
        );
    }

    #[test]
    fn param_value_accepts_an_untyped_value_of_max_length() {
        let value = "a".repeat(64);
        assert!(
            coerce_param_value("q", &value, &types(&[]), 64, "Path parameter")
                .unwrap()
                .is_none()
        );
    }

    fn request_with_headers(count: usize) -> HttpRequest {
        (0..count)
            .fold(actix_web::test::TestRequest::default(), |request, i| {
                request.append_header((format!("x-h{i}"), "v"))
            })
            .to_http_request()
    }

    #[test]
    fn a_request_can_have_100_headers_and_not_101() {
        let request = request_with_headers(100);
        assert_eq!(extract_headers(&request, 1024).unwrap().len(), 100);
        assert!(extract_cookie_header(&request, 1024).is_ok());

        let request = request_with_headers(101);
        for response in [
            extract_headers(&request, 1024).unwrap_err(),
            extract_cookie_header(&request, 1024).unwrap_err(),
        ] {
            assert_eq!(response.status(), 400);
            let body = response.into_body().try_into_bytes().unwrap();
            assert_eq!(&body[..], responses::ERROR_BODY_400_HEADERS);
        }
    }

    #[test]
    fn a_header_value_can_have_max_header_size_bytes_and_not_one_more() {
        let max = format!("a={}", "b".repeat(14));
        let request = actix_web::test::TestRequest::default()
            .append_header(("cookie", max.as_str()))
            .append_header(("x-tag", "c".repeat(16)))
            .to_http_request();
        assert_eq!(extract_headers(&request, 16).unwrap()["cookie"], max);
        assert_eq!(
            extract_cookie_header(&request, 16).unwrap().as_deref(),
            Some(max.as_str())
        );

        for over in [("cookie", format!("{max}c")), ("x-tag", "c".repeat(17))] {
            let request = actix_web::test::TestRequest::default()
                .append_header(over)
                .to_http_request();
            assert_eq!(extract_headers(&request, 16).unwrap_err().status(), 400);
            assert_eq!(
                extract_cookie_header(&request, 16).unwrap_err().status(),
                400
            );
        }
    }
}
