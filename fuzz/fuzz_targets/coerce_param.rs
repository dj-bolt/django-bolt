//! Coercion of a path, query, header or cookie value never panics.
#![no_main]

use bolt_core::type_coercion::{coerce_param, DEFAULT_MAX_PARAM_LENGTH};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|input: (u8, &str)| {
    let (type_hint, value) = input;
    let _ = coerce_param(value, type_hint % 11, DEFAULT_MAX_PARAM_LENGTH);
});
