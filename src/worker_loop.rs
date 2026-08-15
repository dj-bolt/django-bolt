//! Process-lived asyncio facade for worker-local HTTP dispatch.
//!
//! Python Tasks and Futures use one stable loop identity.  A Tokio pump owns
//! the ready queue independently of request futures, so detached tasks keep
//! running after their originating response has completed.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
#[cfg(unix)]
use std::collections::HashMap;

use crate::request::PyRequest;
use crate::state::TASK_LOCALS;

static WORKER_LOOP_FACTORY: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_DISPATCH_START: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_DISPATCH_START_CANCELLABLE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_SYNC_DISPATCH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_HANDLE_FAILED: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_SERVICE: std::sync::OnceLock<WorkerLoopService> = std::sync::OnceLock::new();

#[cfg(unix)]
static SIGNAL_WATCHERS: std::sync::LazyLock<
    std::sync::Mutex<HashMap<i32, tokio::sync::oneshot::Sender<()>>>,
> = std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));

pub(crate) enum WorkerLoopCommand {
    Soon(Py<PyAny>),
    /// File-descriptor readiness (see `worker_fd`): run the handle, then
    /// either requeue while the descriptor stays ready, tell the watcher to
    /// re-arm, or drop it when the handle was cancelled.
    #[cfg(unix)]
    Ready(crate::worker_fd::Ready),
}

struct WorkerLoopService {
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    #[cfg(unix)]
    runtime: tokio::runtime::Handle,
    #[cfg(unix)]
    fd_watchers: crate::worker_fd::FdWatchers,
}

impl WorkerLoopService {
    /// Invariant: the pump task lives in whichever Tokio runtime first calls
    /// this, but the statics are process-level. Every entry point (server and
    /// TestClient) must therefore share one process-lived runtime — if that
    /// runtime were torn down, every later `call_soon` would fail with
    /// "worker asyncio loop is unavailable" with no recovery path.
    fn get() -> &'static Self {
        WORKER_SERVICE.get_or_init(|| {
            let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
            tokio::spawn(run_ready_queue(tx.clone(), rx));
            Self {
                tx,
                #[cfg(unix)]
                runtime: tokio::runtime::Handle::current(),
                #[cfg(unix)]
                fd_watchers: crate::worker_fd::FdWatchers::default(),
            }
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

/// The process-lived WorkerLoop. Every Python coroutine belonging to a Bolt
/// route — HTTP dispatch, streaming response generators, WebSocket handlers —
/// must run here, so that futures, queues, and locks shared between handlers
/// always live on one loop. A coroutine driven on the selector loop instead
/// would deadlock the moment a WorkerLoop-dispatched handler resolves one of
/// its futures (asyncio wakes waiters via a same-loop `call_soon`).
///
/// Rust outside this module reaches the loop through `worker_task_locals`.
/// The one deliberate exception is `asgi_http::submit_to_event_loop`: mounted
/// ASGI apps stay on the startup/selector loop (`state::TASK_LOCALS`), so they
/// cannot share asyncio primitives with Bolt handlers.
fn get_loop(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    WORKER_LOOP.get_or_try_init(py, || {
        let scheduler = Py::new(
            py,
            WorkerLoopScheduler {
                tx: WorkerLoopService::get().tx.clone(),
            },
        )?;
        let factory = get_worker_fn(py, &WORKER_LOOP_FACTORY, "make_worker_loop")?;
        factory.call1(py, (scheduler,)).map(Into::into)
    })
}

/// TaskLocals bound to the WorkerLoop, for Rust code that schedules Python
/// coroutines or resolves Python futures on the HTTP dispatch loop
/// (e.g. the streaming forwarder and its backpressure futures).
///
/// The context is the startup one, not a fresh `copy_context()`: this snapshot
/// is initialized lazily from whichever request first streams or upgrades, and
/// is then inherited by every stream/WebSocket task for the process lifetime —
/// so copying here would pin that request's contextvars forever.
pub(crate) fn worker_task_locals(
    py: Python<'_>,
) -> PyResult<&'static pyo3_async_runtimes::TaskLocals> {
    static WORKER_TASK_LOCALS: PyOnceLock<pyo3_async_runtimes::TaskLocals> = PyOnceLock::new();
    WORKER_TASK_LOCALS.get_or_try_init(py, || {
        let event_loop = get_loop(py)?.bind(py).clone();
        // `get_loop` already errored if TASK_LOCALS was unset.
        let startup = TASK_LOCALS.get().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized")
        })?;
        Ok(pyo3_async_runtimes::TaskLocals::new(event_loop).with_context(startup.context(py)))
    })
}

/// Ready handles run per drain batch before the pump yields back to Tokio.
/// Bounds GIL-attached bursts: a self-rescheduling callback chain would
/// otherwise pin this worker thread (and its GIL attachment) forever.
const READY_QUEUE_DRAIN_LIMIT: usize = 128;

