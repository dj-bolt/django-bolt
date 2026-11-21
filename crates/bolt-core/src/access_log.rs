//! The access-log filter of a route: its logger, sampling, the slow-request
//! threshold, and the paths and status codes that write no line.
//!
//! Python gives the values of `LoggingConfig` at route registration. The
//! request path reads them from the route metadata, with no Python call.

use ahash::AHashSet;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::{Cell, RefCell};
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::time::Instant;

/// Route metadata key of `LoggingConfig.sample_rate`.
const SAMPLE_RATE_KEY: &str = "access_log_sample_rate";
/// Route metadata key of `LoggingConfig.min_duration_ms`.
const MIN_DURATION_KEY: &str = "access_log_min_duration_ms";
/// Route metadata key of `LoggingConfig.skip_paths`.
const SKIP_PATHS_KEY: &str = "access_log_skip_paths";
/// Route metadata key of `LoggingConfig.skip_status_codes`.
const SKIP_STATUS_CODES_KEY: &str = "access_log_skip_status_codes";
/// Route metadata key of `LoggingConfig.logger_name`.
const LOGGER_NAME_KEY: &str = "access_log_logger_name";
/// The logger of a route with no `logger_name`.
pub const DEFAULT_LOGGER_NAME: &str = "django.server";

/// A Python logger of the access log and its levels, read once at startup.
#[derive(Debug)]
pub struct AccessLogger {
    /// The INFO, WARNING and ERROR bits that the logger writes. 0 writes no line.
    pub levels: u8,
    pub logger: Py<PyAny>,
}

impl AccessLogger {
    /// Get the logger `name` and read the levels that it writes.
    pub fn read(py: Python<'_>, name: &str) -> PyResult<Self> {
        let logger = py.import("logging")?.call_method1("getLogger", (name,))?;
        let mut levels = 0;
        for (bit, level) in [(1, 20), (2, 30), (4, 40)] {
            if logger
                .call_method1("isEnabledFor", (level,))?
                .extract::<bool>()?
            {
                levels |= bit;
            }
        }
        Ok(Self {
            levels,
            logger: logger.unbind(),
        })
    }
}

/// Read the logger of each filter once. Each filter gets the index of its
/// logger in the result. Index 0 is `django.server`, the logger of a route
/// with no `logger_name` and of a route with no metadata.
pub fn read_access_loggers<'a>(
    py: Python<'_>,
    filters: impl IntoIterator<Item = &'a mut AccessLogFilter>,
) -> PyResult<Vec<AccessLogger>> {
    let mut names = vec![DEFAULT_LOGGER_NAME.to_string()];
    let mut loggers = vec![AccessLogger::read(py, DEFAULT_LOGGER_NAME)?];
    for filter in filters {
        let Some(name) = filter.logger_name.as_deref() else {
            continue;
        };
        filter.logger = match names.iter().position(|known| known == name) {
            Some(index) => index,
            None => {
                loggers.push(AccessLogger::read(py, name)?);
                names.push(name.to_string());
                names.len() - 1
            }
        };
    }
    Ok(loggers)
}

/// Which responses of a route write an access-log line.
/// The sample rate and the threshold apply to a 1xx to 3xx response only.
/// A skipped path or status code writes no line for each status class.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AccessLogFilter {
    /// The part of the 1xx to 3xx responses that write a line, from 0.0 to 1.0.
    sample_rate: Option<f64>,
    /// A 1xx to 3xx response faster than this many milliseconds writes no line.
    min_duration_ms: Option<f64>,
    /// `false` when the two values set no limit. The request path then makes one check.
    has_limit: bool,
    /// A request with one of these paths writes no line. The match is exact.
    skip_paths: AHashSet<String>,
    /// A response with one of these status codes writes no line.
    skip_status_codes: Vec<u16>,
    /// `LoggingConfig.logger_name`. `None` is `django.server`.
    logger_name: Option<String>,
    /// The index of the logger in `AppState::access_loggers`. Startup sets it.
    pub logger: usize,
}

impl AccessLogFilter {
    pub fn new(sample_rate: Option<f64>, min_duration_ms: Option<f64>) -> Self {
        Self {
            sample_rate,
            min_duration_ms,
            has_limit: sample_rate.is_some() || min_duration_ms.is_some(),
            skip_paths: AHashSet::new(),
            skip_status_codes: Vec::new(),
            logger_name: None,
            logger: 0,
        }
    }

