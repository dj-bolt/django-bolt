//! SEP-2663 Tasks state for one mount: rmcp's `TaskManager` plus the
//! principal that created each task.
//!
//! `TaskManager` keeps tasks in process memory. With `SO_REUSEPORT`
//! multi-process serving, `tasks/get` can reach a process that does not hold
//! the task. Worker recycling has the same effect while the old worker
//! drains. Thus runbolt rejects task tools with `--processes > 1`,
//! `--max-rss` and `--workers-lifetime`.
//!
//! Task ids are random UUIDs, but `tasks/get` / `tasks/update` /
//! `tasks/cancel` from a different principal must still not reach the task:
//! such calls get the same `-32602` as an unknown task id.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use rmcp::model::{ErrorData as McpError, Task};
use rmcp::task_manager::TaskManager;

struct TaskOwner {
    principal_hash: String,
    /// The manager evicts a task one TTL after it settles, and a task settles
    /// no later than its TTL. Two TTLs from creation thus covers the task.
    evict_after: Instant,
}

#[derive(Default)]
pub struct McpTasks {
    pub manager: TaskManager,
    owners: Mutex<HashMap<String, TaskOwner>>,
}

/// Same error rmcp's `TaskManager` returns for an unknown task id.
fn unknown_task(task_id: &str) -> McpError {
    McpError::invalid_params(format!("unknown task: {task_id}"), None)
}

impl McpTasks {
    /// Bind a new task to the principal that created it. Also drops the
    /// bindings of tasks that the manager has evicted.
    pub fn record_owner(&self, task: &Task, principal_hash: String) {
        let now = Instant::now();
        // Task tools always carry a TTL (Python rejects an unlimited one).
        let ttl = Duration::from_millis(task.ttl_ms.unwrap_or(0));
        let mut owners = self.owners.lock().unwrap();
        owners.retain(|_, owner| owner.evict_after > now);
        owners.insert(
            task.task_id.clone(),
            TaskOwner {
                principal_hash,
                evict_after: now + ttl * 2,
            },
        );
    }

    /// Fail with `-32602` unless `principal_hash` created the task.
    pub fn check_owner(&self, task_id: &str, principal_hash: &str) -> Result<(), McpError> {
        let owners = self.owners.lock().unwrap();
        match owners.get(task_id) {
            Some(owner) if owner.principal_hash == principal_hash => Ok(()),
            _ => Err(unknown_task(task_id)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rmcp::model::TaskStatus;

    fn task(id: &str, ttl_ms: u64) -> Task {
        let mut task = Task::new(id, TaskStatus::Working, "t", "t");
        task.ttl_ms = Some(ttl_ms);
        task
    }

    #[test]
    fn owner_check_rejects_other_principals_and_unknown_ids() {
        let tasks = McpTasks::default();
        tasks.record_owner(&task("a", 60_000), "alice".into());
        assert!(tasks.check_owner("a", "alice").is_ok());
        assert_eq!(
            tasks.check_owner("a", "mallory").unwrap_err().code,
            rmcp::model::ErrorCode::INVALID_PARAMS
        );
        assert!(tasks.check_owner("missing", "alice").is_err());
    }

    #[test]
    fn expired_bindings_are_pruned_on_record() {
        let tasks = McpTasks::default();
        tasks.record_owner(&task("old", 1), "alice".into());
        std::thread::sleep(Duration::from_millis(5));
        tasks.record_owner(&task("new", 60_000), "alice".into());
        let owners = tasks.owners.lock().unwrap();
        assert!(!owners.contains_key("old"));
        assert!(owners.contains_key("new"));
    }
}
