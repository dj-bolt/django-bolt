//! A urlencoded form body never makes the parser panic.
#![no_main]

use std::collections::HashMap;

use bolt_core::form_parsing::parse_urlencoded;
use bolt_core::type_coercion::DEFAULT_MAX_PARAM_LENGTH;
use libfuzzer_sys::fuzz_target;

fuzz_target!(|body: &[u8]| {
    // One field of each type, with a one-letter name that the fuzzer finds fast.
    let type_hints: HashMap<String, u8> = ('a'..='j')
        .zip(1u8..)
        .map(|(name, type_hint)| (name.to_string(), type_hint))
        .collect();
    let _ = parse_urlencoded(body, &type_hints, DEFAULT_MAX_PARAM_LENGTH);
});
