//! Row-chain router. Probes every enabled backend's `/health` concurrently
//! (cached briefly so requests don't re-probe constantly), then picks among
//! the healthy ones with **smooth weighted round-robin** (nginx-style): each
//! backend's `weight` from `server-config.json:row_chain` controls its
//! relative traffic share (omega 2 / beta 1 / delta 1 → omega gets twice the
//! traffic of each slave). A backend that fails its probe simply loses its
//! share until it recovers; if every probe fails, the router still falls back
//! to the first enabled backend so "failure isn't an option" stays operative.
//!
//! The router is created once and shared via `Arc<AppState>`, so the probe
//! cache and the running selection weights persist across requests.

use omega_common::config::AppConfig;
use omega_common::types::{BackendServer, RouterError};
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};

/// How long a successful `/health` probe is trusted before re-probing.
const HEALTH_TTL: Duration = Duration::from_secs(5);
/// Failures are re-probed sooner so a recovering backend rejoins the rotation
/// quickly.
const FAIL_TTL: Duration = Duration::from_secs(2);
/// Per-probe HTTP timeout.
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

struct HealthEntry {
    healthy: bool,
    checked_at: Instant,
}

impl HealthEntry {
    /// TTL depends on the last result: healthy backends are trusted longer,
    /// failures are re-checked sooner.
    fn ttl(&self) -> Duration {
        if self.healthy {
            HEALTH_TTL
        } else {
            FAIL_TTL
        }
    }
}

pub struct RowRouter {
    /// Enabled backends in `server-config.json:row_chain` order — this Vec
    /// order *is* the chain order (no separate entries list needed).
    backends: Vec<BackendServer>,
    client: reqwest::Client,
    /// id -> last probe result. Interior mutability: the router is shared via
    /// `Arc<AppState>` across handlers.
    health: tokio::sync::Mutex<HashMap<String, HealthEntry>>,
    /// Smooth WRRS running weights, aligned with `self.backends` by index.
    current: Mutex<Vec<i64>>,
}

impl RowRouter {
    pub fn new(cfg: &AppConfig) -> Self {
        let n = cfg.backends.len();
        Self {
            backends: cfg.backends.clone(),
            client: reqwest::Client::builder()
                .timeout(PROBE_TIMEOUT)
                .build()
                .expect("reqwest client build"),
            health: tokio::sync::Mutex::new(HashMap::new()),
            current: Mutex::new(vec![0i64; n]),
        }
    }

    /// Pick the backend for this request.
    ///
    /// 1. Collect the enabled backends in chain order.
    /// 2. Probe (concurrently) only those whose cached status is missing or
    ///    older than their TTL; everyone else reuses the fresh cache.
    /// 3. Select among the healthy set with smooth weighted round-robin.
    /// 4. Last resort: if nothing is healthy, return the first enabled
    ///    backend anyway (service-up under transient outages).
    pub async fn pick(&self) -> Result<BackendServer, RouterError> {
        let candidates: Vec<&BackendServer> = self.backends.iter().filter(|b| b.enabled).collect();
        if candidates.is_empty() {
            return Err(RouterError::ChainExhausted);
        }

        // Which backends need a fresh probe right now?
        let now = Instant::now();
        let to_probe: Vec<&BackendServer> = {
            let cache = self.health.lock().await;
            candidates
                .iter()
                .copied()
                .filter(|b| match cache.get(&b.id) {
                    Some(e) if now.duration_since(e.checked_at) < e.ttl() => false,
                    _ => true,
                })
                .collect()
        };

        // Probe concurrently (all backends share the 2s budget — the worst
        // case is ~2s once, then the TTL cache covers subsequent picks).
        let results = futures::future::join_all(to_probe.iter().map(|b| self.probe(b))).await;
        {
            let mut cache = self.health.lock().await;
            for (b, healthy) in to_probe.iter().zip(results) {
                cache.insert(
                    b.id.clone(),
                    HealthEntry {
                        healthy,
                        checked_at: Instant::now(),
                    },
                );
            }
        }

        // Build the healthy set from the now-fresh cache.
        let healthy: Vec<BackendServer> = {
            let cache = self.health.lock().await;
            candidates
                .into_iter()
                .filter(|b| cache.get(&b.id).map(|e| e.healthy).unwrap_or(false))
                .cloned()
                .collect()
        };

        if healthy.is_empty() {
            // Last-resort: any enabled backend, even if its /health is
            // returning bad. Keeps service-up under transient outages.
            return self
                .backends
                .iter()
                .find(|b| b.enabled)
                .cloned()
                .ok_or(RouterError::ChainExhausted);
        }

        Ok(self.select_weighted(&healthy))
    }

    /// Force-forget a backend's healthy status so the next `pick()` re-probes
    /// it and (while it stays down) it drops out of the rotation. Called by
    /// the chat handler when a forward to a picked backend fails with a
    /// connection error, so a backend that died mid-flight loses its share
    /// immediately instead of waiting out the TTL.
    pub async fn mark_unhealthy(&self, id: &str) {
        self.health.lock().await.insert(
            id.to_owned(),
            HealthEntry {
                healthy: false,
                checked_at: Instant::now(),
            },
        );
    }

    async fn probe(&self, b: &BackendServer) -> bool {
        let url = format!("http://{}:{}/health", b.host, b.port);
        match self.client.get(&url).send().await {
            Ok(r) => r.status().is_success(),
            Err(_) => false,
        }
    }

