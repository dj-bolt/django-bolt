//! Each cookie of a header, quoted again as Django quotes it, reads back unchanged.
#![no_main]

use bolt_core::cookies::{cookie_pairs, quote_cookie_value, unquote_cookie_value};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|header: &str| {
    for (name, value) in cookie_pairs(header) {
        assert!(!name.contains(';'));
        let quoted = quote_cookie_value(&value);
        assert!(!quoted.contains(';'));
        assert_eq!(unquote_cookie_value(&quoted), value);
    }
});
