//! HTTP route handlers for the Omega public surface.

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::{Html, IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::Path,
    sync::Arc,
    time::{Duration, Instant},
};

use crate::peer_telemetry::{PeerSnapshot, PeerTelemetry};

use omega_common::config::{AppConfig, UpdatePeer};
use omega_common::metrics::MetricsStore;
use omega_common::types::{
    public_model_name, sanitize_model_name, AgentOperation, ChatRequest, CodingCapacitySnapshot,
    HealthResponse, Intent, LaneStatus, OmegaModelConfig, PoolSnapshot, PublicStatus,
    QueueStatus,
};
use omega_core::demand::DemandPool;
use omega_core::orchestrator::Orchestrator;
use omega_core::proxy::{CodingAgentProxy, CrossServerProxy};
use omega_core::router::RowRouter;
use omega_core::tool_loop::ToolLoop;

pub struct AppState {
    pub config: AppConfig,
    pub started: Instant,
    pub metrics: Arc<MetricsStore>,
    /// Cached telemetry from enabled row-chain backends (Beta, Delta).
    /// Background-polled by `PeerTelemetry`; handlers merge it into
    /// `public_status` / `public_metrics` / `dashboard_timeseries` so the
    /// frontend shows real slave lanes instead of offline placeholders.
    pub peer_telemetry: Arc<PeerTelemetry>,
    pub router: RowRouter,
    pub orchestrator: Orchestrator,
    pub coding_agent: CodingAgentProxy,
    pub coding_agent_pool: Arc<DemandPool>,
    pub orchestrator_pool: Arc<DemandPool>,
    /// Advanced coding agent (Phase 6): drives a held 1.2B slot through the
    /// tool loop when the orchestrator classifies intent as `code`.
    pub tool_loop: ToolLoop,
    /// Cross-server proxy used when the row chain picks a non-`omega`
    /// backend (Beta, Delta, ...). Inactive while all non-`omega`
    /// backends have `enabled: false` in `server-config.json`.
    pub cross_server: CrossServerProxy,
    /// Serializes `POST /api/admin/update` runs so concurrent POSTs cannot
    /// race on a peer's deploy (rsync + systemd restart on the slave).
    pub update_lock: tokio::sync::Mutex<()>,
}

