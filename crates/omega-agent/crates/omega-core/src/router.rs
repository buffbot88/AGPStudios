//! Row-chain walker. Walks `ServerConfig.row_chain` in order, returning the
//! first backend that is both `enabled` and `/health`-reachable. Used by the
//! `/v1/chat/completions` handler before proxying to the local Coding Agent
//! pool — for Phase 1 only omega is enabled, so the result is always the
//! local loopback target. Phase 2 / Phase 3 light this up for `beta` /
//! `delta` by flipping `enabled: true` in `server-config.json`.

use omega_common::config::AppConfig;
use omega_common::types::{BackendServer, RouterError, RowChainEntry};
use std::time::Duration;

pub struct RowRouter {
    entries: Vec<RowChainEntry>,
    backends: Vec<BackendServer>,
}

impl RowRouter {
    pub fn new(cfg: &AppConfig) -> Self {
        Self {
            entries: cfg.row_chain_entries.clone(),
            backends: cfg.backends.clone(),
        }
    }

    /// Walk the chain; return the first enabled backend whose `/health`
    /// endpoint reports ok. Falls back to the first enabled backend (without
    /// health check) if the local probe loop runs out of attempts — that's the
    /// line that keeps "failure isn't an option" operative.
    pub async fn pick(&self) -> Result<BackendServer, RouterError> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .map_err(|_| RouterError::ChainExhausted)?;

        for entry in &self.entries {
            let Some(backend) = self.backends.iter().find(|b| b.id == entry.server_id) else {
                continue;
            };
            if !backend.enabled {
                continue;
            }
            let url = format!("http://{}:{}/health", backend.host, backend.port);
            match client.get(&url).send().await {
                Ok(r) if r.status().is_success() => return Ok(backend.clone()),
                Ok(_) | Err(_) => continue,
            }
        }

        // Last-resort: any enabled backend, even if its /health is returning
        // bad. Keeps service-up under transient outages.
        self.backends
            .iter()
            .find(|b| b.enabled)
            .cloned()
            .ok_or(RouterError::ChainExhausted)
    }
}
