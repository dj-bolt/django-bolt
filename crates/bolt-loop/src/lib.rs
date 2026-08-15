//! `bolt-loop`: an asyncio event loop whose ready queue, timers, Unix signals,
//! and file-descriptor readiness are driven by a Tokio runtime.
//!
//! The Python half is a `asyncio.SelectorEventLoop` subclass (django-bolt ships
//! it as `django_bolt._worker_loop.WorkerLoop`) built on the `WorkerLoopScheduler`
//! pyclass exported here: `call_soon` pushes an asyncio `Handle` onto a Tokio
//! task (the "pump") that runs ready handles under one GIL attachment;
//! positive-delay timers go to a GIL-free high-resolution timer thread; the
//! four fd primitives (`_add_reader`/`_add_writer`/`_remove_reader`/
//! `_remove_writer`) register descriptors with the Tokio reactor (Unix); and
//! [`dispatch`] / [`dispatch_sync`] / [`dispatch_cancellable`] run a Python
//! callable on that loop from Rust and await its result.
//!
//! The host names its Python module once with [`init`] and registers the
//! Python-visible items with [`register`]. The Python module must define
//! `make_worker_loop(scheduler)`, `worker_dispatch_start`,
//! `worker_dispatch_start_cancellable`, `worker_sync_dispatch`, and
//! `worker_handle_failed` (see django-bolt's `_worker_loop.py`).
//!
//! Invariant: the pump task lives in whichever Tokio runtime first touches
//! the loop, while the statics are process-level, so every entry point must
//! share one process-lived runtime.

use pyo3::prelude::*;

mod dispatch;
#[cfg(unix)]
mod fd;
mod scheduler;

pub use dispatch::{dispatch, dispatch_cancellable, dispatch_sync};
pub use scheduler::{get_loop, worker_fd_watcher_count, worker_timer_count, WorkerLoopScheduler};

/// Name of the Python module implementing the loop (imported lazily).
pub fn init(python_module: &'static str) {
    scheduler::set_python_module(python_module);
}

/// Add the scheduler class and introspection hooks to a Python module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WorkerLoopScheduler>()?;
    m.add_function(wrap_pyfunction!(worker_timer_count, m)?)?;
    m.add_function(wrap_pyfunction!(worker_fd_watcher_count, m)?)?;
    Ok(())
}
