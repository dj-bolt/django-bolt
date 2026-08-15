//! Native file-descriptor readiness for `WorkerLoop` (Unix).
//!
//! `add_reader`/`add_writer` register the descriptor with the Tokio reactor
//! directly, so asyncio transports, `sock_*` helpers, pipes, and drivers such
//! as psycopg that use the selector-style fd API never leave the worker
//! runtime. Readiness is delivered as a `WorkerLoopCommand::Ready` on the same
//! pump that services `call_soon`, so callbacks stay FIFO with other handles.
//!
//! asyncio's selector is level-triggered while Tokio's reactor is
//! edge-triggered. After each callback the pump re-checks the descriptor with
//! a zero-timeout `poll(2)` and requeues the callback while the kernel still
//! reports it ready, so a callback that consumes only part of the pending
//! data (a stdlib transport reading one `max_size` chunk) is called again
//! instead of stalling; the watcher only wakes to clear Tokio's cached
//! readiness once the descriptor is drained.
//!
//! Each registration watches its own `dup(2)` of the descriptor: epoll and
//! kqueue key registrations by descriptor number, so an independent reader
//! and writer registration on one socket needs two descriptors, and a
//! replaced registration can be torn down asynchronously without racing the
//! new one for the same number. Consequently a descriptor must be removed
//! (`remove_reader`/`remove_writer`) before it is closed: closing the
//! caller's descriptor does not close the duplicate, so the watcher keeps the
//! open file description alive and keeps reporting readiness. asyncio's own
//! transports and `sock_*` helpers already remove before closing.

use pyo3::prelude::*;
use std::collections::HashMap;
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::sync::{Arc, Mutex};
use tokio::io::unix::AsyncFd;
use tokio::io::Interest;

use crate::worker_loop::WorkerLoopCommand;

/// What the pump runs when a descriptor is ready.
#[derive(Clone)]
pub(crate) enum FdCallback {
    /// An asyncio `Handle` from `add_reader`/`add_writer` (opaque callback:
    /// level triggering is emulated with `poll(2)` afterwards).
    Handle(Arc<Py<PyAny>>),
    /// A native `SocketTransport`; its read/write result says whether the
    /// descriptor is drained, so no `poll(2)` is needed.
    Transport(Arc<Py<crate::worker_transport::SocketTransport>>),
}

/// Result of one readiness callback, decided by the pump.
pub(crate) enum ReadyOutcome {
    /// Kernel may still have data/space: requeue without waking the watcher.
    StillReady,
    /// Descriptor drained: the watcher clears Tokio's readiness and re-arms.
    Drained,
    /// Registration is over (cancelled handle, transport closed/paused/eof).
    Stop,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum Direction {
    Read,
    Write,
}

impl Direction {
    fn interest(self) -> Interest {
        match self {
            Direction::Read => Interest::READABLE,
            Direction::Write => Interest::WRITABLE,
        }
    }

    fn poll_events(self) -> libc::c_short {
        match self {
            Direction::Read => libc::POLLIN,
            Direction::Write => libc::POLLOUT,
        }
    }
}

#[derive(Default)]
pub(crate) struct FdWatchers {
    tasks: Mutex<HashMap<(RawFd, Direction), tokio::task::AbortHandle>>,
}

impl FdWatchers {
    /// Register `handle` for `direction` on `fd`, replacing any previous
    /// registration. The caller (Python) has already cancelled the previous
    /// Handle, so a readiness event still in flight for it is a no-op.
    pub(crate) fn add(
        &self,
        runtime: &tokio::runtime::Handle,
        tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
        fd: RawFd,
        direction: Direction,
        callback: FdCallback,
    ) -> PyResult<()> {
        // SAFETY: the descriptor is owned by Python for the duration of this
        // call; the borrow only lives long enough to dup it.
        let owned: OwnedFd = unsafe { BorrowedFd::borrow_raw(fd) }
            .try_clone_to_owned()
            .map_err(PyErr::from)?;
        let async_fd = {
            let _guard = runtime.enter();
            AsyncFd::with_interest(owned, direction.interest()).map_err(PyErr::from)?
        };
        let task = runtime.spawn(watch(async_fd, direction, callback, tx.clone()));
        if let Some(previous) = self
            .tasks
            .lock()
            .unwrap()
            .insert((fd, direction), task.abort_handle())
        {
            previous.abort();
        }
        Ok(())
    }

