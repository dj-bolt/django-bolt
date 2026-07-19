//! Process-lived asyncio facade for worker-local HTTP dispatch.
//!
//! Python Tasks and Futures use one stable loop identity.  A Tokio pump owns
//! the ready queue independently of request futures, so detached tasks keep
//! running after their originating response has completed.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
#[cfg(unix)]
use std::collections::HashMap;

use crate::request::PyRequest;
use crate::state::TASK_LOCALS;

static WORKER_LOOP_FACTORY: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_DISPATCH_START: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_SYNC_DISPATCH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static RUN_WORKER_HANDLE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_SERVICE: std::sync::OnceLock<WorkerLoopService> = std::sync::OnceLock::new();
static WORKER_LOOP_ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();

#[cfg(unix)]
static SIGNAL_WATCHERS: std::sync::LazyLock<
    std::sync::Mutex<HashMap<i32, tokio::sync::oneshot::Sender<()>>>,
> = std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));

#[inline]
pub(crate) fn enabled() -> bool {
    *WORKER_LOOP_ENABLED.get_or_init(|| {
        std::env::var("DJANGO_BOLT_WORKER_LOOP")
            .map(|value| value != "0" && !value.eq_ignore_ascii_case("false"))
            .unwrap_or(true)
    })
}

enum WorkerLoopCommand {
    Soon(Py<PyAny>),
}

struct WorkerLoopService {
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
}

impl WorkerLoopService {
    fn get() -> &'static Self {
        WORKER_SERVICE.get_or_init(|| {
            let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
            tokio::spawn(run_ready_queue(rx));
            Self { tx }
        })
    }
}

fn get_worker_fn<'py>(
    py: Python<'py>,
    slot: &'static PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<&'static Py<PyAny>> {
    slot.get_or_try_init(py, || {
        Ok(py
            .import("django_bolt._worker_loop")?
            .getattr(name)?
            .unbind())
    })
}

fn get_loop(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    WORKER_LOOP.get_or_try_init(py, || {
        let locals = TASK_LOCALS.get().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized")
        })?;
        let scheduler = Py::new(
            py,
            WorkerLoopScheduler {
                tx: WorkerLoopService::get().tx.clone(),
            },
        )?;
        let factory = get_worker_fn(py, &WORKER_LOOP_FACTORY, "make_worker_loop")?;
        factory
            .call1(py, (scheduler, locals.event_loop(py)))
            .map(Into::into)
    })
}

async fn run_ready_queue(mut rx: tokio::sync::mpsc::UnboundedReceiver<WorkerLoopCommand>) {
    while let Some(WorkerLoopCommand::Soon(first)) = rx.recv().await {
        Python::attach(|py| {
            let Ok(loop_obj) = get_loop(py) else {
                return;
            };
            let Ok(run) = get_worker_fn(py, &RUN_WORKER_HANDLE, "run_worker_handle") else {
                return;
            };

            // Future chaining commonly produces several immediately-ready
            // handles. Drain them under one GIL attachment.
            let mut next = Some(first);
            loop {
                if let Some(handle) = next.take() {
                    if let Err(error) = run.call1(py, (loop_obj, handle)) {
                        error.print(py);
                    }
                }
                match rx.try_recv() {
                    Ok(WorkerLoopCommand::Soon(handle)) => next = Some(handle),
                    Err(_) => break,
                }
            }
        });
    }
}

struct WorkerTimer {
    deadline: std::time::Instant,
    sequence: u64,
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    handle: Py<PyAny>,
}

impl PartialEq for WorkerTimer {
    fn eq(&self, other: &Self) -> bool {
        self.deadline == other.deadline && self.sequence == other.sequence
    }
}

impl Eq for WorkerTimer {}