pub async fn landing() -> Html<&'static str> {
    Html(
        "\
<!doctype html><html><head><meta charset=\"utf-8\">\
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
<title>Omega · Ashat Master Coding Agent</title>\
<style>body{margin:0;background:#0d0d0f;color:#e9e9ee;font:16px system-ui;padding:3rem}\
a{color:#ff7a45}.pill{display:inline-block;padding:0.15rem 0.6rem;border-radius:999px;\
background:#1f1f25;color:#ff7a45;font:11px ui-monospace}</style></head>\
<body>\
<h1>Omega</h1>\
<p class=\"pill\">Ashat Master Coding Agent</p>\
<p>Universal-source server. LFM2.5-VL-450M intent router + spawn-on-demand 1.2B Coding Agent pool.</p>\
<ul>\
<li><a href=\"/health\">/health</a></li>\
<li><a href=\"/api/public_status\">/api/public_status</a></li>\
<li><a href=\"/api/public_metrics\">/api/public_metrics</a></li>\
<li><a href=\"/api/dashboard_timeseries\">/api/dashboard_timeseries</a></li>\
<li><a href=\"/v1/models\">/v1/models</a></li>\
</ul>\
</body></html>",
    )
}

pub async fn health(State(state): State<Arc<AppState>>) -> Json<HealthResponse> {
    let orchestrator_ready = state.orchestrator_pool.baseline_alive().await;
    let snapshot = state.coding_agent_pool.snapshot().await;
    let metrics = state.metrics.summary(state.started.elapsed().as_secs_f64());
    Json(HealthResponse {
        status: "ok",
        uptime_seconds: state.started.elapsed().as_secs_f64(),
        orchestrator_ready,
        coding_agent_capacity: CodingCapacitySnapshot {
            ports_total: snapshot.ports_total,
            ports_active: snapshot.ports_active,
            queue_depth: snapshot.queue_depth,
            queue_limit: snapshot.queue_limit,
            memory_available_mb: memory_available_mb(),
            memory_pressure: memory_pressure(),
            worker_startup_latency_ms: snapshot.worker_startup_latency_ms,
            recent_failure_rate: (metrics.summaries.omega.total_requests > 0)
                .then(|| (100.0 - metrics.summaries.omega.success_rate) / 100.0),
            estimated_request_cost: (metrics.summaries.omega.total_requests > 0)
                .then_some(metrics.summaries.omega.avg_total_latency_ms),
        },
    })
}

fn memory_available_mb() -> Option<u64> {
    std::fs::read_to_string("/proc/meminfo").ok()?.lines().find(|line| line.starts_with("MemAvailable:"))?.split_whitespace().nth(1)?.parse::<u64>().ok().map(|kb| kb / 1024)
}

fn memory_pressure() -> Option<f64> {
    let available = memory_available_mb()? as f64;
    let total = std::fs::read_to_string("/proc/meminfo").ok()?.lines().find(|line| line.starts_with("MemTotal:"))?.split_whitespace().nth(1)?.parse::<f64>().ok()? / 1024.0;
    Some((1.0 - available / total).clamp(0.0, 1.0))
}

pub async fn public_status(State(state): State<Arc<AppState>>) -> Json<PublicStatus> {
    Json(status_snapshot(&state).await)
}

/// Snapshot shared by `/api/public_status` and the Alpha reporter's hub posts.
pub(crate) async fn status_snapshot(state: &Arc<AppState>) -> PublicStatus {
    let orchestrator_snap = state.orchestrator_pool.snapshot().await;
    let coding_snap = state.coding_agent_pool.snapshot().await;
    let metrics = state.metrics.summary(state.started.elapsed().as_secs_f64());
    let peer_snap = state.peer_telemetry.snapshot().await;
    let lane_omega = if state.orchestrator_pool.baseline_alive().await {
        let mut summary = metrics.summaries.omega.clone();
        // The Omega lane in 3-lane shape reflects the Coding Agent's 1.2B
        // model (which actually serves requests routed through the row
        // chain), not the intent router's GGUF. Public surface only ever
        // sees the basename — never the on-disk model path.
        summary.model = public_model_name(&state.coding_agent_pool.spec.model);
        summary.ctx = state.coding_agent_pool.spec.ctx;
        // Live baseline but no requests yet: the metrics placeholder reads
        // `offline` — surface reachability instead.
        if summary.total_requests == 0 {
            summary.lane_state = "online".to_owned();
            summary.ready = true;
            summary.available = true;
        }
        summary
    } else {
        LaneStatus::placeholder("Omega")
    };
    // Beta / Delta come from the peers' own local lanes (each slave's
    // `lanes.omega`), relabeled — real data once the collector has polled
    // them, offline placeholders otherwise.
    let lane_beta = peer_lane(&peer_snap, "beta", "Beta");
    let lane_delta = peer_lane(&peer_snap, "delta", "Delta");
    let all_ready = orchestrator_snap.baseline_alive && coding_snap.ports_total > 0;
    let coding_agent_pool_snapshot = PoolSnapshot {
        ports_total: coding_snap.ports_total,
        ports_active: coding_snap.ports_active,
        baseline_alive: true,
        extras_active: coding_snap.extras_active.clone(),
        free_ports: coding_snap.free_ports.clone(),
        queue_depth: coding_snap.queue_depth,
        queue_limit: coding_snap.queue_limit,
        last_failure_reason: coding_snap.last_failure_reason.clone(),
    };
    PublicStatus {
        uptime_seconds: state.started.elapsed().as_secs_f64(),
        llama_server_available: state.orchestrator_pool.baseline_alive().await,
        degraded: !orchestrator_snap.baseline_alive,
        queue: QueueStatus {
            depth: coding_snap.queue_depth,
            limit: coding_snap.queue_limit,
        },
        lanes: omega_common::types::Lanes {
            omega: lane_omega,
            beta: lane_beta,
            delta: lane_delta,
        },
        all_ready,
        orchestrator_pool: PoolSnapshot {
            ports_total: orchestrator_snap.ports_total,
            ports_active: orchestrator_snap.ports_active,
            baseline_alive: orchestrator_snap.baseline_alive,
            extras_active: orchestrator_snap.extras_active.clone(),
            free_ports: orchestrator_snap.free_ports.clone(),
            queue_depth: orchestrator_snap.queue_depth,
            queue_limit: orchestrator_snap.queue_limit,
            last_failure_reason: orchestrator_snap.last_failure_reason.clone(),
        },
        coding_agent_pool: coding_agent_pool_snapshot,
        lanes_in_use: coding_snap.ports_active as u32,
        lanes_capacity: coding_snap.ports_total as u32,
    }
}

pub async fn public_metrics(State(state): State<Arc<AppState>>) -> Json<Value> {
    let peers = state.peer_telemetry.snapshot().await;
    let mut value =
        serde_json::to_value(&state.metrics.summary(state.started.elapsed().as_secs_f64()))
            .unwrap_or_else(|_| json!({}));
    merge_peer_metrics(&mut value, &peers);
    Json(value)
}

pub async fn dashboard_timeseries(State(state): State<Arc<AppState>>) -> Json<Value> {
    let peers = state.peer_telemetry.snapshot().await;
    let mut value = serde_json::to_value(
        &state
            .metrics
            .dashboard_timeseries(state.started.elapsed().as_secs_f64()),
    )
    .unwrap_or_else(|_| json!({}));
    // Overlay each peer's local-lane frames under its lane key.
    for (id, key) in [("beta", "beta"), ("delta", "delta")] {
        if let Some(peer) = peers.get(id).and_then(|snap| snap.timeseries.as_ref()) {
            if let Ok(frames) = serde_json::to_value(&peer.omega) {
                value[key] = frames;
            }
        }
    }
    // Peer events (tagged with the lane id) first, then the master's own.
    let mut merged: Vec<Value> = Vec::new();
    for (id, peer) in &peers {
        if let Some(ts) = peer.timeseries.as_ref() {
            for event in &ts.events {
                merged.push(json!({ "event": format!("[{id}] {}", event.event) }));
            }
        }
    }
    if let Some(events) = value.get_mut("events").and_then(|e| e.as_array_mut()) {
        merged.extend(events.iter().cloned());
        *events = merged;
    }
    Json(value)
}

/// Merge each peer's local-lane summary + recent events into the master's
/// `/api/public_metrics` payload under the matching lane key, relabeled.
fn merge_peer_metrics(value: &mut Value, peers: &HashMap<String, PeerSnapshot>) {
    for (id, label) in [("beta", "Beta"), ("delta", "Delta")] {
        let Some(peer) = peers.get(id).and_then(|snap| snap.metrics.as_ref()) else {
            continue;
        };
        let mut lane = serde_json::to_value(&peer.summaries.omega).unwrap_or_else(|_| json!({}));
        if let Some(obj) = lane.as_object_mut() {
            obj.insert("label".into(), json!(label));
            // Defensive: an older slave build could still report a full model
            // path; never forward filesystem locations to the public surface.
            if let Some(model) = obj.get("model").and_then(|m| m.as_str()) {
                obj.insert("model".into(), json!(sanitize_model_name(model)));
            }
        }
        if let Some(obj) = value.get_mut("summaries").and_then(|s| s.as_object_mut()) {
            obj.insert(id.into(), lane);
        }
    }
    let mut merged: Vec<Value> = Vec::new();
    for (id, peer) in peers {
        if let Some(metrics) = peer.metrics.as_ref() {
            for event in metrics.recent_events.iter().rev() {
                merged.push(json!(format!("[{id}] {event}")));
            }
        }
    }
    if let Some(events) = value
        .get_mut("recent_events")
        .and_then(|e| e.as_array_mut())
    {
        merged.extend(events.iter().cloned());
        *events = merged;
    }
}

/// A peer's own `omega` lane (the slave's local serving lane) mapped into the
/// master's `beta`/`delta` lane slot, relabeled. Offline placeholder when the
/// peer is absent or unreachable.
fn peer_lane(peers: &HashMap<String, PeerSnapshot>, id: &str, label: &str) -> LaneStatus {
    peers
        .get(id)
        .and_then(|snap| snap.status.as_ref())
        .map(|status| {
            let mut lane = status.lanes.omega.clone();
            lane.label = label.to_owned();
            // Public surface only sees the basename; never forward a path an
            // older slave build might have reported.
            lane.model = sanitize_model_name(&lane.model);
            // The slave's own lane reads `offline` until its first request
            // (metrics placeholder), but a reachable peer with a live
            // baseline is genuinely online — surface that instead.
            if lane.total_requests == 0 && status.orchestrator_pool.baseline_alive {
                lane.lane_state = "online".to_owned();
                lane.ready = true;
                lane.available = true;
            }
            lane
        })
        .unwrap_or_else(|| LaneStatus::placeholder(label))
}

/// `POST /api/admin/update` — propagate the current master build to every
/// enabled peer via `scripts/seed_slave.sh`. Runs peers sequentially; each
/// peer's output is truncated to its tail for the report. Auth-gated by the
/// shared `X-Ashat-Key` (same middleware as chat). Can take minutes: each
/// peer is allowed `update.timeout_seconds` (default 600).
pub async fn admin_update(State(state): State<Arc<AppState>>) -> Response {
    let peers: Vec<UpdatePeer> = state
        .config
        .update
        .peers
        .iter()
        .filter(|p| p.enabled)
        .cloned()
        .collect();
    if peers.is_empty() {
        return Json(json!({
            "status": "noop",
            "message": "no enabled peers configured",
            "peers": [],
        }))
        .into_response();
    }

    let _guard = state.update_lock.lock().await;
    let script = state
        .config
        .project_root
        .join("scripts")
        .join("seed_slave.sh");
    let budget = Duration::from_secs(state.config.update.timeout_seconds);

    let mut results = Vec::new();
    let mut any_failed = false;
    for peer in &peers {
        let started = Instant::now();
        let outcome = run_seed_script(&script, &state.config.project_root, peer, budget).await;
        let elapsed = started.elapsed().as_secs_f64();
        if outcome.status != "ok" {
            any_failed = true;
        }
        results.push(json!({
            "host": peer.host,
            "install": peer.install,
            "port": peer.port,
            "status": outcome.status,
            "elapsed_seconds": elapsed,
            "output_tail": outcome.output,
        }));
    }

    Json(json!({
        "status": if any_failed { "partial" } else { "ok" },
        "peers": results,
    }))
    .into_response()
}

struct SeedOutcome {
    status: &'static str,
    output: String,
}

/// Run `seed_slave.sh <host> <install> <port>` and capture the output tail.
/// On budget expiry the child is killed (kill_on_drop) and reported as
/// `timeout`. Exit code 0 from the script is the success signal (it exits 1
/// on deploy or rollback failure).
async fn run_seed_script(
    script: &Path,
    project_root: &Path,
    peer: &UpdatePeer,
    budget: Duration,
) -> SeedOutcome {
    let child = tokio::process::Command::new(script)
        .arg(&peer.host)
        .arg(&peer.install)
        .arg(peer.port.to_string())
        .current_dir(project_root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn();
    let child = match child {
        Ok(c) => c,
        Err(err) => {
            return SeedOutcome {
                status: "error",
                output: format!("failed to spawn seed_slave.sh: {err}"),
            }
        }
    };
    let output = match tokio::time::timeout(budget, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(err)) => {
            return SeedOutcome {
                status: "error",
                output: format!("seed process error: {err}"),
            }
        }
        Err(_) => {
            return SeedOutcome {
                status: "timeout",
                output: format!(
                    "seed exceeded the {}s budget; child killed (rollback copy is in place on the peer)",
                    budget.as_secs()
                ),
            }
        }
    };
    let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !stderr.trim().is_empty() {
        text.push_str(&format!("\n[stderr]\n{stderr}"));
    }
    let status = if output.status.success() {
        "ok"
    } else {
        "failed"
    };
    SeedOutcome {
        status,
        output: truncate_tail(&text, 4096),
    }
}

/// `POST /api/admin/github_sync` — verified bidirectional GitHub sync.
/// Body: `{"mode": "status"|"pull"|"push"}` (default `status`). Runs
/// `scripts/github_sync.sh <mode> --json --yes` and returns its JSON report
/// (direction, ahead/behind, commit + file manifests, tracked-secret check).
/// Admin-key gated. `pull` takes the update lock: its propagate step seeds
/// peers via `seed_slave.sh`, same as `/api/admin/update`.
pub async fn github_sync(State(state): State<Arc<AppState>>, request: Request<Body>) -> Response {
    let raw = match axum::body::to_bytes(request.into_body(), 16 * 1024).await {
        Ok(bytes) => bytes,
        Err(_) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"message": "Invalid JSON body", "type": "invalid_request"}})),
            )
                .into_response();
        }
    };
    let mode = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|v| v.get("mode").and_then(|m| m.as_str()).map(str::to_owned))
        .unwrap_or_else(|| "status".to_owned());
    let mode = match mode.as_str() {
        "status" => "status",
        "pull" => "pull",
        "push" => "push",
        other => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"message": format!("unknown mode: {other}"), "type": "invalid_request"}})),
            )
                .into_response();
        }
    };

    let script = state
        .config
        .project_root
        .join("scripts")
        .join("github_sync.sh");
    let budget = Duration::from_secs(match mode {
        "status" => 90,
        "pull" => 1500,
        _ => 180,
    });

    // pull propagates to peers via seed_slave.sh — serialize with the admin
    // update endpoint so a peer never gets two seeds at once.
    let _guard = if mode == "pull" {
        Some(state.update_lock.lock().await)
    } else {
        None
    };

    let started = Instant::now();
    let outcome = run_script_capture(
        &script,
        &[mode, "--json", "--yes"],
        &state.config.project_root,
        budget,
    )
    .await;
    let elapsed = started.elapsed().as_secs_f64();

    let report: Value = serde_json::from_str(&outcome.stdout).unwrap_or_else(|_| json!({
        "ok": outcome.success,
        "message": format!("script produced no JSON report: {}", truncate_tail(&outcome.stderr, 300)),
    }));

    let mut combined = outcome.stdout.clone();
    if !outcome.stderr.trim().is_empty() {
        combined.push_str(&format!("\n[stderr]\n{}", &outcome.stderr));
    }

    Json(json!({
        "mode": mode,
        "status": if outcome.success { "ok" } else { "failed" },
        "elapsed_seconds": elapsed,
        "report": report,
        "output_tail": truncate_tail(&combined, 4096),
    }))
    .into_response()
}

