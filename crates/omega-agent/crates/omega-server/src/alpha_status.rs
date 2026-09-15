//! Alpha status channel.
//!
//! Reports this master server's status snapshot to the Ashat Hub — the Alpha
//! server's Chat Studio, the single point of incoming/outgoing traffic for
//! the coding-agent ecosystem. Gated by `hub.enabled` in `server-config.json`;
//! when disabled the module stays inert. Later phases extend this same seam to
//! seed Beta / Delta peers with updates and to drive the GitHub updater.

use crate::handlers::{status_snapshot, AppState};
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::time::{interval, Duration, MissedTickBehavior};
use tracing::{debug, warn};

/// Periodically POSTs the master's status snapshot to the Ashat Hub.
pub struct AlphaReporter {
    state: Arc<AppState>,
    hub_url: String,
    interval: Duration,
}

impl AlphaReporter {
    pub fn new(state: Arc<AppState>, hub_url: String, interval: Duration) -> Self {
        Self {
            state,
            hub_url,
            interval,
        }
    }

    /// Spawn the reporting loop. Runs until the runtime shuts down.
    pub fn spawn(self) {
        tokio::spawn(async move {
            if self.hub_url.trim().is_empty() {
                debug!("alpha status reporter enabled but hub.url is empty; idling");
                return;
            }
            let mut tick = interval(self.interval);
            tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
            loop {
                // Tick first: tokio's `interval` completes its first tick
                // immediately, so this posts once at boot and then every
                // interval (no duplicate back-to-back startup posts).
                tick.tick().await;
                let status = status_snapshot(&self.state).await;
                let countdown = self.status_countdown();
                match Self::post(&self.hub_url, &status, &countdown).await {
                    Ok(()) => debug!("alpha status posted to hub"),
                    Err(err) => warn!(error = %err, "alpha status post to hub failed"),
                }
            }
        });
    }

    /// Completion countdown for active coding-agent runs (kept fresh so the
    /// Hub session stays open with a live ETA for the user's project).
    fn status_countdown(&self) -> Value {
        let active_runs: Vec<Value> = self
            .state
            .metrics
            .active_run_etas()
            .iter()
            .map(|r| {
                json!({
                    "port": r.port,
                    "elapsed_seconds": r.elapsed_seconds,
                    "eta_seconds": r.eta_seconds,
                })
            })
            .collect();
        json!({
            "active_runs": active_runs,
            "generated_at": chrono::Utc::now().to_rfc3339(),
        })
    }

    async fn post(
        url: &str,
        status: &omega_common::types::PublicStatus,
        countdown: &Value,
    ) -> Result<(), String> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|err| err.to_string())?;
        let body = json!({
            "instance": "omega",
            "status": status,
            "countdown": countdown,
        });
        let response = client
            .post(url)
            .json(&body)
            .send()
            .await
            .map_err(|err| err.to_string())?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(format!("hub returned HTTP {}", response.status().as_u16()))
        }
    }
}
