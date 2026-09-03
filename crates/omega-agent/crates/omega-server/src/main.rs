//! Omega server: entry point. Loads config, starts the 1.2B execution pool,
//! and opens the public axum listener.

mod alpha_status;
mod auth;
mod handlers;
mod peer_telemetry;

use axum::{
    middleware,
    routing::{get, post},
    Router,
};
use std::{sync::Arc, time::Duration};
use tokio::signal;
use tracing::info;

use omega_common::config::AppConfig;
use omega_common::metrics::MetricsStore;
use omega_common::types::{BackendServer, Pool};
use omega_common::workspace::AgentWorkspace;
use omega_core::demand::{DemandPool, DemandSpec};
use omega_core::proxy::{CodingAgentProxy, CrossServerProxy};
use omega_core::router::RowRouter;
use omega_core::tool_loop::{ToolLoop, ToolLoopConfig};
use peer_telemetry::PeerTelemetry;

use crate::handlers::AppState;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    omega_common::log::init();

    let cfg = AppConfig::load();
    info!(
        bind = %cfg.bind,
        inference_path = %cfg.inference_model.display(),
        "Omega starting up"
    );

    let metrics = Arc::new(MetricsStore::open(&cfg.metrics_path));

    let coding_spec = DemandSpec {
        binary: cfg.llama_binary.clone(),
        model: cfg.inference_model.clone(),
        ctx: cfg.inference.context,
        threads: cfg.inference.llama_threads,
        gpu_layers: cfg.inference.llama_gpu_layers,
        host: "127.0.0.1".to_string(),
        mlock: cfg.inference.llama_mlock,
        always_alive: false,
    };

    let coding_agent_pool = Arc::new(
        DemandPool::builder(Pool::CodingAgent, coding_spec)
            .queue_max(cfg.coding_agent_pool.queue_max)
            .spawn_attempts_before_503(cfg.coding_agent_pool.spawn_attempts_before_503)
            .build(&cfg.coding_agent_pool.ports),
    );

    // Retain 1.2B lanes for reuse, then unload them after 30 minutes idle.
    Arc::clone(&coding_agent_pool).spawn_idle_reaper(
        Duration::from_secs(1800),
        Duration::from_secs(30),
    );

    let agent_workspace = AgentWorkspace::new(cfg.workspace_dir.clone());
    let coding_agent = CodingAgentProxy::new(
        Arc::clone(&coding_agent_pool),
        Duration::from_secs(cfg.inference.timeout_seconds),
        Duration::from_secs(cfg.coding_agent_pool.admission_timeout_seconds),
        agent_workspace,
    );
    let cross_server = CrossServerProxy::new(Duration::from_secs(cfg.inference.timeout_seconds));
    let router = RowRouter::new(&cfg);

    // Peer telemetry: background-poll the enabled row-chain backends so the
    // telemetry page can show real Beta/Delta lanes. Poll interval 5 s — the
    // slaves' public endpoints are cheap and the responses are cached.
    let peers: Vec<BackendServer> = cfg
        .backends
        .iter()
        .filter(|b| b.enabled && b.id != "omega")
        .cloned()
        .collect();
    let peer_telemetry = Arc::new(PeerTelemetry::new(peers));
    Arc::clone(&peer_telemetry).spawn(Duration::from_secs(5));
    info!(
        peers = ?peer_telemetry.peer_ids(),
        "peer telemetry collector started"
    );
    let tool_loop = ToolLoop::new(
        Arc::clone(&coding_agent_pool),
        AgentWorkspace::new(cfg.workspace_dir.clone()),
        ToolLoopConfig::from_sections(
            &cfg.tool_loop,
            cfg.inference.max_tokens,
            cfg.coding_agent_pool.admission_timeout_seconds,
        ),
    );

    let state = Arc::new(AppState {
        config: cfg.clone(),
        started: std::time::Instant::now(),
        metrics: Arc::clone(&metrics),
        peer_telemetry: Arc::clone(&peer_telemetry),
        router,
        coding_agent,
        coding_agent_pool: Arc::clone(&coding_agent_pool),
        tool_loop,
        cross_server,
        update_lock: tokio::sync::Mutex::new(()),
    });

    // Alpha status channel: reports the master snapshot to the Ashat Hub.
    // Inert unless `hub.enabled` is true in server-config.json.
    if cfg.hub.enabled {
        info!(hub = %cfg.hub.url, "alpha status reporter enabled");
        alpha_status::AlphaReporter::new(
            Arc::clone(&state),
            cfg.hub.url.clone(),
            Duration::from_secs(30),
        )
        .spawn();
    }

    let app = Router::new()
        .route("/", get(handlers::landing))
        .route("/health", get(handlers::health))
        .route("/api/public_status", get(handlers::public_status))
        .route("/api/public_metrics", get(handlers::public_metrics))
        .route(
            "/api/dashboard_timeseries",
            get(handlers::dashboard_timeseries),
        )
        .route("/v1/models", get(handlers::list_models))
        .route(
            "/v1/chat/completions",
            post(handlers::chat).layer(middleware::from_fn_with_state(
                Arc::clone(&state),
                auth::require_ashat_key,
            )),
        )
        .route(
            "/api/admin/update",
            post(handlers::admin_update).layer(middleware::from_fn_with_state(
                Arc::clone(&state),
                auth::require_admin_key,
            )),
        )
        .route(
            "/api/admin/github_sync",
            post(handlers::github_sync).layer(middleware::from_fn_with_state(
                Arc::clone(&state),
                auth::require_admin_key,
            )),
        )
        .with_state(state.clone());

    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    info!(bind = %cfg.bind, "Omega listening (public + internal surfaces)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal(Arc::clone(&coding_agent_pool)))
        .await?;

    Ok(())
}

async fn shutdown_signal(coding_pool: Arc<DemandPool>) {
    let ctrl_c = async {
        let _ = signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        let mut s = match signal::unix::signal(signal::unix::SignalKind::terminate()) {
            Ok(s) => s,
            Err(_) => return,
        };
        s.recv().await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => info!("SIGINT received"),
        _ = terminate => info!("SIGTERM received"),
    }

    info!("killing active children; bringing Omega down");
    coding_pool.kill_active().await;
}