    /// Write the lines to the logger `name` in place of `django.server`.
    pub fn with_logger_name(mut self, name: impl Into<String>) -> Self {
        self.logger_name = Some(name.into());
        self
    }

    /// Add the request paths and the status codes that write no line.
    pub fn with_skips(
        mut self,
        paths: impl IntoIterator<Item = String>,
        status_codes: impl IntoIterator<Item = u16>,
    ) -> Self {
        self.skip_paths = paths.into_iter().collect();
        self.skip_status_codes = status_codes.into_iter().collect();
        self
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
        let skip_paths = match py_meta.get_item(SKIP_PATHS_KEY)? {
            Some(value) if !value.is_none() => value.extract::<Vec<String>>()?,
            _ => Vec::new(),
        };
        let skip_status_codes = match py_meta.get_item(SKIP_STATUS_CODES_KEY)? {
            Some(value) if !value.is_none() => value.extract::<Vec<u16>>()?,
            _ => Vec::new(),
        };
        let filter =
            Self::new(sample_rate, min_duration_ms).with_skips(skip_paths, skip_status_codes);
        match py_meta.get_item(LOGGER_NAME_KEY)? {
            Some(value) if !value.is_none() => {
                Ok(filter.with_logger_name(value.extract::<String>()?))
            }
            _ => Ok(filter),
        }
    }

