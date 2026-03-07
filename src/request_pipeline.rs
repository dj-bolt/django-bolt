//! Shared request pipeline logic for production and test handlers.
//!
//! This module contains validation and processing logic that is common
//! between the production handler (handler.rs) and test handler (testing.rs).

use actix_web::HttpResponse;
use ahash::AHashMap;
use std::collections::HashMap;

use crate::responses;
use crate::type_coercion::{coerce_param, CoercedValue, MAX_PARAM_LENGTH, TYPE_STRING};

/// Validate and pre-coerce path/query parameters against type hints.
///
/// Returns a pair of maps containing only non-string pre-coerced values, keyed by
/// parameter name. String parameters are validated for length but left as-is.
pub fn validate_and_cache_typed_params(
    path_params: Option<&AHashMap<String, String>>,
    query_params: Option<&AHashMap<String, String>>,
    param_types: &HashMap<String, u8>,
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
            if value.len() > MAX_PARAM_LENGTH {
                return Err(responses::error_422_validation(&format!(
                    "Path parameter '{}': Parameter too long: {} bytes (max {} bytes)",
                    name,
                    value.len(),
                    MAX_PARAM_LENGTH
                )));
            }

            // Type validation for non-string types
            if let Some(&type_hint) = param_types.get(name) {
                if type_hint != TYPE_STRING {
                    match coerce_param(value, type_hint) {
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
            if value.len() > MAX_PARAM_LENGTH {
                return Err(responses::error_422_validation(&format!(
                    "Query parameter '{}': Parameter too long: {} bytes (max {} bytes)",
                    name,
                    value.len(),
                    MAX_PARAM_LENGTH
                )));
            }

            // Type validation for non-string types
            if let Some(&type_hint) = param_types.get(name) {
                if type_hint != TYPE_STRING {
                    match coerce_param(value, type_hint) {
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