impl PartialOrd for WorkerTimer {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for WorkerTimer {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // BinaryHeap is a max-heap; reverse the deadline ordering.
        other
            .deadline
            .cmp(&self.deadline)
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

enum TimerCommand {
    Schedule(WorkerTimer),
    Cancelled,
}

static WORKER_TIMER_TX: std::sync::OnceLock<std::sync::mpsc::Sender<TimerCommand>> =
    std::sync::OnceLock::new();
static WORKER_TIMER_SEQUENCE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static WORKER_TIMER_LIVE: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// Cancellation notices accumulate lazily; only rebuild the heap once enough
/// pile up to matter. Mirrors asyncio's cancelled-timer compaction heuristic.
const TIMER_COMPACTION_MIN_CANCELLED: usize = 64;

/// Live (not yet fired or compacted) timers in the worker timer heap.
/// Test/introspection hook: without compaction, cancelled long-delay timers
/// (e.g. completed `asyncio.wait_for` deadlines) would be counted here until
/// their deadline passes.
#[pyfunction]
pub(crate) fn worker_timer_count() -> usize {
    WORKER_TIMER_LIVE.load(std::sync::atomic::Ordering::Relaxed)
}

fn worker_timer_tx() -> &'static std::sync::mpsc::Sender<TimerCommand> {
    WORKER_TIMER_TX.get_or_init(|| {
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::Builder::new()
            .name("bolt-worker-timer".to_string())
            .spawn(move || run_worker_timers(rx))
            .expect("failed to start worker timer thread");
        tx
    })
}

/// Drop cancelled timers instead of holding their handles (and captured
/// arguments) until distant deadlines. Requires the GIL to read each
/// handle's cancelled bit; runs rarely (see the compaction heuristic).
fn compact_cancelled_timers(timers: &mut std::collections::BinaryHeap<WorkerTimer>) {
    Python::attach(|py| {
        let retained: Vec<WorkerTimer> = timers
            .drain()
            .filter(|timer| {
                // On any error reading the bit, keep the timer: firing a
                // cancelled handle is safe (run_worker_handle re-checks).
                !timer
                    .handle
                    .call_method0(py, "cancelled")
                    .and_then(|value| value.extract::<bool>(py))
                    .unwrap_or(false)
            })
            .collect();
        *timers = retained.into();
    });
}

fn run_worker_timers(rx: std::sync::mpsc::Receiver<TimerCommand>) {
    let mut timers = std::collections::BinaryHeap::<WorkerTimer>::new();
    let mut cancelled_notices: usize = 0;
    loop {
        while let Some(timer) = timers.peek() {
            if timer.deadline > std::time::Instant::now() {
                break;
            }
            let timer = timers.pop().expect("timer heap was non-empty");
            let _ = timer.tx.send(WorkerLoopCommand::Soon(timer.handle));
        }

        if cancelled_notices >= TIMER_COMPACTION_MIN_CANCELLED
            && cancelled_notices * 2 >= timers.len()
        {
            compact_cancelled_timers(&mut timers);
            cancelled_notices = 0;
        }
        WORKER_TIMER_LIVE.store(timers.len(), std::sync::atomic::Ordering::Relaxed);

        let command = match timers.peek() {
            Some(timer) => match rx.recv_timeout(
                timer
                    .deadline
                    .saturating_duration_since(std::time::Instant::now()),
            ) {
                Ok(command) => Some(command),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => None,
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return,
            },
            None => match rx.recv() {
                Ok(command) => Some(command),
                Err(_) => return,
            },
        };
        match command {
            Some(TimerCommand::Schedule(timer)) => timers.push(timer),
            Some(TimerCommand::Cancelled) => cancelled_notices += 1,
            None => {}
        }
    }
}

#[pyclass(frozen)]
pub(crate) struct WorkerLoopScheduler {
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
}

#[pymethods]
impl WorkerLoopScheduler {
    fn call_soon(&self, handle: Py<PyAny>) -> PyResult<()> {
        self.tx.send(WorkerLoopCommand::Soon(handle)).map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("worker asyncio loop is unavailable")
        })
    }

    fn call_later(&self, delay: f64, handle: Py<PyAny>) -> PyResult<()> {
        if !delay.is_finite() || delay < 0.0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "timer delay must be a finite non-negative number",
            ));
        }
        // try_from_secs_f64: from_secs_f64 PANICS on Duration overflow (e.g.
        // 1e300), which aborts the process in release wheels (panic = "abort").
        let duration = std::time::Duration::try_from_secs_f64(delay).map_err(|_| {
            pyo3::exceptions::PyOverflowError::new_err("timer delay is too large")
        })?;
        let deadline = std::time::Instant::now()
            .checked_add(duration)
            .ok_or_else(|| {
                pyo3::exceptions::PyOverflowError::new_err("timer delay is too large")
            })?;
        worker_timer_tx()
            .send(TimerCommand::Schedule(WorkerTimer {
                deadline,
                sequence: WORKER_TIMER_SEQUENCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
                tx: self.tx.clone(),
                handle,
            }))
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("worker timer thread is unavailable")
            })
    }

    fn timer_cancelled(&self) {
        // No timer thread yet means nothing was ever scheduled — nothing to
        // compact. Never blocks: this runs with the GIL held.
        if let Some(tx) = WORKER_TIMER_TX.get() {
            let _ = tx.send(TimerCommand::Cancelled);
        }
    }

    #[cfg(unix)]
    fn add_signal_handler(&self, sig: i32, schedule: Py<PyAny>) -> PyResult<()> {
        let mut stream = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::from_raw(
            sig,
        ))
        .map_err(|error| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "invalid or unsupported signal {sig}: {error}"
            ))
        })?;
        let (cancel_tx, mut cancel_rx) = tokio::sync::oneshot::channel();
        if let Some(previous) = SIGNAL_WATCHERS.lock().unwrap().insert(sig, cancel_tx) {
            let _ = previous.send(());
        }
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    biased;
                    _ = &mut cancel_rx => break,
                    received = stream.recv() => {
                        if received.is_none() {
                            break;
                        }
                        Python::attach(|py| {
                            if let Err(error) = schedule.call0(py) {
                                error.print(py);
                            }
                        });
                    }
                }
            }
        });
        Ok(())
    }

    #[cfg(not(unix))]
    fn add_signal_handler(&self, _sig: i32, _schedule: Py<PyAny>) -> PyResult<()> {
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "WorkerLoop signal handlers require Unix",
        ))
    }

    #[cfg(unix)]
    fn remove_signal_handler(&self, sig: i32) -> bool {
        SIGNAL_WATCHERS
            .lock()
            .unwrap()
            .remove(&sig)
            .map(|cancel| cancel.send(()).is_ok())
            .unwrap_or(false)
    }

    #[cfg(not(unix))]
    fn remove_signal_handler(&self, _sig: i32) -> bool {
        false
    }
}

