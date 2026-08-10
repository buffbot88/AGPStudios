//! Omega server: entry point. Loads config, boots the always-on intent
//! router baseline (LFM2.5-VL-450M), binds the spawn-on-demand pools, then
//! opens the public axum listener. Failure to boot the baseline is fatal:
//! the public listener never binds if the orchestrator isn't healthy.

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
use tracing::{error, info};

use omega_common::config::AppConfig;
use omega_common::metrics::MetricsStore;
use omega_common::types::{BackendServer, Pool};
use omega_common::workspace::AgentWorkspace;
use omega_core::demand::{DemandPool, DemandSpec};
use omega_core::orchestrator::Orchestrator;
use omega_core::proxy::{CodingAgentProxy, CrossServerProxy};
use omega_core::router::RowRouter;
use omega_core::skill_db::SkillDb;
use omega_core::supervision::Supervisor;
use omega_core::tool_loop::{ToolLoop, ToolLoopConfig};
use peer_telemetry::PeerTelemetry;

use crate::handlers::AppState;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    omega_common::log::init();

    let cfg = AppConfig::load();
    info!(
        bind = %cfg.bind,
        orchestrator_path = %cfg.orchestrator_model.display(),
        inference_path = %cfg.inference_model.display(),
        "Omega starting up"
    );

    let metrics = Arc::new(MetricsStore::open(&cfg.metrics_path));

    // Build the two demand pools. Both share the same llama-server binary
    // resolution but carry different GGUF paths.
    let orchestrator_spec = DemandSpec {
        binary: cfg.llama_binary.clone(),
        model: cfg.orchestrator_model.clone(),
        ctx: cfg.inference.context,
        threads: cfg.inference.llama_threads,
        gpu_layers: cfg.inference.llama_gpu_layers,
        host: "127.0.0.1".to_string(),
        mlock: cfg.inference.llama_mlock,
        always_alive: true,
    };
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

    // Build the Orchestrator pool port list by concatenating `ports_baseline`
    // and `ports_extra` straight from config. The total is whatever the config
    // dictates; today's spec is 1 baseline + 2 extras (3x VL-450M peak) but
    // the source treats the list as data.
    let orchestrator_ports: Vec<u16> = cfg
        .orchestrator_pool
        .ports_baseline
        .iter()
        .chain(cfg.orchestrator_pool.ports_extra.iter())
        .copied()
        .collect();
    let orchestrator_pool = Arc::new(
        DemandPool::builder(Pool::Orchestrator, orchestrator_spec)
            .queue_max(cfg.orchestrator_pool.queue_max)
            .spawn_attempts_before_503(cfg.orchestrator_pool.spawn_attempts_before_503)
            .build(&orchestrator_ports),
    );
    let coding_agent_pool = Arc::new(
        DemandPool::builder(Pool::CodingAgent, coding_spec)
            .queue_max(cfg.coding_agent_pool.queue_max)
            .spawn_attempts_before_503(cfg.coding_agent_pool.spawn_attempts_before_503)
            .build(&cfg.coding_agent_pool.ports),
    );

    // Seed the intent-router baseline (LFM2.5-VL-450M). If this fails: log
    // loudly and exit without binding the public listener — Omega does NOT
    // serve unauthenticated traffic while the orchestrator is dead.
    if let Err(err) = orchestrator_pool.seed_baseline(&metrics).await {
        error!(
            error = %err,
            "intent-router baseline failed to start. Refusing to bind public listener."
        );
        return Err(Box::new(err) as Box<dyn std::error::Error>);
    }
    info!("intent-router baseline healthy; orchestrator ready");

    let orchestrator = Orchestrator::new(
        Arc::clone(&orchestrator_pool),
        Duration::from_secs(cfg.inference.timeout_seconds),
    );
    let agent_workspace = AgentWorkspace::new(cfg.workspace_dir.clone());
    let coding_agent = CodingAgentProxy::new(
        Arc::clone(&coding_agent_pool),
        Duration::from_secs(cfg.inference.timeout_seconds),
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
            cfg.inference.timeout_seconds,
        ),
        SkillDb::from_config(&cfg.skills_db),
    );

    let state = Arc::new(AppState {
        config: cfg.clone(),
        started: std::time::Instant::now(),
        metrics: Arc::clone(&metrics),
        peer_telemetry: Arc::clone(&peer_telemetry),
        router,
        orchestrator,
        coding_agent,
        coding_agent_pool: Arc::clone(&coding_agent_pool),
        orchestrator_pool: Arc::clone(&orchestrator_pool),
        tool_loop,
        cross_server,
        update_lock: tokio::sync::Mutex::new(()),
    });

    // Spawn supervisor. It will respawn the baseline orchestrator on death
    // and emit metrics events.
    Supervisor::new(Arc::clone(&orchestrator_pool), Duration::from_secs(30))
        .spawn(Arc::clone(&metrics));

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
        .with_graceful_shutdown(shutdown_signal(
            Arc::clone(&coding_agent_pool),
            Arc::clone(&orchestrator_pool),
        ))
        .await?;

    Ok(())
}

async fn shutdown_signal(coding_pool: Arc<DemandPool>, orchestrator_pool: Arc<DemandPool>) {
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
    // Don't bring the orchestrator baseline down — it'd prevent any future
    // automated restart. Leave it to the supervisor / manual cleanup.
    let _ = Arc::clone(&orchestrator_pool);
}