    /// `true` when the config of the route skips this request path or this
    /// status code. With no skip value, the cost is two length checks.
    #[inline]
    pub fn skips(&self, path: &str, status: u16) -> bool {
        self.skip_status_codes.contains(&status)
            || (!self.skip_paths.is_empty() && self.skip_paths.contains(path))
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

/// The limit of the access-log lines of 429 responses: one line for each second.
/// The rate limiter has the same limit for its own line on stderr.
#[derive(Debug, Default, PartialEq)]
pub struct RejectionLineLimit {
    /// The second of the last line plus one, or 0 before the first line.
    after_line: u64,
    /// The 429 responses since the last line that wrote none.
    suppressed: u64,
}

impl RejectionLineLimit {
    /// Decide if a 429 response in `second` writes its line. The first one of
    /// each second does. The result is the number of 429 responses before it
    /// that wrote no line, or `None` when this response writes no line. The
    /// stored second never goes back, so a late response of an earlier second
    /// only adds to the count.
    pub fn line_in(&mut self, second: u64) -> Option<u64> {
        if second < self.after_line {
            self.suppressed += 1;
            return None;
        }
        self.after_line = second + 1;
        Some(std::mem::take(&mut self.suppressed))
    }
}

thread_local! {
    /// The 429 line limit of this worker thread, and the time of its first 429 response.
    static REJECTION_LINES: RefCell<Option<(Instant, RejectionLineLimit)>> =
        const { RefCell::new(None) };
}

/// Decide if a 429 response at `now` writes its access-log line on this
/// worker thread. See `RejectionLineLimit::line_in` for the result.
pub fn rejection_line_at(now: Instant) -> Option<u64> {
    REJECTION_LINES.with_borrow_mut(|state| {
        let (first, limit) = state.get_or_insert_with(|| (now, RejectionLineLimit::default()));
        limit.line_in(now.saturating_duration_since(*first).as_secs())
    })
}

#[cfg(test)]
mod tests {
    use super::{
        AccessLogFilter, RejectionLineLimit, random_unit, read_access_loggers, rejection_line_at,
    };
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    use std::time::{Duration, Instant};

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
    fn a_skipped_path_matches_the_complete_request_path() {
        let filter = AccessLogFilter::default().with_skips(["/health".to_string()], []);
        assert!(filter.skips("/health", 200));
        // A skipped path writes no line for an error.
        assert!(filter.skips("/health", 503));
        for path in ["/health/", "/healthy", "/api/health", "/", ""] {
            assert!(!filter.skips(path, 200), "path {path}");
        }
    }

    #[test]
    fn a_skipped_status_code_applies_to_each_path() {
        let filter = AccessLogFilter::default().with_skips([], [204, 404]);
        assert!(filter.skips("/items", 204));
        assert!(filter.skips("/gone", 404));
        for status in [200, 205, 304, 403, 500] {
            assert!(!filter.skips("/items", status), "status {status}");
        }
    }

    #[test]
    fn a_filter_with_no_skip_value_skips_nothing() {
        let filter = AccessLogFilter::new(Some(0.0), Some(250.0));
        assert!(!filter.skips("/health", 200));
        assert!(!filter.skips("", 0));
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
    fn from_python_reads_the_keys_and_refuses_a_value_out_of_range() {
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

            meta.set_item("access_log_skip_paths", ("/health", "/ready"))
                .unwrap();
            meta.set_item("access_log_skip_status_codes", (204, 304))
                .unwrap();
            assert_eq!(
                AccessLogFilter::from_python(&meta).unwrap(),
                AccessLogFilter::new(Some(0.05), Some(250.0))
                    .with_skips(["/ready".to_string(), "/health".to_string()], [204, 304])
            );
            // A status code that is not a number from 0 to 65535 is an error.
            meta.set_item("access_log_skip_status_codes", (-1,))
                .unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
            meta.del_item("access_log_skip_status_codes").unwrap();

            meta.set_item("access_log_logger_name", "myapp.api")
                .unwrap();
            assert_eq!(
                AccessLogFilter::from_python(&meta).unwrap(),
                AccessLogFilter::new(Some(0.05), Some(250.0))
                    .with_skips(["/ready".to_string(), "/health".to_string()], [])
                    .with_logger_name("myapp.api")
            );
            meta.set_item("access_log_logger_name", 7).unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
            meta.del_item("access_log_logger_name").unwrap();

            meta.set_item("access_log_sample_rate", 1.5).unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
            meta.set_item("access_log_sample_rate", py.None()).unwrap();
            meta.set_item("access_log_min_duration_ms", -1).unwrap();
            assert!(AccessLogFilter::from_python(&meta).is_err());
        });
    }

    #[test]
    fn each_logger_name_is_read_once_and_index_0_is_django_server() {
        Python::initialize();
        Python::attach(|py| {
            let logging = py.import("logging").unwrap();
            logging
                .call_method1("getLogger", ("access_log_test.quiet",))
                .unwrap()
                .call_method1("setLevel", (30,))
                .unwrap();
            let mut filters = [
                AccessLogFilter::default().with_logger_name("access_log_test.quiet"),
                AccessLogFilter::default(),
                AccessLogFilter::default().with_logger_name("access_log_test.other"),
                AccessLogFilter::default().with_logger_name("access_log_test.quiet"),
                AccessLogFilter::default().with_logger_name("django.server"),
            ];
            let loggers = read_access_loggers(py, filters.iter_mut()).unwrap();
            let indexes: Vec<usize> = filters.iter().map(|filter| filter.logger).collect();
            assert_eq!(indexes, [1, 0, 2, 1, 0]);
            assert_eq!(loggers.len(), 3);
            let name = |index: usize| -> String {
                loggers[index]
                    .logger
                    .bind(py)
                    .getattr("name")
                    .unwrap()
                    .extract()
                    .unwrap()
            };
            assert_eq!(name(0), "django.server");
            assert_eq!(name(1), "access_log_test.quiet");
            // A logger at WARNING writes the WARNING and the ERROR lines only.
            assert_eq!(loggers[1].levels, 2 | 4);
        });
    }

    #[test]
    fn the_first_429_of_each_second_writes_a_line_with_the_count_before_it() {
        let mut limit = RejectionLineLimit::default();
        assert_eq!(limit.line_in(0), Some(0));
        assert_eq!(limit.line_in(0), None);
        assert_eq!(limit.line_in(0), None);
        assert_eq!(limit.line_in(1), Some(2));
        // A second with no 429 response between two lines changes nothing.
        assert_eq!(limit.line_in(5), Some(0));
        assert_eq!(limit.line_in(5), None);
        // A late response of an earlier second writes no line. The count has it.
        assert_eq!(limit.line_in(3), None);
        assert_eq!(limit.line_in(6), Some(2));
    }

    #[test]
    fn each_thread_counts_its_seconds_from_its_first_429() {
        std::thread::spawn(|| {
            let start = Instant::now();
            assert_eq!(rejection_line_at(start), Some(0));
            assert_eq!(rejection_line_at(start + Duration::from_millis(999)), None);
            assert_eq!(
                rejection_line_at(start + Duration::from_millis(1000)),
                Some(1)
            );
            // A time before the first response is in second 0.
            assert_eq!(rejection_line_at(start - Duration::from_secs(9)), None);
            assert_eq!(rejection_line_at(start + Duration::from_secs(2)), Some(1));
        })
        .join()
        .unwrap();
        // The state of the other thread is not here.
        std::thread::spawn(|| assert_eq!(rejection_line_at(Instant::now()), Some(0)))
            .join()
            .unwrap();
    }
}