#[pyclass(frozen)]
struct WorkerDispatchResolver {
    tx: std::sync::Mutex<Option<tokio::sync::oneshot::Sender<PyResult<Py<PyAny>>>>>,
}

#[pymethods]
impl WorkerDispatchResolver {
    fn set_result(&self, value: Py<PyAny>) {
        if let Some(tx) = self.tx.lock().unwrap().take() {
            let _ = tx.send(Ok(value));
        }
    }

    fn set_exception(&self, exc: Bound<'_, PyAny>) {
        if let Some(tx) = self.tx.lock().unwrap().take() {
            let _ = tx.send(Err(PyErr::from_value(exc)));
        }
    }
}

pub(crate) async fn dispatch(dispatch: Py<PyAny>, request: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    Python::attach(|py| {
        let resolver = Py::new(
            py,
            WorkerDispatchResolver {
                tx: std::sync::Mutex::new(Some(tx)),
            },
        )?;
        let start = get_worker_fn(py, &WORKER_DISPATCH_START, "worker_dispatch_start")?;
        start.call1(py, (dispatch, request, get_loop(py)?, resolver))?;
        Ok::<_, PyErr>(())
    })?;

    match rx.await {
        Ok(result) => result,
        Err(_) => Err(pyo3::exceptions::PyRuntimeError::new_err(
            "worker dispatch resolver dropped without a result",
        )),
    }
}

pub(crate) fn dispatch_sync(
    py: Python<'_>,
    dispatch: &Py<PyAny>,
    request: &Py<PyRequest>,
) -> PyResult<Py<PyAny>> {
    let call = get_worker_fn(py, &WORKER_SYNC_DISPATCH, "worker_sync_dispatch")?;
    call.call1(py, (dispatch, request, get_loop(py)?))
}