    /// Number of watcher tasks that are still running (test hook).
    pub(crate) fn live_count(&self) -> usize {
        self.tasks
            .lock()
            .unwrap()
            .values()
            .filter(|task| !task.is_finished())
            .count()
    }

    pub(crate) fn remove(&self, fd: RawFd, direction: Direction) -> bool {
        match self.tasks.lock().unwrap().remove(&(fd, direction)) {
            Some(task) => {
                task.abort();
                true
            }
            None => false,
        }
    }
}

/// One readiness delivery. The pump owns the level-trigger re-check: after
/// the callback it polls the descriptor and requeues itself while the
/// descriptor stays ready, and only wakes the watcher (to clear Tokio's
/// cached readiness and re-arm) once the kernel says it is drained.
pub(crate) struct Ready {
    callback: FdCallback,
    fd: RawFd,
    direction: Direction,
    rearm: tokio::sync::oneshot::Sender<()>,
}

impl Ready {
    /// Run the callback on the pump thread (GIL held), then requeue, re-arm,
    /// or stop. Dropping `self` without re-arming stops the watcher, as the
    /// stdlib selector loop drops cancelled readers.
    pub(crate) fn run(
        self,
        py: Python<'_>,
        loop_obj: &Py<PyAny>,
        tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    ) {
        let outcome = match &self.callback {
            FdCallback::Handle(handle) => {
                if crate::worker_loop::run_handle(py, loop_obj, handle) {
                    if still_ready(self.fd, self.direction) {
                        ReadyOutcome::StillReady
                    } else {
                        ReadyOutcome::Drained
                    }
                } else {
                    ReadyOutcome::Stop
                }
            }
            FdCallback::Transport(transport) => match self.direction {
                Direction::Read => transport.get().on_readable(transport, py),
                Direction::Write => transport.get().on_writable(transport, py),
            },
        };
        match outcome {
            ReadyOutcome::StillReady => {
                let _ = tx.send(WorkerLoopCommand::Ready(self));
            }
            ReadyOutcome::Drained => {
                let _ = self.rearm.send(());
            }
            ReadyOutcome::Stop => {}
        }
    }
}

fn still_ready(fd: RawFd, direction: Direction) -> bool {
    let mut pollfd = libc::pollfd {
        fd,
        events: direction.poll_events(),
        revents: 0,
    };
    // SAFETY: `pollfd` is a valid, exclusively borrowed array of one entry.
    let rc = unsafe { libc::poll(&mut pollfd, 1, 0) };
    // On error (EINTR) report ready: an extra callback is harmless, a lost
    // wakeup is not.
    rc != 0
}

async fn watch(
    async_fd: AsyncFd<OwnedFd>,
    direction: Direction,
    callback: FdCallback,
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
) {
    let fd = async_fd.get_ref().as_raw_fd();
    loop {
        let mut guard = match async_fd.ready(direction.interest()).await {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let (rearm_tx, rearm_rx) = tokio::sync::oneshot::channel();
        let ready = Ready {
            callback: callback.clone(),
            fd,
            direction,
            rearm: rearm_tx,
        };
        if tx.send(WorkerLoopCommand::Ready(ready)).is_err() {
            return;
        }
        // The pump requeues itself while the descriptor stays ready and
        // re-arms us once it is drained; a cancelled handle drops the sender.
        if rearm_rx.await.is_err() {
            return;
        }
        guard.clear_ready();
    }
}
