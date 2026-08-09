//! Peer telemetry collector.
//!
//! Polls every enabled non-`omega` row-chain backend (Beta, Delta, ...) on a
//! background interval and caches its `/api/public_status`,
//! `/api/public_metrics`, and `/api/dashboard_timeseries` so the master's
//! telemetry endpoints can surface **real** slave data instead of offline
//! placeholders. Handlers read the cache (fast, no request-time network) and
//! merge the peers' local `omega` lane into the master's `beta` / `delta`
//! lanes. A peer that is unreachable simply contributes `None` fields, and
//! the UI falls back to the offline placeholder for that lane.

use omega_common::types::{BackendServer, PublicMetrics, PublicStatus, TimeseriesResponse};
use std::{collections::HashMap, sync::Arc, time::Duration};
use tokio::sync::RwLock;
use tracing::warn;

/// Cached snapshot of one peer's telemetry endpoints.
#[derive(Debug, Clone, Default)]
pub struct PeerSnapshot {
    pub status: Option<PublicStatus>,
    pub metrics: Option<PublicMetrics>,
    pub timeseries: Option<TimeseriesResponse>,
}

pub struct PeerTelemetry {
    peers: Vec<BackendServer>,
    client: reqwest::Client,
    inner: Arc<RwLock<HashMap<String, PeerSnapshot>>>,
}

impl PeerTelemetry {
    pub fn new(peers: Vec<BackendServer>) -> Self {
        Self {
            peers,
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(4))
                .build()
                .expect("reqwest client build"),
            inner: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Poll the peers immediately, then every `interval`. Runs forever.
    pub fn spawn(self: Arc<Self>, interval: Duration) {
        tokio::spawn(async move {
            loop {
                self.refresh().await;
                tokio::time::sleep(interval).await;
            }
        });
    }

    /// Backend ids this collector polls (`beta`, `delta`, ...).
    pub fn peer_ids(&self) -> Vec<String> {
        self.peers.iter().map(|peer| peer.id.clone()).collect()
    }

    /// Latest cached snapshots keyed by backend id (`beta`, `delta`, ...).
    /// Empty until the first poll completes (≈ one round-trip at boot).
    pub async fn snapshot(&self) -> HashMap<String, PeerSnapshot> {
        self.inner.read().await.clone()
    }

    async fn refresh(&self) {
        let results =
            futures::future::join_all(self.peers.iter().map(|peer| self.poll(peer))).await;
        let mut cache = self.inner.write().await;
        for (peer, snap) in self.peers.iter().zip(results) {
            cache.insert(peer.id.clone(), snap);
        }
    }

    async fn poll(&self, peer: &BackendServer) -> PeerSnapshot {
        let status = self
            .get_json::<PublicStatus>(peer, "/api/public_status")
            .await;
        let metrics = self
            .get_json::<PublicMetrics>(peer, "/api/public_metrics")
            .await;
        let timeseries = self
            .get_json::<TimeseriesResponse>(peer, "/api/dashboard_timeseries")
            .await;
        if status.is_none() {
            warn!(
                peer = %peer.id,
                host = %peer.host,
                port = peer.port,
                "peer telemetry unreachable"
            );
        }
        PeerSnapshot {
            status,
            metrics,
            timeseries,
        }
    }

    async fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        peer: &BackendServer,
        path: &str,
    ) -> Option<T> {
        let mut request = self
            .client
            .get(format!("http://{}:{}{}", peer.host, peer.port, path));
        if let Some(api_key) = peer.api_key.as_ref() {
            request = request.header("X-Ashat-Key", api_key);
        }
        request.send().await.ok()?.json::<T>().await.ok()
    }
}
