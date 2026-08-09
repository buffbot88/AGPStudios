//! Bounded FIFO used by both demand pools to fall through to when every spawn
//! slot is busy; the deadline for an aged-out head lives in `tokio::time::timeout`
//! at the call site, not in the queue itself.

use std::{collections::VecDeque, sync::Arc};
use tokio::sync::{Mutex, Notify};

/// One waiting task. Currently a unit struct; future aging metrics would attach
/// here without changing the public API.
#[derive(Debug, Clone, Copy)]
struct Ticket;

/// Bounded FIFO shared between the Orchestrator pool and the Coding Agent pool.
#[derive(Debug, Clone)]
pub struct WaitQueue {
    inner: Arc<Inner>,
}

#[derive(Debug)]
struct Inner {
    tickets: Mutex<VecDeque<Ticket>>,
    limit: usize,
    notify: Notify,
}

impl WaitQueue {
    pub fn new(limit: usize) -> Self {
        Self {
            inner: Arc::new(Inner {
                tickets: Mutex::new(VecDeque::with_capacity(limit)),
                limit,
                notify: Notify::new(),
            }),
        }
    }

    /// Currently waiting tasks.
    pub async fn depth(&self) -> usize {
        self.inner.tickets.lock().await.len()
    }

    /// Configured cap.
    pub fn limit(&self) -> usize {
        self.inner.limit
    }

    /// Try to enqueue. `true` = ticket accepted; `false` = queue is full so
    /// the caller should escalate to `PoolExhausted`.
    pub async fn try_enqueue(&self) -> bool {
        let mut tickets = self.inner.tickets.lock().await;
        if tickets.len() >= self.inner.limit {
            return false;
        }
        tickets.push_back(Ticket);
        true
    }

    /// Wakes all current awaiters so they retry the slot-acquire path.
    pub fn notify_slot_available(&self) {
        self.inner.notify.notify_waiters();
    }

    /// Drop the head ticket after the timeout fires or the slot has been
    /// claimed. Idempotent with the `try_enqueue` of the next waiter.
    pub async fn remove(&self) {
        let mut tickets = self.inner.tickets.lock().await;
        tickets.pop_front();
    }

    /// Await a slot-available notification.
    pub async fn wait_for_slot(&self) {
        self.inner.notify.notified().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bounded_fifo_accepts_until_limit() {
        let q = WaitQueue::new(2);
        assert!(q.try_enqueue().await);
        assert!(q.try_enqueue().await);
        assert!(!q.try_enqueue().await);
        assert_eq!(q.depth().await, 2);
        assert_eq!(q.limit(), 2);
    }

    #[tokio::test]
    async fn remove_drops_head() {
        let q = WaitQueue::new(2);
        q.try_enqueue().await;
        q.try_enqueue().await;
        q.remove().await;
        assert_eq!(q.depth().await, 1);
        q.remove().await;
        assert_eq!(q.depth().await, 0);
    }

    #[tokio::test]
    async fn wait_for_slot_wakes_on_notify() {
        let q = WaitQueue::new(1);
        let waiter = q.clone();
        let handle = tokio::spawn(async move {
            tokio::time::timeout(std::time::Duration::from_secs(2), waiter.wait_for_slot())
                .await
                .expect("woken before timeout");
        });
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        q.notify_slot_available();
        handle.await.expect("waiter task");
    }
}