    /// Smooth weighted round-robin over the healthy set (in chain order).
    ///
    /// Each pick adds `weight` to a running total, picks the highest running
    /// total, then subtracts the grand total from the winner. Deterministic
    /// and stateless across instances of the loop — weights 2:1:1 yield the
    /// stable pattern A B A C A B A C … with no bursty runs.
    fn select_weighted(&self, healthy: &[BackendServer]) -> BackendServer {
        // Map healthy backends to their position in self.backends (the
        // running-weight vector is aligned with self.backends by index).
        let positions: Vec<usize> = healthy
            .iter()
            .filter_map(|h| self.backends.iter().position(|b| b.id == h.id))
            .collect();
        debug_assert_eq!(positions.len(), healthy.len());

        let mut current = self.current.lock().unwrap_or_else(|e| e.into_inner());
        let mut total: i64 = 0;
        let mut best_idx = 0usize;
        let mut best_pos = positions[0];
        let mut best_score = i64::MIN;

        for (i, &pos) in positions.iter().enumerate() {
            let weight = self.backends[pos].weight.max(1) as i64; // clamp 0 -> 1
            current[pos] += weight;
            total += weight;
            if current[pos] > best_score {
                best_score = current[pos];
                best_idx = i;
                best_pos = pos;
            }
        }

        current[best_pos] -= total;
        healthy[best_idx].clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backend(id: &str, weight: u32) -> BackendServer {
        BackendServer {
            id: id.to_owned(),
            host: "127.0.0.1".to_owned(),
            port: 8080,
            api_key: None,
            enabled: true,
            weight,
        }
    }

    fn router_with(backends: Vec<BackendServer>) -> RowRouter {
        let n = backends.len();
        RowRouter {
            backends,
            client: reqwest::Client::builder().build().expect("client"),
            health: tokio::sync::Mutex::new(HashMap::new()),
            current: Mutex::new(vec![0i64; n]),
        }
    }

    #[test]
    fn equal_weights_round_robin() {
        let backends = vec![backend("omega", 1), backend("beta", 1), backend("delta", 1)];
        let router = router_with(backends.clone());
        let mut counts = std::collections::HashMap::new();
        for _ in 0..9 {
            let pick = router.select_weighted(&backends);
            *counts.entry(pick.id).or_insert(0) += 1;
        }
        assert_eq!(counts["omega"], 3);
        assert_eq!(counts["beta"], 3);
        assert_eq!(counts["delta"], 3);
    }

    #[test]
    fn weighted_two_to_one_split() {
        // omega weight 2, beta 1: over 9 picks, omega gets 6, beta 3, with
        // smooth (non-bursty) ordering.
        let backends = vec![backend("omega", 2), backend("beta", 1)];
        let router = router_with(backends.clone());
        let order: Vec<String> = (0..9)
            .map(|_| router.select_weighted(&backends).id)
            .collect();
        let omega = order.iter().filter(|id| *id == "omega").count();
        let beta = order.iter().filter(|id| *id == "beta").count();
        assert_eq!(omega, 6);
        assert_eq!(beta, 3);
        // Smooth: no three-in-a-row of the same backend.
        for w in order.windows(3) {
            assert!(!(w[0] == w[1] && w[1] == w[2]), "bursty run: {order:?}");
        }
        // Expected smooth pattern for 2:1.
        assert_eq!(
            order,
            ["omega", "beta", "omega", "omega", "beta", "omega", "omega", "beta", "omega"]
        );
    }

    #[test]
    fn zero_weight_clamped_to_one() {
        let backends = vec![backend("omega", 0), backend("beta", 1)];
        let router = router_with(backends.clone());
        let mut counts = std::collections::HashMap::new();
        for _ in 0..6 {
            let pick = router.select_weighted(&backends);
            *counts.entry(pick.id).or_insert(0) += 1;
        }
        // 0 weight behaves as 1: equal split, not starved.
        assert_eq!(counts["omega"], 3);
        assert_eq!(counts["beta"], 3);
    }

    #[test]
    fn selection_state_is_isolated_per_backend_position() {
        // Sanity: selecting over a subset that excludes an enabled backend
        // must not disturb the excluded backend's running weight.
        let backends = vec![backend("omega", 2), backend("beta", 1), backend("delta", 1)];
        let router = router_with(backends.clone());
        let subset = vec![backends[0].clone(), backends[1].clone()];
        for _ in 0..6 {
            router.select_weighted(&subset);
        }
        // Omega's running weight is a multiple of its own selections; the
        // excluded delta never moved from 0 and no panics occurred.
        let current = router.current.lock().unwrap();
        assert_eq!(current[2], 0);
    }

    #[tokio::test]
    async fn mark_unhealthy_puts_fresh_failure_in_cache() {
        // mark_unhealthy records a fresh unhealthy entry: the backend drops
        // out of the pick's healthy-set computation (which filters on
        // cache.healthy) even though its real /health may still pass.
        let backends = vec![backend("omega", 1), backend("beta", 1)];
        let router = router_with(backends.clone());
        router.mark_unhealthy("beta").await;

        let cache = router.health.lock().await;
        let entry = cache.get("beta").expect("beta entry present");
        assert!(!entry.healthy);
        // Fresh failure: within the short FAIL_TTL so the next pick() trusts
        // the exclusion without re-probing.
        assert!(entry.checked_at.elapsed() < FAIL_TTL);

        // The healthy-set computation excludes beta, leaving omega.
        let healthy: Vec<&BackendServer> = backends
            .iter()
            .filter(|b| cache.get(&b.id).map(|e| e.healthy).unwrap_or(true))
            .collect();
        assert_eq!(healthy.len(), 1);
        assert_eq!(healthy[0].id, "omega");
    }
}
