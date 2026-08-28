//! In-memory + JSONL-backed metrics store for the Omega server.
//!
//! Records a sliding-window [MetricRecord] trail, derives per-lane summaries,
//! and persists every record to a JSONL file under `logs/`.

use crate::types::{
    LaneStatus, Lanes, MetricRecord, Pool, PublicMetrics, TelemetryFrame, TimeseriesEvent,
    TimeseriesResponse,
};
use serde::Serialize;
use std::{
    collections::{HashMap, VecDeque},
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    },
    time::Instant,
};

const IN_MEMORY_CAP: usize = 1024;

/// A coding-agent run currently in progress, with an estimated completion
/// countdown derived from the rolling average latency.
#[derive(Debug, Clone, Serialize)]
pub struct ActiveRunEta {
    pub port: u16,
    pub elapsed_seconds: f64,
    pub eta_seconds: f64,
}

#[derive(Debug)]
pub struct MetricsStore {
    records: Mutex<VecDeque<MetricRecord>>,
    persist_path: PathBuf,
    recent_events: Mutex<VecDeque<String>>,
    active_runs: Mutex<HashMap<u16, Instant>>,
    active_requests: AtomicUsize,
}

impl MetricsStore {
    pub fn open(persist_path: &Path) -> Self {
        if let Some(parent) = persist_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        Self {
            records: Mutex::new(VecDeque::with_capacity(IN_MEMORY_CAP)),
            persist_path: persist_path.to_path_buf(),
            recent_events: Mutex::new(VecDeque::with_capacity(64)),
            active_runs: Mutex::new(HashMap::new()),
            active_requests: AtomicUsize::new(0),
        }
    }

    /// Append a record. Persists to JSONL and updates the rolling in-memory trail.
    pub fn record(&self, rec: MetricRecord) {
        if let Ok(line) = serde_json::to_string(&rec) {
            if let Ok(mut file) = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.persist_path)
            {
                let _ = writeln!(file, "{line}");
            }
        }

