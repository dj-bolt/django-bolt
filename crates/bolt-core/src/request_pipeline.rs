//! Shared request pipeline logic for production and test handlers.
//!
//! This module contains validation and processing logic that is common
//! between the production handler (handler.rs) and test handler (testing.rs).

use actix_web::{HttpRequest, HttpResponse};
use ahash::AHashMap;
use std::collections::HashMap;

use crate::form_parsing::ValidationError;
use crate::responses;
use crate::type_coercion::{coerce_param, CoercedValue, TYPE_STRING};

/// Validate and pre-coerce path/query parameters against type hints.
///
/// Returns a pair of maps containing only non-string pre-coerced values, keyed by
/// parameter name. String parameters are validated for length but left as-is.
pub fn validate_and_cache_typed_params(
    path_params: Option<&AHashMap<String, String>>,
    query_params: Option<&AHashMap<String, String>>,
    param_types: &HashMap<String, u8>,
    max_length: usize,
) -> Result<
    (
        Option<AHashMap<String, CoercedValue>>,
        Option<AHashMap<String, CoercedValue>>,
    ),
    HttpResponse,
> {
    let mut path_coerced: Option<AHashMap<String, CoercedValue>> = None;
    let mut query_coerced: Option<AHashMap<String, CoercedValue>> = None;

    // Validate path parameters - always check length, type validation for non-strings
    if let Some(path_params) = path_params {
        for (name, value) in path_params {
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(responses::error_422_validation(&format!(
                    "Path parameter '{}': Parameter too long: {} bytes (max {} bytes)",
                    name,
                    value.len(),
                    max_length
                )));
            }

            // Type validation for non-string types
            if let Some(&type_hint) = param_types.get(name) {
                if type_hint != TYPE_STRING {
                    match coerce_param(value, type_hint, max_length) {
                        Ok(coerced) => {
                            path_coerced
                                .get_or_insert_with(AHashMap::new)
                                .insert(name.clone(), coerced);
                        }
                        Err(error_msg) => {
                            return Err(responses::error_422_validation(&format!(
                                "Path parameter '{}': {}",
                                name, error_msg
                            )));
                        }
                    }
                }
            }
        }
    }

    // Validate query parameters - always check length, type validation for non-strings
    if let Some(query_params) = query_params {
        for (name, value) in query_params {
            // Security: Always validate length for ALL parameters (including strings)
            if value.len() > max_length {
                return Err(responses::error_422_validation(&format!(
                    "Query parameter '{}': Parameter too long: {} bytes (max {} bytes)",
                    name,
                    value.len(),
                    max_length
                )));
            }

            // Type validation for non-string types
            if let Some(&type_hint) = param_types.get(name) {
                if type_hint != TYPE_STRING {
                    match coerce_param(value, type_hint, max_length) {
                        Ok(coerced) => {
                            query_coerced
                                .get_or_insert_with(AHashMap::new)
                                .insert(name.clone(), coerced);
                        }
                        Err(error_msg) => {
                            return Err(responses::error_422_validation(&format!(
                                "Query parameter '{}': {}",
                                name, error_msg
                            )));
                        }
                    }
                }
            }
        }
    }

    Ok((path_coerced, query_coerced))
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
