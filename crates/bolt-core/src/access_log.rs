//! The access-log filter of a route: sampling and the slow-request threshold.
//!
//! Python gives the two values of `LoggingConfig` at route registration. The
//! request path reads them from the route metadata, with no Python call.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::Cell;
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};

/// Route metadata key of `LoggingConfig.sample_rate`.
const SAMPLE_RATE_KEY: &str = "access_log_sample_rate";
/// Route metadata key of `LoggingConfig.min_duration_ms`.
const MIN_DURATION_KEY: &str = "access_log_min_duration_ms";

/// Which 1xx to 3xx responses of a route write an access-log line.
/// A 4xx or a 5xx response always writes its line.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct AccessLogFilter {
    /// The part of the 1xx to 3xx responses that write a line, from 0.0 to 1.0.
    sample_rate: Option<f64>,
    /// A 1xx to 3xx response faster than this many milliseconds writes no line.
    min_duration_ms: Option<f64>,
    /// `false` when the two values set no limit. The request path then makes one check.
    has_limit: bool,
}

impl AccessLogFilter {
    pub fn new(sample_rate: Option<f64>, min_duration_ms: Option<f64>) -> Self {
        Self {
            sample_rate,
            min_duration_ms,
            has_limit: sample_rate.is_some() || min_duration_ms.is_some(),
        }
    }

    /// Read the filter from the metadata dict of a route. A missing key or
    /// `None` sets no limit. A value out of range is a `ValueError`.
    pub fn from_python(py_meta: &Bound<'_, PyDict>) -> PyResult<Self> {
        let number = |key: &str| -> PyResult<Option<f64>> {
            match py_meta.get_item(key)? {
                Some(value) if !value.is_none() => value.extract::<f64>().map(Some),
                _ => Ok(None),
            }
        };
        let sample_rate = number(SAMPLE_RATE_KEY)?;
        if sample_rate.is_some_and(|rate| !(0.0..=1.0).contains(&rate)) {
            return Err(PyValueError::new_err(format!(
                "{SAMPLE_RATE_KEY} must be from 0.0 to 1.0"
            )));
        }
        let min_duration_ms = number(MIN_DURATION_KEY)?;
        if min_duration_ms.is_some_and(|min| min.is_nan() || min < 0.0) {
            return Err(PyValueError::new_err(format!(
                "{MIN_DURATION_KEY} must be 0 or more"
            )));
        }
        Ok(Self::new(sample_rate, min_duration_ms))
    }

    /// `true` when the response writes its access-log line.
    /// Without a limit, the cost is one check.
    #[inline]
    pub fn keeps(&self, status: u16, duration_ms: f64) -> bool {
        self.keeps_with(status, duration_ms, random_unit)
    }

    /// `keeps` with the source of the random number, for tests. The function
    /// calls `random` only for a 1xx to 3xx response of a route with a sample rate.
    #[inline]
    pub fn keeps_with(&self, status: u16, duration_ms: f64, random: impl FnOnce() -> f64) -> bool {
        if !self.has_limit || status >= 400 {
            return true;
        }
        if let Some(rate) = self.sample_rate
            && random() >= rate
        {
            return false;
        }
        if let Some(min) = self.min_duration_ms
            && duration_ms < min
        {
            return false;
        }
        true
    }
}

thread_local! {
    /// State of the sampling generator of this thread. Never 0.
    static SAMPLE_STATE: Cell<u64> = Cell::new(RandomState::new().build_hasher().finish() | 1);
}

/// A number in `[0, 1)` from a xorshift64* generator. Sampling needs an even
/// spread, not an unpredictable value.
fn random_unit() -> f64 {
    SAMPLE_STATE.with(|state| {
        let mut x = state.get();
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        state.set(x);
        // The top 53 bits make a float in [0, 1).
        (x.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 11) as f64 / (1u64 << 53) as f64
    })
}

#[cfg(test)]
mod tests {
    use super::{AccessLogFilter, random_unit};
    use pyo3::prelude::*;
    use pyo3::types::PyDict;

    fn no_random() -> f64 {
        panic!("the filter must not sample this response")
    }

    #[test]
    fn a_filter_with_no_limit_keeps_each_response() {
        let filter = AccessLogFilter::default();
        for status in [200, 204, 308, 404, 500] {
            assert!(filter.keeps_with(status, 0.0, no_random), "status {status}");
        }
    }

    #[test]
    fn the_sample_rate_applies_to_responses_below_400_only() {
        let filter = AccessLogFilter::new(Some(0.25), None);
        assert!(filter.keeps_with(200, 0.0, || 0.0));
        assert!(filter.keeps_with(302, 0.0, || 0.249));
        assert!(!filter.keeps_with(200, 0.0, || 0.25));
        assert!(!filter.keeps_with(399, 0.0, || 0.9));
        // An error is not sampled.
        assert!(filter.keeps_with(400, 0.0, no_random));
        assert!(filter.keeps_with(503, 0.0, no_random));
    }

    #[test]
    fn the_sample_rates_0_and_1_have_no_exception() {
        let none = AccessLogFilter::new(Some(0.0), None);
        let all = AccessLogFilter::new(Some(1.0), None);
        // The generator gives a number in [0, 1).
        for random in [0.0, 0.5, 0.999_999] {
            assert!(!none.keeps_with(200, 0.0, || random));
            assert!(all.keeps_with(200, 0.0, || random));
        }
    }

    #[test]
    fn the_min_duration_applies_to_responses_below_400_only() {
        let filter = AccessLogFilter::new(None, Some(250.0));
        assert!(!filter.keeps_with(200, 249.9, no_random));
        assert!(filter.keeps_with(200, 250.0, no_random));
        assert!(filter.keeps_with(301, 900.0, no_random));
        // A fast error keeps its line.
        assert!(filter.keeps_with(404, 0.1, no_random));
        assert!(filter.keeps_with(500, 0.1, no_random));
    }

    #[test]
    fn a_sampled_response_must_also_be_slow() {
        let filter = AccessLogFilter::new(Some(0.5), Some(100.0));
        assert!(filter.keeps_with(200, 150.0, || 0.1));
        assert!(!filter.keeps_with(200, 50.0, || 0.1));
        assert!(!filter.keeps_with(200, 150.0, || 0.7));
    }

    #[test]
    fn the_generator_gives_an_even_spread_in_the_unit_range() {
        let total = 100_000;
        let mut below = 0;
        for _ in 0..total {
            let value = random_unit();
            assert!((0.0..1.0).contains(&value));
            if value < 0.1 {
                below += 1;
            }
        }
        // 10% of 100000 is 10000. The standard deviation is 95.
        assert!((9_000..11_000).contains(&below), "below 0.1: {below}");
    }

    #[test]
    fn from_python_reads_the_two_keys_and_refuses_a_value_out_of_range() {
        Python::initialize();
        Python::attach(|py| {
            let meta = PyDict::new(py);
            assert_eq!(
                AccessLogFilter::from_python(&meta).unwrap(),
                AccessLogFilter::default()
            );

            meta.set_item("access_log_sample_rate", 0.05).unwrap();
            meta.set_item("access_log_min_duration_ms", 250).unwrap();
            assert_eq!(
                AccessLogFilter::from_python(&meta).unwrap(),
                AccessLogFilter::new(Some(0.05), Some(250.0))
            );

            meta.set_item("access_log_sample_rate", 1.5).unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
            meta.set_item("access_log_sample_rate", py.None()).unwrap();
            meta.set_item("access_log_min_duration_ms", -1).unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
        });
    }
}