        let mut guard = self.records.lock().expect("metrics lock poisoned");
        if guard.len() == IN_MEMORY_CAP {
            guard.pop_front();
        }
        guard.push_back(rec);
    }

    /// Append a human-readable event (e.g. "spawned new 1.2B instance on :18081").
    pub fn event(&self, message: impl Into<String>) {
        let mut guard = self.recent_events.lock().expect("events lock poisoned");
        if guard.len() == 64 {
            guard.pop_front();
        }
        guard.push_back(message.into());
    }

    /// Last `n` events, oldest-first.
    pub fn recent_events(&self, n: usize) -> Vec<String> {
        let guard = self.recent_events.lock().expect("events lock poisoned");
        guard.iter().rev().take(n).cloned().collect::<Vec<_>>()
    }

    pub fn summary(&self, uptime_seconds: f64) -> PublicMetrics {
        let guard = self.records.lock().expect("metrics lock poisoned");
        let total = guard.len();
        let requests_last_5m = guard
            .iter()
            .filter(|rec| {
                chrono::DateTime::parse_from_rfc3339(&rec.timestamp)
                    .map(|at| {
                        (chrono::Utc::now() - at.with_timezone(&chrono::Utc)).num_seconds() < 300
                    })
                    .unwrap_or(false)
            })
            .count();

        let mut omega = LaneAccumulator::default();
        let beta = LaneAccumulator::default();
        let delta = LaneAccumulator::default();

        for rec in guard.iter() {
            if matches!(rec.pool, Pool::CodingAgent) {
                omega.observe(rec);
            }
        }

        let omega_status = omega.to_lane_status("Omega");
        let beta_status = beta.to_lane_status("Beta");
        let delta_status = delta.to_lane_status("Delta");

        let events = self.recent_events(20);
        PublicMetrics {
            uptime_seconds,
            summaries: Lanes {
                omega: omega_status,
                beta: beta_status,
                delta: delta_status,
            },
            active_requests: self.active_requests(),
            requests_last_5m,
            total_events: total,
            recent_events: events,
        }
    }

    pub fn request_started(&self) {
        self.active_requests.fetch_add(1, Ordering::Relaxed);
    }

    pub fn request_finished(&self) {
        self.active_requests.fetch_sub(1, Ordering::Relaxed);
    }

    pub fn active_requests(&self) -> usize {
        self.active_requests.load(Ordering::Relaxed)
    }

    /// Record the start of a coding-agent run (tool-loop / project sessions).
    pub fn mark_run_start(&self, port: u16) {
        if let Ok(mut guard) = self.active_runs.lock() {
            guard.insert(port, Instant::now());
        }
    }

    /// Record the end of a coding-agent run.
    pub fn mark_run_finish(&self, port: u16) {
        if let Ok(mut guard) = self.active_runs.lock() {
            guard.remove(&port);
        }
    }

    /// Estimated seconds remaining for each active run. Stale entries (runs
    /// whose end couldn't be hooked, e.g. dropped streams) are pruned using
    /// the rolling average latency.
    pub fn active_run_etas(&self) -> Vec<ActiveRunEta> {
        let avg_ms = {
            let guard = self.records.lock().expect("metrics lock poisoned");
            let coding: Vec<&MetricRecord> = guard
                .iter()
                .filter(|r| matches!(r.pool, Pool::CodingAgent))
                .collect();
            if coding.is_empty() {
                // No completed run to reference yet: fall back to a 60 s
                // default so the first run still shows an elapsed + ETA
                // countdown on the Hub instead of vanishing.
                60_000.0
            } else {
                let sum: f64 = coding.iter().map(|r| r.latency_ms).sum();
                sum / coding.len() as f64
            }
        };

        let mut out = Vec::new();
        let mut stale = Vec::new();
        {
            let mut guard = self.active_runs.lock().expect("metrics lock poisoned");
            let now = Instant::now();
            for (&port, &start) in guard.iter() {
                let elapsed = now.duration_since(start).as_secs_f64();
                // Give a run 3x the average latency + a minute before we
                // consider its end untracked.
                if elapsed > (avg_ms / 1000.0) * 3.0 + 60.0 {
                    stale.push(port);
                    continue;
                }
                let eta = ((avg_ms / 1000.0) - elapsed).max(0.0);
                out.push(ActiveRunEta {
                    port,
                    elapsed_seconds: elapsed,
                    eta_seconds: eta,
                });
            }
            for port in stale {
                guard.remove(&port);
            }
        }
        out
    }

    pub fn dashboard_timeseries(&self, _uptime_seconds: f64) -> TimeseriesResponse {
        let guard = self.records.lock().expect("metrics lock poisoned");
        let mut frames: Vec<TelemetryFrame> = guard
            .iter()
            .filter(|r| matches!(r.pool, Pool::CodingAgent))
            .map(|r| TelemetryFrame {
                timestamp: r.timestamp.clone(),
                generation_tokens_per_second: (r.completion_tokens > 0 && r.latency_ms > 0.0)
                    .then(|| r.completion_tokens as f64 / (r.latency_ms / 1000.0)),
                prompt_tokens_per_second: (r.prompt_tokens > 0 && r.latency_ms > 0.0)
                    .then(|| r.prompt_tokens as f64 / (r.latency_ms / 1000.0)),
                total_latency_ms: r.latency_ms,
                time_to_first_token_ms: r.time_to_first_token_ms,
                success: r.success,
            })
            .collect();

        frames.sort_by(|a, b| a.timestamp.cmp(&b.timestamp));

        let events: Vec<TimeseriesEvent> = self
            .recent_events(64)
            .into_iter()
            .rev()
            .map(|event| TimeseriesEvent { event })
            .collect();

        TimeseriesResponse {
            omega: frames,
            beta: Vec::new(),
            delta: Vec::new(),
            events,
        }
    }
}

#[derive(Default, Debug)]
struct LaneAccumulator {
    total_requests: u64,
    success_count: u64,
    failure_count: u64,
    total_latency_ms: f64,
    prompt_tokens: u64,
    completion_tokens: u64,
    generation_rate_sum: f64,
    prompt_rate_sum: f64,
    quickest_generation_rate: f64,
    slowest_generation_rate: f64,
    quickest_latency_ms: f64,
    slowest_latency_ms: f64,
    latest_latency_ms: f64,
    last_request_time: Option<String>,
    last_failure_code: Option<String>,
}

