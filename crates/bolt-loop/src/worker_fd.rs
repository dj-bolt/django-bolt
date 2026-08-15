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

use crate::WorkerLoopCommand;

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub enum Direction {
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
pub struct FdWatchers {
    tasks: Mutex<HashMap<(RawFd, Direction), tokio::task::AbortHandle>>,
}

impl FdWatchers {
    /// Register `handle` for `direction` on `fd`, replacing any previous
    /// registration. The caller (Python) has already cancelled the previous
    /// Handle, so a readiness event still in flight for it is a no-op.
    pub fn add(
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
        let task = runtime.spawn(watch(async_fd, fd, direction, Arc::new(handle), tx.clone()));
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

    /// Number of registered watchers (test hook). Watchers unregister
    /// themselves on exit, so this is the live count.
    pub fn live_count(&self) -> usize {
        self.tasks.lock().unwrap().len()
    }

    /// Drop the entry for `(fd, direction)` if it still belongs to the task
    /// `id` (an exiting watcher must not remove a replacement registered
    /// under the same key).
    fn retire(&self, fd: RawFd, direction: Direction, id: tokio::task::Id) {
        let mut tasks = self.tasks.lock().unwrap();
        if tasks
            .get(&(fd, direction))
            .is_some_and(|task| task.id() == id)
        {
            tasks.remove(&(fd, direction));
        }
    }

    pub fn remove(&self, fd: RawFd, direction: Direction) -> bool {
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
pub struct Ready {
    pub handle: Arc<Py<PyAny>>,
    fd: RawFd,
    direction: Direction,
    rearm: tokio::sync::oneshot::Sender<()>,
}

impl Ready {
    /// Called by the pump after the callback ran. Dropping `self` without
    /// re-arming (cancelled handle: the pump never calls this) stops the
    /// watcher, as the stdlib selector loop drops cancelled readers.
    pub fn after_callback(self, tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>) {
        if still_ready(self.fd, self.direction) {
            let _ = tx.send(WorkerLoopCommand::Ready(self));
        } else {
            let _ = self.rearm.send(());
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

/// Unregisters the watcher's map entry however the task ends: readiness
/// error, closed pump, a cancelled handle stopping it, or abort.
struct Retire {
    fd: RawFd,
    direction: Direction,
}

impl Drop for Retire {
    fn drop(&mut self) {
        crate::fd_watchers().retire(self.fd, self.direction, tokio::task::id());
    }
}

async fn watch(
    async_fd: AsyncFd<OwnedFd>,
    key_fd: RawFd,
    direction: Direction,
    handle: Arc<Py<PyAny>>,
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
) {
    let _retire = Retire {
        fd: key_fd,
        direction,
    };
    let fd = async_fd.get_ref().as_raw_fd();
    loop {
        let mut guard = match async_fd.ready(direction.interest()).await {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let (rearm_tx, rearm_rx) = tokio::sync::oneshot::channel();
        let ready = Ready {
            handle: Arc::clone(&handle),
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
