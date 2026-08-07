//! Background supervisor for the 230M Orchestrator baseline.
//!
//! Every `poll_interval` seconds the supervisor probes the baseline port with
//! a fast `/health` request. If the port is unreachable, the baseline child is
//! assumed dead and is respawned via `DemandPool::respawn_baseline`. The
//! coding-agent pool is not supervised here; its children are kill-after-task
//! by design.

use crate::demand::DemandPool;
use crate::metrics::MetricsStore;
use std::{sync::Arc, time::Duration};
use tokio::time::{interval, MissedTickBehavior};
use tracing::{error, info, warn};

pub struct Supervisor {
    pub pool: Arc<DemandPool>,
    pub poll_interval: Duration,
}

impl Supervisor {
    pub fn new(pool: Arc<DemandPool>, poll_interval: Duration) -> Self {
        Self {
            pool,
            poll_interval,
        }
    }

    /// Spawn the supervisor loop. Returns immediately; the task runs in the
    /// background and is cancelled when the runtime shuts down.
    pub fn spawn(self, metrics: Arc<MetricsStore>) {
        tokio::spawn(async move {
            let mut tick = interval(self.poll_interval);
            tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
            // Skip the first immediate tick — give the baseline a chance to settle.
            tick.tick().await;
            loop {
                self.step(&metrics).await;
                tick.tick().await;
            }
        });
    }

    async fn step(&self, metrics: &Arc<MetricsStore>) {
        if !self.pool.spec.always_alive {
            return;
        }
        let port = match self.pool.baseline_port {
            Some(p) => p,
            None => return,
        };

        if self.pool.baseline_alive().await {
            if let Ok(()) = ping(port, &self.pool.spec.host).await {
                return;
            }
        }

        warn!(port, "baseline orchestrator unreachable; respawning");
        metrics.event(format!("baseline orchestrator on {port}: respawning"));
        match self.pool.respawn_baseline(metrics).await {
            Ok(new_port) => info!(new_port, "orchestrator baseline respawned"),
            Err(err) => {
                error!(error = %err, "orchestrator baseline failed to respawn");
                metrics.event(format!("orchestrator baseline respawn failed: {err}"));
            }
        }
    }
}

async fn ping(port: u16, host: &str) -> Result<(), ()> {
    let url = format!("http://{host}:{port}/health");
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return Err(()),
    };
    match client.get(&url).send().await {
        Ok(r) if r.status().is_success() => Ok(()),
        _ => Err(()),
    }
}