impl LaneAccumulator {
    fn observe(&mut self, rec: &MetricRecord) {
        self.total_requests += 1;
        if rec.success {
            self.success_count += 1;
        } else {
            self.failure_count += 1;
            self.last_failure_code = rec.error_category.clone();
        }
        self.total_latency_ms += rec.latency_ms;
        self.prompt_tokens += rec.prompt_tokens as u64;
        self.completion_tokens += rec.completion_tokens as u64;
        if rec.latency_ms > 0.0 {
            let generation_rate = rec.completion_tokens as f64 / (rec.latency_ms / 1000.0);
            self.generation_rate_sum += generation_rate;
            self.prompt_rate_sum += rec.prompt_tokens as f64 / (rec.latency_ms / 1000.0);
            if self.total_requests == 1 || generation_rate < self.slowest_generation_rate {
                self.slowest_generation_rate = generation_rate;
            }
            if generation_rate > self.quickest_generation_rate {
                self.quickest_generation_rate = generation_rate;
            }
        }
        if self.total_requests == 1 || rec.latency_ms < self.quickest_latency_ms {
            self.quickest_latency_ms = rec.latency_ms;
        }
        if rec.latency_ms > self.slowest_latency_ms {
            self.slowest_latency_ms = rec.latency_ms;
        }
        self.latest_latency_ms = rec.latency_ms;
        self.last_request_time = Some(rec.timestamp.clone());
    }

    fn to_lane_status(&self, label: &str) -> LaneStatus {
        if self.total_requests == 0 {
            return LaneStatus::placeholder(label);
        }
        let avg_latency = self.total_latency_ms / self.total_requests as f64;
        let success_rate = (self.success_count as f64 / self.total_requests as f64) * 100.0;
        LaneStatus {
            label: label.to_owned(),
            model: "LFM2.5-1.2B-Instruct-Q4_K_M.gguf".to_owned(),
            ctx: 4096,
            available: self.failure_count < self.success_count,
            ready: true,
            lane_state: if self.failure_count > self.success_count {
                "degraded".to_owned()
            } else {
                "online".to_owned()
            },
            total_requests: self.total_requests,
            success_rate,
            total_prompt_tokens: self.prompt_tokens,
            total_completion_tokens: self.completion_tokens,
            quickest_generation_tokens_per_second: self.quickest_generation_rate,
            slowest_generation_tokens_per_second: self.slowest_generation_rate,
            latest_generation_tokens_per_second: if self.latest_latency_ms > 0.0 {
                self.completion_tokens as f64 / (self.latest_latency_ms / 1000.0)
            } else {
                0.0
            },
            avg_generation_tokens_per_second: self.generation_rate_sum / self.total_requests as f64,
            avg_prompt_tokens_per_second: self.prompt_rate_sum / self.total_requests as f64,
            avg_total_latency_ms: avg_latency,
            last_time_to_first_token_ms: None,
            avg_time_to_first_token_ms: None,
            last_request_time: self.last_request_time.clone(),
            last_failure_code: self.last_failure_code.clone(),
            reason_message: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn active_run_eta_tracks_start_and_finish() {
        let store = MetricsStore::open(&std::env::temp_dir().join(format!(
            "omega-metrics-eta-test-{}.jsonl",
            std::process::id()
        )));
        // Seed a rolling average so the ETA has a reference latency.
        store.record(MetricRecord {
            timestamp: "2026-01-01T00:00:00Z".to_owned(),
            pool: Pool::CodingAgent,
            intent: "chat",
            target_port: 18280,
            success: true,
            latency_ms: 5_000.0,
            queue_wait_ms: 0.0,
            prompt_tokens: 0,
            completion_tokens: 0,
            time_to_first_token_ms: None,
            error_category: None,
        });
        store.mark_run_start(18280);
        let etas = store.active_run_etas();
        assert_eq!(etas.len(), 1);
        assert_eq!(etas[0].port, 18280);
        assert!(etas[0].eta_seconds > 0.0 && etas[0].eta_seconds <= 5.0);
        store.mark_run_finish(18280);
        assert!(store.active_run_etas().is_empty());
    }
}
