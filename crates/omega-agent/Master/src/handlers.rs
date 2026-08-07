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
use std::{sync::Arc, time::Instant};

use crate::config::AppConfig;
use crate::demand::DemandPool;
use crate::metrics::MetricsStore;
use crate::orchestrator::Orchestrator;
use crate::proxy::{CodingAgentProxy, CrossServerProxy};
use crate::router::RowRouter;
use crate::types::{
    ChatRequest, CodingCapacitySnapshot, HealthResponse, LaneStatus, MetricRecord,
    Pool, PoolSnapshot, PublicStatus, QueueStatus,
};

pub struct AppState {
    pub config: AppConfig,
    pub started: Instant,
    pub metrics: Arc<MetricsStore>,
    pub router: RowRouter,
    pub orchestrator: Orchestrator,
    pub coding_agent: CodingAgentProxy,
    pub coding_agent_pool: Arc<DemandPool>,
    pub orchestrator_pool: Arc<DemandPool>,
    /// Cross-server proxy used when the row chain picks a non-`omega`
    /// backend (Beta, Delta, ...). Inactive while all non-`omega`
    /// backends have `enabled: false` in `server-config.json`.
    pub cross_server: CrossServerProxy,
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
<p>Universal-source server. 230M orchestrator + spawn-on-demand 1.2B Coding Agent pool.</p>\
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
    let orchestrator_snap = state.orchestrator_pool.snapshot().await;
    let coding_snap = state.coding_agent_pool.snapshot().await;
    let metrics = state.metrics.summary(state.started.elapsed().as_secs_f64());
    let lane_omega = if state.orchestrator_pool.baseline_alive().await {
        let summary = &metrics.summaries.omega;
        // The Omega lane in 3-lane shape reflects the Coding Agent's 1.2B
        // model (which actually serves requests routed through the row
        // chain), not the 230M Orchestrator's GGUF.
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
    Json(PublicStatus {
        uptime_seconds: state.started.elapsed().as_secs_f64(),
        llama_server_available: state.orchestrator_pool.baseline_alive().await,
        degraded: !orchestrator_snap.baseline_alive,
        queue: QueueStatus {
            depth: coding_snap.queue_depth,
            limit: coding_snap.queue_limit,
        },
        lanes: crate::types::Lanes {
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
    })
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
    Json(json!({
        "object": "list",
        "data": [
            {"id": orchestrator_label, "owned_by": "ashat-230m-orchestrator", "purpose": "intent-classification"},
            {"id": coding_label,       "owned_by": "ashat-1.2b-coding-agent", "purpose": "inference"}
        ]
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

    // Route by backend identity: omega goes through the local spawn-on-demand
    // Coding Agent pool (lane = serving port); anything else (beta, delta)
    // goes through the cross-server HTTP forwarder (lane = backend.id).
    if backend.id == "omega" {
        forward_local(&state, &parsed, stream_mode, intent_label).await
    } else {
        forward_cross_server(&state, &parsed, stream_mode, intent_label, &backend).await
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
                resp.headers_mut()
                    .insert("cache-control", axum::http::HeaderValue::from_static("no-cache"));
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
async fn forward_cross_server(
    state: &Arc<AppState>,
    parsed: &ChatRequest,
    stream_mode: bool,
    intent_label: &'static str,
    backend: &crate::types::BackendServer,
) -> Response {
    if stream_mode {
        match state
            .cross_server
            .forward_streaming(parsed, intent_label, backend, &state.metrics)
            .await
        {
            Ok(stream) => {
                let body = Response::new(axum::body::Body::from_stream(stream));
                let mut resp = body;
                resp.headers_mut().insert(
                    axum::http::header::CONTENT_TYPE,
                    axum::http::HeaderValue::from_static("text/event-stream"),
                );
                resp.headers_mut()
                    .insert("cache-control", axum::http::HeaderValue::from_static("no-cache"));
                resp
            }
            Err(err) => error_response(err),
        }
    } else {
        match state
            .cross_server
            .forward(parsed, intent_label, backend, &state.metrics)
            .await
        {
            Ok(response) => Json(response).into_response(),
            Err(err) => error_response(err),
        }
    }
}

fn error_response(err: crate::types::ProxyError) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"message": err.to_string(), "type": "routing_error"}})),
    )
        .into_response()
}
