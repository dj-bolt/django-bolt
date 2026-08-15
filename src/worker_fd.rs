//! Native file-descriptor readiness for `WorkerLoop` (Unix).
//!
//! `add_reader`/`add_writer` register the descriptor with the Tokio reactor
//! directly, so asyncio transports, `sock_*` helpers, pipes, and drivers such
//! as psycopg that use the selector-style fd API never leave the worker
//! runtime. Readiness is delivered as a `WorkerLoopCommand::Ready` on the same
//! pump that services `call_soon`, so callbacks stay FIFO with other handles.
//!
//! asyncio's selector is level-triggered while Tokio's reactor is
//! edge-triggered. The watcher re-checks the descriptor with a zero-timeout
//! `poll(2)` after each callback and only clears Tokio's cached readiness when
//! the kernel says the descriptor is no longer ready, so a callback that
//! consumes only part of the pending data (a stdlib transport reading one
//! `max_size` chunk) is called again instead of stalling.
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
        handle: Py<PyAny>,
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
        let task = runtime.spawn(watch(async_fd, direction, Arc::new(handle), tx.clone()));
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
    handle: Arc<Py<PyAny>>,
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
) {
    let raw = async_fd.get_ref().as_raw_fd();
    loop {
        let mut guard = match async_fd.ready(direction.interest()).await {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        if tx
            .send(WorkerLoopCommand::Ready(Arc::clone(&handle), ack_tx))
            .is_err()
        {
            return;
        }
        // Wait until the callback ran before re-arming: firing again while it
        // is still queued would run it twice for one readiness event. A
        // cancelled handle stops the watcher (as the stdlib selector loop
        // drops cancelled readers) instead of requeueing while the
        // descriptor stays ready.
        if !matches!(ack_rx.await, Ok(true)) {
            return;
        }
        if !still_ready(raw, direction) {
            guard.clear_ready();
        }
    }
}
