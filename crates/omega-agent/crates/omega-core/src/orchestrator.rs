//! Compatibility wrapper for deterministic request routing.
//!
//! No model is invoked here. The 1.2B coding pool remains the execution lane;
//! this wrapper is retained until the HTTP request kind is fully canonical.

use crate::demand::{DemandPool, InstanceGuard};
use omega_common::metrics::MetricsStore;
use omega_common::types::{ChatMessage, Intent};
use std::{sync::Arc, time::Duration};

pub struct Orchestrator {
    pool: Arc<DemandPool>,
    timeout: Duration,
}

impl Orchestrator {
    pub fn new(pool: Arc<DemandPool>, timeout: Duration) -> Self { Self { pool, timeout } }

    /// Mechanical compatibility routing: tool continuations use the execution
    /// lane; ordinary requests use the same lane without semantic inference.
    pub async fn classify(&self, messages: &[ChatMessage], metrics: &Arc<MetricsStore>) -> (Intent, InstanceGuard) {
        let guard = self.pool.clone().acquire(metrics, self.timeout).await
            .unwrap_or_else(|_| InstanceGuard::baseline(self.pool.clone(), self.pool.baseline_port.unwrap_or(0)));
        let intent = if messages.iter().any(|message| message.role == "tool") {
            Intent::Code
        } else {
            Intent::Chat
        };
        metrics.event(format!("deterministic request routing: {}", intent.as_str()));
        (intent, guard)
    }
}