/// Run one ready Handle. Mirrors `BaseEventLoop._run_once`: skip cancelled
/// handles, otherwise call `Handle._run()` (which routes callback exceptions
/// to the loop's exception handler itself). Returns whether the handle ran.
fn run_handle(py: Python<'_>, loop_obj: &Py<PyAny>, handle: &Py<PyAny>) -> bool {
    let handle = handle.bind(py);
    match handle.getattr(intern!(py, "_cancelled")) {
        Ok(cancelled) if cancelled.is_truthy().unwrap_or(false) => return false,
        Ok(_) => {}
        Err(error) => {
            error.print(py);
            return false;
        }
    }
    if let Err(error) = handle.call_method0(intern!(py, "_run")) {
        // `Handle._run` only lets SystemExit/KeyboardInterrupt (or a broken
        // handle) escape; report those like any other callback failure.
        let failed = get_worker_fn(py, &WORKER_HANDLE_FAILED, "worker_handle_failed");
        if let Err(report_error) = failed.and_then(|f| f.call1(py, (loop_obj, handle, error))) {
            report_error.print(py);
        }
    }
    true
}

fn run_command(
    py: Python<'_>,
    loop_obj: &Py<PyAny>,
    tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    command: WorkerLoopCommand,
) {
    match command {
        WorkerLoopCommand::Soon(handle) => {
            run_handle(py, loop_obj, &handle);
        }
        #[cfg(unix)]
        WorkerLoopCommand::Ready(ready) => {
            if run_handle(py, loop_obj, &ready.handle) {
                ready.after_callback(tx);
            }
        }
    }
}