/// Run an arbitrary script with args, capturing stdout/stderr separately with
/// a kill-on-timeout budget.
struct ScriptOutcome {
    success: bool,
    stdout: String,
    stderr: String,
}

async fn run_script_capture(
    script: &Path,
    args: &[&str],
    cwd: &Path,
    budget: Duration,
) -> ScriptOutcome {
    let child = tokio::process::Command::new(script)
        .args(args)
        .current_dir(cwd)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn();
    let child = match child {
        Ok(c) => c,
        Err(err) => {
            return ScriptOutcome {
                success: false,
                stdout: String::new(),
                stderr: format!("failed to spawn {script:?}: {err}"),
            }
        }
    };
    let output = match tokio::time::timeout(budget, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(err)) => {
            return ScriptOutcome {
                success: false,
                stdout: String::new(),
                stderr: format!("script error: {err}"),
            }
        }
        Err(_) => {
            return ScriptOutcome {
                success: false,
                stdout: String::new(),
                stderr: format!(
                    "script exceeded the {}s budget; child killed",
                    budget.as_secs()
                ),
            }
        }
    };
    ScriptOutcome {
        success: output.status.success(),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }
}

/// Keep the last `max` characters of a script run for the report.
fn truncate_tail(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_owned()
    } else {
        let tail: String = s
            .chars()
            .rev()
            .take(max)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        format!("...[truncated]\n{tail}")
    }
}

