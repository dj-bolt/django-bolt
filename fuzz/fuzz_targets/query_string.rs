//! The query parser gives what the WHATWG form parser (`serde_urlencoded`)
//! gives. Django `QueryDict` gives the same pairs.
#![no_main]

use ahash::{AHashMap, AHashSet};
use bolt_core::router::{collect_query_sequences, parse_query_string};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|query: &str| {
    let expected: AHashMap<String, String> =
        serde_urlencoded::from_str::<Vec<(String, String)>>(query)
            .expect("a form parser accepts any string")
            .into_iter()
            .collect();
    let actual: AHashMap<String, String> = parse_query_string(query)
        .into_iter()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    assert_eq!(actual, expected);

    // A sequence key ends with the value that the map keeps.
    let keys: AHashSet<String> = actual.keys().cloned().collect();
    for (key, values) in collect_query_sequences(query, &keys) {
        assert_eq!(
            values.last().map(|value| value.as_ref()),
            actual.get(key.as_ref()).map(String::as_str)
        );
    }
});
