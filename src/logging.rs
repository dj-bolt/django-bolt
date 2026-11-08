//! The backend of the `log` crate: one line on stderr for each record.
//!
//! The crates of Bolt log with `log::error!`, `log::warn!` and `log::debug!`.
//! Without a backend, the `log` crate drops each record.
//!
//! The level comes from the `django_bolt` logger of Python, read once when the
//! server or a test app starts. Python does not see the records. A record of a
//! lower level costs one atomic load in the `log` macro and is not formatted.

use std::io::Write;

use log::{Level, LevelFilter, Log, Metadata, Record};
use pyo3::intern;
use pyo3::prelude::*;

/// Python logger whose effective level sets the level of the Rust records.
const PYTHON_LOGGER: &str = "django_bolt";

struct StderrLogger;

static LOGGER: StderrLogger = StderrLogger;

impl Log for StderrLogger {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format_record(record.level(), record.target(), record.args());
        // One write for the line, so that lines of different threads and
        // worker processes do not mix. A failed write cannot be logged.
        let _ = std::io::stderr().lock().write_all(line.as_bytes());
    }

    fn flush(&self) {
        let _ = std::io::stderr().flush();
    }
}

/// Python's name of a level, so that a Rust line reads like a Python one.
fn level_name(level: Level) -> &'static str {
    match level {
        Level::Error => "ERROR",
        Level::Warn => "WARNING",
        Level::Info => "INFO",
        Level::Debug => "DEBUG",
        Level::Trace => "TRACE",
    }
}

fn format_record(level: Level, target: &str, args: &std::fmt::Arguments<'_>) -> String {
    format!("[django-bolt] {} {}: {}\n", level_name(level), target, args)
}

/// The Rust level filter for an effective level of a Python logger.
///
/// Python logs a record when its level is at least the logger level. Rust has
/// no CRITICAL, so a Python level above ERROR (40) turns the Rust records off.
fn level_filter(python_level: i64) -> LevelFilter {
    match python_level {
        ..=5 => LevelFilter::Trace,
        6..=10 => LevelFilter::Debug,
        11..=20 => LevelFilter::Info,
        21..=30 => LevelFilter::Warn,
        31..=40 => LevelFilter::Error,
        _ => LevelFilter::Off,
    }
}

/// Install the backend, and set its level from the `django_bolt` logger.
///
/// Call it at startup, after Django configured logging. A second call only
/// sets the level again, so a test app follows the current Python level.
pub fn install(py: Python<'_>) -> PyResult<()> {
    let python_level: i64 = py
        .import(intern!(py, "logging"))?
        .call_method1(intern!(py, "getLogger"), (PYTHON_LOGGER,))?
        .call_method0(intern!(py, "getEffectiveLevel"))?
        .extract()?;
    // Only the first call sets the logger. Later calls get an error here, and
    // the logger stays the same.
    let _ = log::set_logger(&LOGGER);
    log::set_max_level(level_filter(python_level));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{format_record, level_filter};
    use log::{Level, LevelFilter};

    #[test]
    fn python_levels_map_to_the_rust_filter_that_logs_the_same_records() {
        let cases = [
            (0, LevelFilter::Trace), // NOTSET on the root logger
            (5, LevelFilter::Trace),
            (10, LevelFilter::Debug), // DEBUG
            (15, LevelFilter::Info),  // between DEBUG and INFO: DEBUG records drop
            (20, LevelFilter::Info),  // INFO
            (25, LevelFilter::Warn),  // between INFO and WARNING: INFO records drop
            (30, LevelFilter::Warn),  // WARNING
            (40, LevelFilter::Error), // ERROR
            (41, LevelFilter::Off),   // above ERROR: ERROR records drop
            (50, LevelFilter::Off),   // CRITICAL
        ];
        for (python_level, filter) in cases {
            assert_eq!(
                level_filter(python_level),
                filter,
                "Python level {python_level}"
            );
        }
    }

    #[test]
    fn a_record_is_one_line_with_the_python_level_name_and_the_target() {
        assert_eq!(
            format_record(
                Level::Warn,
                "bolt_core::lane",
                &format_args!("lane {} failed", 3)
            ),
            "[django-bolt] WARNING bolt_core::lane: lane 3 failed\n"
        );
    }
}