pub async fn list_models(State(state): State<Arc<AppState>>) -> Json<Value> {
    let orchestrator_label = state
        .orchestrator_pool
        .spec
        .model
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("orchestrator");
    let coding_label = state
        .coding_agent_pool
        .spec
        .model
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("coding-agent");

    // Build the model list with Omega v6 support.
    let mut models = vec![
        json!({
            "id": orchestrator_label,
            "owned_by": "ashat-350m-router",
            "purpose": "intent-classification",
            "model": "Omega v6",
            "engine": "ollama.cpp",
            "model_path": public_model_name(&state.config.orchestrator_model),
        }),
        json!({
            "id": coding_label,
            "owned_by": "ashat-1.2b-coding-agent",
            "purpose": "inference",
            "model": "Omega v6",
            "engine": "ollama.cpp",
            "model_path": public_model_name(&state.config.inference_model),
        }),
    ];

    // Add Omega v6 model variant from the config.
    let omega_v6 = OmegaModelConfig {
        name: "Omega-v6".to_string(),
        version: "6.0.0".to_string(),
        engine: "ollama.cpp".to_string(),
        model_path: public_model_name(&state.config.orchestrator_model),
        description: "Omega v6 model optimized for ollama.cpp inference".to_string(),
    };
    models.push(json!(omega_v6));

    Json(json!({
        "object": "list",
        "data": models,
    }))
}

