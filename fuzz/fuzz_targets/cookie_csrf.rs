//! The cookie CSRF check never panics, and a cross-site request never passes.
#![no_main]

use ahash::AHashMap;
use bolt_core::validation::passes_cookie_csrf;
use libfuzzer_sys::fuzz_target;

fuzz_target!(|input: (bool, &str, &str, &str, Option<&str>)| {
    let (https, host, origin, referer, fetch_site) = input;
    let mut headers = AHashMap::new();
    headers.insert("host".to_string(), host.to_string());
    headers.insert("origin".to_string(), origin.to_string());
    headers.insert("referer".to_string(), referer.to_string());
    if let Some(site) = fetch_site {
        headers.insert("sec-fetch-site".to_string(), site.to_string());
    }
    let passed = passes_cookie_csrf("POST", &headers, if https { "https" } else { "http" });
    if fetch_site == Some("cross-site") {
        assert!(!passed);
    }
});
