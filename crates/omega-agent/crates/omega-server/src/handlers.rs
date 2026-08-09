//! HTTP route handlers for the Omega public surface.

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::{Html, IntoResponse, Response},
    Json,
};
use chrono::Utc;
use serde_json::{json, Value};
use std::{
    path::Path,
    sync::Arc,
    time::{Duration, Instant},
};

use omega_common::config::{AppConfig, UpdatePeer};
use omega_common::metrics::MetricsStore;
use omega_common::types::{
    ChatRequest, CodingCapacitySnapshot, HealthResponse, Intent, LaneStatus, MetricRecord,
    OmegaModelConfig, Pool, PoolSnapshot, PublicStatus, QueueStatus,
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
<title>Omega · Ashat Neural Host Master Edition</title>\
<style>body{margin:0;background:#0d0d0f;color:#e9e9ee;font:16px system-ui;padding:3rem}\
a{color:#ff7a45}.pill{display:inline-block;padding:0.15rem 0.6rem;border-radius:999px;\
background:#1f1f25;color:#ff7a45;font:11px ui-monospace}</style></head>\
<body>\
<h1>Omega</h1>\
<p class=\"pill\">Ashat Neural Host · Master Edition</p>\
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
    Json(HealthResponse {
        status: "ok",
        uptime_seconds: state.started.elapsed().as_secs_f64(),
        orchestrator_ready,
        coding_agent_capacity: CodingCapacitySnapshot {
            ports_total: snapshot.ports_total,
            ports_active: snapshot.ports_active,
            queue_depth: snapshot.queue_depth,
            queue_limit: snapshot.queue_limit,
        },
    })
}

pub async fn public_status(State(state): State<Arc<AppState>>) -> Json<PublicStatus> {
    Json(status_snapshot(&state).await)
}

/// Snapshot shared by `/api/public_status` and the Alpha reporter's hub posts.
pub(crate) async fn status_snapshot(state: &Arc<AppState>) -> PublicStatus {
    let orchestrator_snap = state.orchestrator_pool.snapshot().await;
    let coding_snap = state.coding_agent_pool.snapshot().await;
    let metrics = state.metrics.summary(state.started.elapsed().as_secs_f64());
    let lane_omega = if state.orchestrator_pool.baseline_alive().await {
        let summary = &metrics.summaries.omega;
        // The Omega lane in 3-lane shape reflects the Coding Agent's 1.2B
        // model (which actually serves requests routed through the row
        // chain), not the intent router's GGUF.
        LaneStatus {
            label: "Omega".into(),
            model: state.coding_agent_pool.spec.model.display().to_string(),
            ctx: state.coding_agent_pool.spec.ctx,
            ..summary.clone()
        }
    } else {
        LaneStatus::placeholder("Omega")
    };
    let lane_beta = LaneStatus::placeholder("Beta");
    let lane_delta = LaneStatus::placeholder("Delta");
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
    Json(
        serde_json::to_value(&state.metrics.summary(state.started.elapsed().as_secs_f64()))
            .unwrap_or_else(|_| json!({})),
    )
}

pub async fn dashboard_timeseries(State(state): State<Arc<AppState>>) -> Json<Value> {
    Json(
        serde_json::to_value(
            &state
                .metrics
                .dashboard_timeseries(state.started.elapsed().as_secs_f64()),
        )
        .unwrap_or_else(|_| json!({})),
    )
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
            "owned_by": "ashat-vl450m-router",
            "purpose": "intent-classification",
            "model": "Omega v6",
            "engine": "ollama.cpp",
            "model_path": state.config.orchestrator_model.display().to_string(),
        }),
        json!({
            "id": coding_label,
            "owned_by": "ashat-1.2b-coding-agent",
            "purpose": "inference",
            "model": "Omega v6",
            "engine": "ollama.cpp",
            "model_path": state.config.inference_model.display().to_string(),
        }),
    ];

    // Add Omega v6 model variant from the config.
    let omega_v6 = OmegaModelConfig {
        name: "Omega-v6".to_string(),
        version: "6.0.0".to_string(),
        engine: "ollama.cpp".to_string(),
        model_path: state.config.orchestrator_model.display().to_string(),
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

    let started = Instant::now();
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

    let (intent, orchestrator_guard) = state
        .orchestrator
        .classify(&parsed.messages, &state.metrics)
        .await;
    drop(orchestrator_guard);

    let intent_label = intent.as_str();
    state.metrics.record(MetricRecord {
        timestamp: Utc::now().to_rfc3339(),
        pool: Pool::Orchestrator,
        intent: intent_label,
        target_port: 0,
        success: true,
        latency_ms: started.elapsed().as_secs_f64() * 1000.0,
        queue_wait_ms: 0.0,
        error_category: None,
    });

    // Advanced coding agent: `code` intent on the local lane runs the tool
    // loop; the other intents use the plain proxy paths below.
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