pub async fn chat(State(state): State<Arc<AppState>>, request: Request<Body>) -> Response {
    let raw =
        match axum::body::to_bytes(request.into_body(), 1_048_576 + 1).await {
            Ok(bytes) => bytes,
            Err(_) => return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"message": "Invalid JSON body", "type": "invalid_request"}})),
            )
                .into_response(),
        };
    if raw.len() > 1_048_576 {
        return (
            StatusCode::BAD_REQUEST,
            Json(
                json!({"error": {"message": "Request body too large", "type": "invalid_request"}}),
            ),
        )
            .into_response();
    }

    let parsed: ChatRequest =
        match serde_json::from_slice(&raw) {
            Ok(v) => v,
            Err(_) => return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"message": "Invalid JSON body", "type": "invalid_request"}})),
            )
                .into_response(),
        };

    if parsed.messages.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": {"message": "Missing or empty messages", "type": "invalid_request"}})),
        )
            .into_response();
    }

    let stream_mode = parsed.stream.unwrap_or(false);

    // Walk the row chain first; we want to reject early if the routing layer
    // itself can't even point at a backend. Phase 1 always resolves to
    // omega; Beta/Delta light up by flipping `enabled: true` in
    // `server-config.json`.
    let backend = match state.router.pick().await {
        Ok(b) => b,
        Err(_) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": {"message": "row chain exhausted", "type": "routing_error"}})),
            )
                .into_response();
        }
    };

    // Operation is declared by the caller; routing never infers intent from prose.
    let intent = match parsed.operation {
        Some(AgentOperation::Agent) => Intent::Code,
        Some(AgentOperation::Chat) | Some(AgentOperation::Vision) | None => Intent::Chat,
    };
    let intent_label = intent.as_str();

    // Agent operations use the local coding lane's structured tool loop.
    if backend.id == "omega" && intent == Intent::Code {
        return forward_tool_loop(&state, &parsed, stream_mode).await;
    }

    // Route by backend identity: omega goes through the local spawn-on-demand
    // Coding Agent pool (lane = serving port); anything else (beta, delta)
    // goes through the cross-server HTTP forwarder (lane = backend.id).
    if backend.id == "omega" {
        return forward_local(&state, &parsed, stream_mode, intent_label).await;
    }

    // Cross-server path with one retry: a slave can die between its health
    // probe and this forward (the TTL cache may still call it healthy). On a
    // connection failure we mark it unhealthy so it drops out of the
    // rotation, then re-pick once and retry — restoring the old per-request
    // failover guarantee that a down backend never causes a long run of 503s.
    match forward_cross_server(&state, &parsed, stream_mode, intent_label, &backend).await {
        Ok(resp) => resp,
        Err(err) if !matches!(&err, omega_common::types::ProxyError::Connection(_)) => {
            error_response(err)
        }
        Err(err) => {
            state.router.mark_unhealthy(&backend.id).await;
            match state.router.pick().await {
                Ok(next) if next.id != backend.id => {
                    match forward_cross_server(&state, &parsed, stream_mode, intent_label, &next)
                        .await
                    {
                        Ok(resp) => resp,
                        Err(err) => error_response(err),
                    }
                }
                _ => error_response(err),
            }
        }
    }
}