/// `asyncio.events._get_running_loop` / `_set_running_loop`: the running
/// loop is thread-local and the pump task can run on any Tokio worker
/// thread, so it is installed once per drain batch and restored afterwards
/// (other Python work on that thread must not observe a running loop).
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn running_loop_fn(
    py: Python<'_>,
    slot: &'static PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<&'static Py<PyAny>> {
    slot.get_or_try_init(py, || {
        Ok(py.import("asyncio.events")?.getattr(name)?.unbind())
    })
}

async fn run_ready_queue(
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    mut rx: tokio::sync::mpsc::UnboundedReceiver<WorkerLoopCommand>,
) {
    let tx = &tx;
    while let Some(first) = rx.recv().await {
        Python::attach(|py| {
            let Ok(loop_obj) = get_loop(py) else {
                return;
            };
            let (Ok(get_running), Ok(set_running)) = (
                running_loop_fn(py, &GET_RUNNING_LOOP, "_get_running_loop"),
                running_loop_fn(py, &SET_RUNNING_LOOP, "_set_running_loop"),
            ) else {
                return;
            };
            let previous = match get_running.call0(py) {
                Ok(previous) => previous,
                Err(error) => {
                    error.print(py);
                    return;
                }
            };
            if let Err(error) = set_running.call1(py, (loop_obj,)) {
                error.print(py);
                return;
            }

            // Future chaining commonly produces several immediately-ready
            // handles. Drain them under one GIL attachment, bounded so the
            // outer `recv().await` stays a scheduler yield point (leftover
            // handles are picked up immediately, FIFO order preserved).
            let mut drained = 0usize;
            let mut next = Some(first);
            loop {
                if let Some(command) = next.take() {
                    run_command(py, loop_obj, tx, command);
                    drained += 1;
                    if drained >= READY_QUEUE_DRAIN_LIMIT {
                        break;
                    }
                }
                match rx.try_recv() {
                    Ok(command) => next = Some(command),
                    Err(_) => break,
                }
            }

            if let Err(error) = set_running.call1(py, (previous,)) {
                error.print(py);
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

/// Test/introspection hook: descriptor watchers whose Tokio task is still
/// running. A watcher for a cancelled handle must exit rather than spin.
#[pyfunction]
pub(crate) fn worker_fd_watcher_count() -> usize {
    #[cfg(unix)]
    {
        WorkerLoopService::get().fd_watchers.live_count()
    }
    #[cfg(not(unix))]
    {
        0
    }
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
                // cancelled handle is safe (the pump re-checks `_cancelled`).
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
        let duration = std::time::Duration::try_from_secs_f64(delay)
            .map_err(|_| pyo3::exceptions::PyOverflowError::new_err("timer delay is too large"))?;
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

    #[cfg(unix)]
    fn add_reader(&self, fd: i32, handle: Py<PyAny>) -> PyResult<()> {
        let service = WorkerLoopService::get();
        service.fd_watchers.add(
            &service.runtime,
            &self.tx,
            fd,
            crate::worker_fd::Direction::Read,
            handle,
        )
    }

    #[cfg(unix)]
    fn add_writer(&self, fd: i32, handle: Py<PyAny>) -> PyResult<()> {
        let service = WorkerLoopService::get();
        service.fd_watchers.add(
            &service.runtime,
            &self.tx,
            fd,
            crate::worker_fd::Direction::Write,
            handle,
        )
    }

    #[cfg(unix)]
    fn remove_reader(&self, fd: i32) -> bool {
        WorkerLoopService::get()
            .fd_watchers
            .remove(fd, crate::worker_fd::Direction::Read)
    }

    #[cfg(unix)]
    fn remove_writer(&self, fd: i32) -> bool {
        WorkerLoopService::get()
            .fd_watchers
            .remove(fd, crate::worker_fd::Direction::Write)
    }

    #[cfg(not(unix))]
    fn add_reader(&self, _fd: i32, _handle: Py<PyAny>) -> PyResult<()> {
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "WorkerLoop file-descriptor watching requires Unix",
        ))
    }

    #[cfg(not(unix))]
    fn add_writer(&self, _fd: i32, _handle: Py<PyAny>) -> PyResult<()> {
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "WorkerLoop file-descriptor watching requires Unix",
        ))
    }

    #[cfg(not(unix))]
    fn remove_reader(&self, _fd: i32) -> bool {
        false
    }

    #[cfg(not(unix))]
    fn remove_writer(&self, _fd: i32) -> bool {
        false
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
        // Cancels delivery to Python only. Tokio's process-level signal
        // handler cannot be uninstalled, so unlike stdlib loops this does not
        // restore SIG_DFL — the signal is ignored from here on.
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

/// Deliberate: if the caller's future is dropped (client disconnect), the
/// Python task is NOT cancelled — it runs to completion and its result is
/// discarded, consistent with tasks being allowed to outlive responses.
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

/// Holds the asyncio Task created by `worker_dispatch_start_cancellable` so a
/// later cancellation can reach it. Filled from the WorkerLoop thread.
#[pyclass(frozen)]
pub(crate) struct WorkerTaskHandle {
    task: std::sync::Mutex<Option<Py<PyAny>>>,
    cancelled: std::sync::atomic::AtomicBool,
}

#[pymethods]
impl WorkerTaskHandle {
    fn set_task(&self, task: Bound<'_, PyAny>) {
        let mut slot = self.task.lock().unwrap();
        if self.cancelled.load(std::sync::atomic::Ordering::SeqCst) {
            drop(slot);
            let _ = task.call_method0("cancel");
        } else {
            *slot = Some(task.unbind());
        }
    }
}

/// Cancellable variant of [`dispatch`], used by MCP tool calls: rmcp fires the
/// request's `CancellationToken` when the client closes the SSE response
/// stream (the 2026-07-28 cancellation signal) or sends
/// `notifications/cancelled` (legacy sessions). Plain routes keep the
/// non-cancelling `dispatch` semantics deliberately (tasks may outlive
/// responses); MCP semantics require the tool to actually stop.
pub(crate) async fn dispatch_cancellable(
    dispatch: Py<PyAny>,
    payload: Py<PyAny>,
    ct: tokio_util::sync::CancellationToken,
) -> PyResult<Py<PyAny>> {
    let (tx, mut rx) = tokio::sync::oneshot::channel();
    let handle = Python::attach(|py| {
        let resolver = Py::new(
            py,
            WorkerDispatchResolver {
                tx: std::sync::Mutex::new(Some(tx)),
            },
        )?;
        let handle = Py::new(
            py,
            WorkerTaskHandle {
                task: std::sync::Mutex::new(None),
                cancelled: std::sync::atomic::AtomicBool::new(false),
            },
        )?;
        let start = get_worker_fn(
            py,
            &WORKER_DISPATCH_START_CANCELLABLE,
            "worker_dispatch_start_cancellable",
        )?;
        start.call1(
            py,
            (
                dispatch,
                payload,
                get_loop(py)?,
                resolver,
                handle.clone_ref(py),
            ),
        )?;
        Ok::<_, PyErr>(handle)
    })?;

    tokio::select! {
        result = &mut rx => match result {
            Ok(result) => result,
            Err(_) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "worker dispatch resolver dropped without a result",
            )),
        },
        _ = ct.cancelled() => {
            Python::attach(|py| {
                let inner = handle.bind(py).get();
                inner.cancelled.store(true, std::sync::atomic::Ordering::SeqCst);
                if let Some(task) = inner.task.lock().unwrap().take() {
                    // Schedule `.cancel()` on the task's own loop; calling it
                    // from this thread directly is not loop-safe.
                    let task = task.bind(py);
                    if let (Ok(task_loop), Ok(cancel)) =
                        (task.call_method0("get_loop"), task.getattr("cancel"))
                    {
                        let _ = task_loop.call_method1("call_soon_threadsafe", (cancel,));
                    }
                }
            });
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "MCP request cancelled by client",
            ))
        }
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