/// Code-intent path: the advanced coding agent runs its tool loop and returns
/// the final answer with the Script Validation Engine report attached. The
/// loop itself is non-streaming; a `stream: true` client gets the final answer
/// as a single SSE event.
async fn forward_tool_loop(
    state: &Arc<AppState>,
    parsed: &ChatRequest,
    stream_mode: bool,
) -> Response {
    match state.tool_loop.run(parsed, &state.metrics).await {
        Ok(response) => {
            if stream_mode {
                let payload = serde_json::to_string(&response).unwrap_or_else(|_| "{}".to_owned());
                let body = format!("data: {payload}\n\ndata: [DONE]\n\n");
                let mut resp = Response::new(axum::body::Body::from(body));
                resp.headers_mut().insert(
                    axum::http::header::CONTENT_TYPE,
                    axum::http::HeaderValue::from_static("text/event-stream"),
                );
                resp
            } else {
                Json(response).into_response()
            }
        }
        Err(err) => error_response(err),
    }
}

/// Local path: omega's lane is the spawn-on-demand Coding Agent pool's
/// serving port.
async fn forward_local(
    state: &Arc<AppState>,
    parsed: &ChatRequest,
    stream_mode: bool,
    intent_label: &'static str,
) -> Response {
    if stream_mode {
        match state
            .coding_agent
            .forward_streaming(parsed, intent_label, &state.metrics)
            .await
        {
            Ok(stream) => {
                // stream is a GuardedStream that already emits llama-server's
                // raw SSE bytes (`data: {...}\n\n` per chunk, plus a final
                // `data: [DONE]\n\n`). We pass them through verbatim into the
                // response body. Wrapping them as `Sse::new(Event::default()
                // .data(bytes))` would emit `data: data: {...}` — double
                // prefix — so we stream raw bytes via `Body::from_stream`
                // with our own SSE headers.
                let body = Response::new(axum::body::Body::from_stream(stream));
                let mut resp = body;
                resp.headers_mut().insert(
                    axum::http::header::CONTENT_TYPE,
                    axum::http::HeaderValue::from_static("text/event-stream"),
                );
                resp.headers_mut().insert(
                    "cache-control",
                    axum::http::HeaderValue::from_static("no-cache"),
                );
                resp
            }
            Err(err) => error_response(err),
        }
    } else {
        match state
            .coding_agent
            .forward(parsed, intent_label, &state.metrics)
            .await
        {
            Ok((response, guard)) => {
                drop(guard);
                Json(response).into_response()
            }
            Err(err) => error_response(err),
        }
    }
}

/// Cross-server path: beta / delta / other row-chain backends are
/// upstream llama-servers on a remote host. The lane on each response
/// is the backend id (e.g. "beta") so Ashat Hub can distinguish
/// local (port-suffixed) from external (id-only) lanes.
///
/// Returns `Err` on proxy failure instead of materializing an error response
/// so the caller can retry with a different backend; a `Connection` error
/// means nothing was written to the client yet, so retrying is safe even for
/// streaming requests.
async fn forward_cross_server(
    state: &Arc<AppState>,
    parsed: &ChatRequest,
    stream_mode: bool,
    intent_label: &'static str,
    backend: &omega_common::types::BackendServer,
) -> Result<Response, omega_common::types::ProxyError> {
    if stream_mode {
        let stream = state
            .cross_server
            .forward_streaming(parsed, intent_label, backend, &state.metrics)
            .await?;
        let body = Response::new(axum::body::Body::from_stream(stream));
        let mut resp = body;
        resp.headers_mut().insert(
            axum::http::header::CONTENT_TYPE,
            axum::http::HeaderValue::from_static("text/event-stream"),
        );
        resp.headers_mut().insert(
            "cache-control",
            axum::http::HeaderValue::from_static("no-cache"),
        );
        Ok(resp)
    } else {
        let response = state
            .cross_server
            .forward(parsed, intent_label, backend, &state.metrics)
            .await?;
        Ok(Json(response).into_response())
    }
}

fn error_response(err: omega_common::types::ProxyError) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"message": err.to_string(), "type": "routing_error"}})),
    )
        .into_response()
}
