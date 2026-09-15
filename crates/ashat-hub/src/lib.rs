use std::{
    collections::{HashMap, VecDeque},
    env,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use axum::{
    body::Body,
    extract::{DefaultBodyLimit, State},
    http::{HeaderValue, Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};

use serde::Serialize;
use serde_json::Value;
use sqlx::{mysql::MySqlPoolOptions, MySqlPool};
use tokio::time::timeout;
use uuid::Uuid;

use response::{error_response, normalize_error_response};

mod admin;
mod auth;
mod changes;
mod chat;
mod compat;
mod conversations;
mod deployment;
mod galileo_jobs;
mod github_app;
mod icarus_auth;
mod mail;
mod member;
mod oidc;
mod omega;
mod preview;
mod projects;
mod response;
mod vesper;
mod web;

const REMOTE_TIMEOUT: Duration = Duration::from_secs(4);
const UPSTREAM_TIMEOUT: Duration = Duration::from_secs(120);
const GATEWAY_REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_REQUEST_BODY_BYTES: usize = 8 * 1024 * 1024;

#[derive(Default)]
pub(crate) struct GatewayMetrics {
    requests_total: AtomicU64,
    responses_4xx: AtomicU64,
    responses_5xx: AtomicU64,
    auth_failures: AtomicU64,
    rate_limited: AtomicU64,
}

impl GatewayMetrics {
    fn record_response(&self, status: StatusCode) {
        self.requests_total.fetch_add(1, Ordering::Relaxed);
        if status.is_client_error() {
            self.responses_4xx.fetch_add(1, Ordering::Relaxed);
        }
        if status.is_server_error() {
            self.responses_5xx.fetch_add(1, Ordering::Relaxed);
        }
        if status == StatusCode::TOO_MANY_REQUESTS {
            self.rate_limited.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub(crate) fn record_auth_failure(&self) {
        self.auth_failures.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn record_rate_limited(&self) {
        self.rate_limited.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn snapshot(&self) -> serde_json::Value {
        serde_json::json!({
            "requests_total": self.requests_total.load(Ordering::Relaxed),
            "responses_4xx": self.responses_4xx.load(Ordering::Relaxed),
            "responses_5xx": self.responses_5xx.load(Ordering::Relaxed),
            "auth_failures": self.auth_failures.load(Ordering::Relaxed),
            "rate_limited": self.rate_limited.load(Ordering::Relaxed),
        })
    }
}

#[derive(Clone)]
pub(crate) struct AuthRateLimiter {
    attempts: Arc<Mutex<HashMap<String, VecDeque<Instant>>>>,
}

impl AuthRateLimiter {
    pub(crate) fn new() -> Self {
        Self {
            attempts: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub(crate) fn check(&self, key: &str, limit: usize, window: Duration) -> Option<u64> {
        let now = Instant::now();
        let mut attempts = self
            .attempts
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if attempts.len() >= 10_000 && !attempts.contains_key(key) {
            attempts.retain(|_, entries| {
                entries
                    .back()
                    .is_some_and(|timestamp| now.duration_since(*timestamp) < window)
            });
            if attempts.len() >= 10_000 {
                return Some(window.as_secs().max(1));
            }
        }
        let entries = attempts.entry(key.to_owned()).or_default();
        while entries
            .front()
            .is_some_and(|timestamp| now.duration_since(*timestamp) >= window)
        {
            entries.pop_front();
        }
        if entries.len() >= limit {
            let retry_after = entries
                .front()
                .map(|timestamp| {
                    window
                        .saturating_sub(now.duration_since(*timestamp))
                        .as_secs()
                        .max(1)
                })
                .unwrap_or(1);
            return Some(retry_after);
        }
        entries.push_back(now);
        None
    }

    pub(crate) fn clear(&self, key: &str) {
        let mut attempts = self
            .attempts
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        attempts.remove(key);
    }
}

#[derive(Clone)]
pub struct AppState {
    pub(crate) client: reqwest::Client,
    targets: Arc<Vec<TelemetryTarget>>,
    pub(crate) db: Option<MySqlPool>,
    pub(crate) auth: AuthConfig,
    pub(crate) mail: mail::MailConfig,
    pub(crate) projects_root: std::path::PathBuf,
    pub(crate) releases_dir: std::path::PathBuf,
    pub(crate) hub_public_url: Option<String>,
    pub(crate) backup_public_url: Option<String>,
    pub(crate) deploy_domain: Option<String>,
    pub(crate) deploy_root: std::path::PathBuf,
    pub(crate) deploy_backup_root: std::path::PathBuf,
    pub(crate) web_root: std::path::PathBuf,
    pub(crate) preview: Arc<preview::PreviewManager>,
    pub(crate) chat_upstream: Option<String>,
    pub(crate) planner_upstream: Option<String>,
    pub(crate) job_upstream: Option<String>,
    pub(crate) auth_rate_limiter: AuthRateLimiter,
    pub(crate) operation_rate_limiter: AuthRateLimiter,
    pub(crate) oidc: Arc<oidc::OidcIssuer>,
    pub(crate) metrics: Arc<GatewayMetrics>,
    migrations_ready: Arc<AtomicBool>,
}

#[derive(Clone)]
pub(crate) struct AuthConfig {
    pub(crate) cookie_name: String,
    pub(crate) legacy_cookie_name: String,
    pub(crate) csrf_cookie_name: String,
    pub(crate) secure_cookie: bool,
    pub(crate) session_lifetime_seconds: i64,
    pub(crate) email_verification_enabled: bool,
    pub(crate) trust_proxy_headers: bool,
    #[allow(dead_code)]
    pub(crate) service_token: Option<String>,
}

#[derive(Clone, Debug)]
struct TelemetryTarget {
    id: &'static str,
    label: &'static str,
    base_url: String,
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    status: &'static str,
    service: &'static str,
}

#[derive(Debug, Serialize)]
struct TelemetryResponse {
    servers: Vec<ServerSnapshot>,
    updated_at: u64,
}

#[derive(Debug, Serialize)]
struct ServerSnapshot {
    id: &'static str,
    label: &'static str,
    online: bool,
    active_requests: u64,
    requests_last_5m: u64,
    generation_tokens_per_second: f64,
    prompt_tokens_per_second: f64,
    total_completion_tokens: u64,
    avg_latency_ms: f64,
    queue_depth: u64,
    queue_limit: u64,
}

pub async fn start_galileo_worker(
    state: AppState,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    if let Some(pool) = state.db.as_ref() {
        // Keep the embedded migration bundle rebuild-sensitive when a new
        // migration file is added to an existing checkout.
        sqlx::migrate!().run(pool).await?;
        state.migrations_ready.store(true, Ordering::Release);
        galileo_jobs::spawn_worker(state);
    }
    Ok(())
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/api/health", get(health))
        .route("/api/telemetry", get(telemetry))
        .route("/api/showcase", get(showcase))
        .route("/api/admin/metrics", get(admin_metrics))
        .merge(auth::routes())
        .merge(admin::routes())
        .merge(projects::routes())
        .merge(conversations::routes())
        .merge(chat::routes())
        .merge(compat::routes())
        .merge(galileo_jobs::routes())
        .merge(github_app::routes())
        .merge(preview::routes())
        .merge(changes::routes())
        .merge(deployment::routes())
        .merge(icarus_auth::routes())
        .merge(oidc::routes())
        .merge(omega::routes())
        .merge(vesper::routes())
        .merge(member::routes())
        .merge(web::routes())
        .layer(DefaultBodyLimit::max(MAX_REQUEST_BODY_BYTES))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            request_context,
        ))
        .with_state(state)
}

pub fn state_from_env() -> Result<AppState, Box<dyn std::error::Error + Send + Sync>> {
    let client = reqwest::Client::builder()
        .timeout(UPSTREAM_TIMEOUT)
        .build()?;

    let targets = vec![
        TelemetryTarget {
            id: "omega",
            label: "Omega",
            base_url: env_url("ASHAT_OMEGA_TELEMETRY_URL", "https://129.213.94.124"),
        },
        TelemetryTarget {
            id: "beta",
            label: "Beta",
            base_url: env_url("ASHAT_BETA_TELEMETRY_URL", "https://150.136.208.93:8082"),
        },
        TelemetryTarget {
            id: "delta",
            label: "Delta",
            base_url: env_url("ASHAT_DELTA_TELEMETRY_URL", "https://129.213.147.225:8088"),
        },
    ];

    let db = match env::var("ASHAT_DATABASE_URL").or_else(|_| env::var("DATABASE_URL")) {
        Ok(url) if !url.trim().is_empty() => Some(
            MySqlPoolOptions::new()
                .max_connections(10)
                .acquire_timeout(Duration::from_secs(5))
                .idle_timeout(Duration::from_secs(300))
                .max_lifetime(Duration::from_secs(1800))
                .connect_lazy(&url)?,
        ),
        _ => None,
    };

    let hub_public_url = env::var("ASHAT_HUB_PUBLIC_URL")
        .ok()
        .filter(|value| !value.trim().is_empty());

    let session_lifetime_seconds = env::var("ASHAT_SESSION_LIFETIME")
        .or_else(|_| env::var("SESSION_LIFETIME"))
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(7200);

    Ok(AppState {
        client,
        targets: Arc::new(targets),
        db,
        projects_root: env::var("ASHAT_PROJECTS_ROOT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from("modules/AshatHub/projects")),
        releases_dir: env::var("ASHAT_RELEASES_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from("storage/vesper-releases")),
        hub_public_url: hub_public_url.clone(),
        backup_public_url: env::var("ASHAT_BACKUP_PUBLIC_URL")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim_end_matches('/').to_owned()),
        deploy_domain: env::var("ASHAT_DEPLOY_DOMAIN")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().trim_end_matches('.').to_owned()),
        deploy_root: env::var("ASHAT_DEPLOY_ROOT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from("modules/AshatHub/public/host")),
        deploy_backup_root: env::var("ASHAT_DEPLOY_BACKUP_ROOT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from("storage/ashat-deploy-backups")),
        web_root: env::var("ASHAT_WEB_ROOT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from("apps/ashat-hub-web/dist")),
        preview: Arc::new(preview::PreviewManager::default()),
        chat_upstream: env::var("ASHAT_GALILEO_CHAT_UPSTREAM")
            .ok()
            .filter(|value| !value.trim().is_empty()),
        planner_upstream: env::var("ASHAT_GALILEO_PLANNER_UPSTREAM")
            .or_else(|_| env::var("ASHAT_GALILEO_CHAT_UPSTREAM"))
            .ok()
            .filter(|value| !value.trim().is_empty()),
        job_upstream: env::var("ASHAT_GALILEO_JOB_UPSTREAM")
            .ok()
            .filter(|value| !value.trim().is_empty()),
        auth_rate_limiter: AuthRateLimiter::new(),
        operation_rate_limiter: AuthRateLimiter::new(),
        oidc: Arc::new(oidc::OidcIssuer::load_or_create()),
        metrics: Arc::new(GatewayMetrics::default()),
        migrations_ready: Arc::new(AtomicBool::new(false)),
        auth: AuthConfig {
            cookie_name: env::var("ASHAT_RUST_SESSION_COOKIE")
                .unwrap_or_else(|_| "ashat_rust_sid".to_owned()),
            legacy_cookie_name: env::var("SESSION_COOKIE_NAME")
                .unwrap_or_else(|_| "ashat_sid".to_owned()),
            csrf_cookie_name: env::var("ASHAT_RUST_CSRF_COOKIE")
                .unwrap_or_else(|_| "ashat_rust_csrf".to_owned()),
            secure_cookie: env::var("ASHAT_AUTH_SECURE_COOKIE")
                .map(|value| value != "0" && value.to_lowercase() != "false")
                .unwrap_or_else(|_| {
                    // Auto-detect: require Secure only when TLS is configured.
                    env::var("ASHAT_TLS_CERT")
                        .or_else(|_| env::var("ASHAT_TLS_KEY"))
                        .is_ok()
                }),
            session_lifetime_seconds,
            email_verification_enabled: env::var("ASHAT_EMAIL_VERIFICATION_ENABLED")
                .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
                .unwrap_or(false),
            trust_proxy_headers: env::var("ASHAT_TRUST_PROXY_HEADERS")
                .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
                .unwrap_or(false),
            service_token: env::var("ASHAT_SERVICE_TOKEN")
                .ok()
                .filter(|value| !value.trim().is_empty()),
        },
        mail: mail::MailConfig::from_env(hub_public_url.as_deref()),
    })
}

fn env_url(key: &str, default: &str) -> String {
    env::var(key)
        .unwrap_or_else(|_| default.to_owned())
        .trim_end_matches('/')
        .to_owned()
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "ashat-hub",
    })
}

async fn ready(State(state): State<AppState>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_not_configured");
    };
    if !state.migrations_ready.load(Ordering::Acquire) {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "migrations_not_ready");
    }
    match pool.acquire().await {
        Ok(_) => Json(serde_json::json!({"status": "ready"})).into_response(),
        Err(error) => {
            tracing::warn!(?error, "Rust gateway readiness check failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable")
        }
    }
}

async fn request_context(
    State(state): State<AppState>,
    mut request: Request<Body>,
    next: Next,
) -> Response {
    let request_id = request
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.is_ascii())
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    request.extensions_mut().insert(request_id.clone());
    if let Ok(value) = HeaderValue::from_str(&request_id) {
        request.headers_mut().insert("x-request-id", value);
    }

    let mut response = match auth::enforce_csrf(
        &state,
        request.method(),
        request.uri().path(),
        request.headers(),
    )
    .await
    {
        Ok(()) => match tokio::time::timeout(GATEWAY_REQUEST_TIMEOUT, next.run(request)).await {
            Ok(response) => response,
            Err(_) => error_response(StatusCode::GATEWAY_TIMEOUT, "request_timeout"),
        },
        Err(response) => response,
    };
    response = normalize_error_response(response, &request_id).await;
    if let Ok(value) = HeaderValue::from_str(&request_id) {
        response.headers_mut().insert("x-request-id", value);
    }
    response.headers_mut().insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    response
        .headers_mut()
        .insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    if path.starts_with("/api/") {
        response
            .headers_mut()
            .insert("cache-control", HeaderValue::from_static("no-store"));
    }
    state.metrics.record_response(response.status());
    if response.status().is_client_error() || response.status().is_server_error() {
        tracing::warn!(%request_id, %method, %path, status = response.status().as_u16(), "AshatHub error response");
    } else {
        tracing::info!(%request_id, %method, %path, status = response.status().as_u16(), "AshatHub request");
    }
    response
}

async fn admin_metrics(
    auth::AdminUser(_user): auth::AdminUser,
    State(state): State<AppState>,
) -> impl IntoResponse {
    Json(state.metrics.snapshot())
}

async fn telemetry(
    State(state): State<AppState>,
    auth::AuthenticatedUser(_user): auth::AuthenticatedUser,
) -> impl IntoResponse {
    (StatusCode::OK, Json(collect_telemetry(&state).await))
}

#[derive(Debug, Serialize)]
struct ShowcaseProject {
    id: &'static str,
    name: &'static str,
    description: &'static str,
    category: &'static str,
    status: &'static str,
    updated: &'static str,
}

async fn showcase() -> impl IntoResponse {
    Json(serde_json::json!({
        "projects": [
            ShowcaseProject {
                id: "galileo",
                name: "Galileo Studio",
                description: "A browser-based development environment for building and shipping web projects. Edit files, work with coding assistants, preview Vite applications live, and deploy from the same workspace.",
                category: "studio",
                status: "in-development",
                updated: "2026-08",
            },
            ShowcaseProject {
                id: "vesper",
                name: "Vesper",
                description: "Internal tooling and infrastructure management for the Ashat agent fleet.",
                category: "project",
                status: "in-development",
                updated: "2026-07",
            },
            ShowcaseProject {
                id: "icarus",
                name: "Icarus Coding Agent CLI",
                description: "A local-first coding agent CLI with file operations, shell tools, Git context, transaction recording, and undo support.",
                category: "project",
                status: "in-development",
                updated: "2026-08",
            },
            ShowcaseProject {
                id: "paws-and-parcels",
                name: "Paws & Parcels",
                description: "A game about running a postal delivery service in a cozy animal village. Build routes, manage packages, and explore the neighborhood.",
                category: "game",
                status: "in-development",
                updated: "2026-06",
            },
        ]
    }))
}

pub(crate) async fn collect_telemetry(state: &AppState) -> TelemetryResponse {
    let omega = snapshot(&state.client, &state.targets[0]);
    let beta = snapshot(&state.client, &state.targets[1]);
    let delta = snapshot(&state.client, &state.targets[2]);
    let (omega, beta, delta) = tokio::join!(omega, beta, delta);

    let servers = vec![omega, beta, delta];
    TelemetryResponse {
        servers,
        updated_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_secs())
            .unwrap_or_default(),
    }
}

async fn snapshot(client: &reqwest::Client, target: &TelemetryTarget) -> ServerSnapshot {
    let status_url = format!("{}/api/public_status", target.base_url);
    let metrics_url = format!("{}/api/public_metrics", target.base_url);

    let (status, metrics) = tokio::join!(
        fetch_json(client, status_url),
        fetch_json(client, metrics_url),
    );

    let online = status.as_ref().is_some_and(|value| {
        value
            .get("llama_server_available")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && !value
                .get("degraded")
                .and_then(Value::as_bool)
                .unwrap_or(false)
    });
    let summary = metrics.as_ref().and_then(|value| value.get("summaries")).and_then(|value| value.get("omega"));
    let status_queue = status.as_ref().and_then(|value| value.get("queue"));
    ServerSnapshot {
        id: target.id,
        label: target.label,
        online,
        active_requests: metrics.as_ref().and_then(|v| v.get("active_requests")).and_then(Value::as_u64).unwrap_or_default(),
        requests_last_5m: metrics.as_ref().and_then(|v| v.get("requests_last_5m")).and_then(Value::as_u64).unwrap_or_default(),
        generation_tokens_per_second: metric_f64(summary, &["latest_generation_tokens_per_second", "avg_generation_tokens_per_second"]),
        prompt_tokens_per_second: metric_f64(summary, &["avg_prompt_tokens_per_second"]),
        total_completion_tokens: metric_u64(summary, &["total_completion_tokens"]),
        avg_latency_ms: metric_f64(summary, &["avg_total_latency_ms"]),
        queue_depth: status_queue.and_then(|v| v.get("depth")).and_then(Value::as_u64).unwrap_or_default(),
        queue_limit: status_queue.and_then(|v| v.get("limit")).and_then(Value::as_u64).unwrap_or_default(),
    }
}

fn metric_f64(value: Option<&Value>, keys: &[&str]) -> f64 {
    keys.iter()
        .find_map(|key| value.and_then(|value| value.get(*key)).and_then(Value::as_f64))
        .unwrap_or(0.0)
}

fn metric_u64(value: Option<&Value>, keys: &[&str]) -> u64 {
    keys.iter()
        .find_map(|key| value.and_then(|value| value.get(*key)).and_then(Value::as_u64))
        .unwrap_or_default()
}

async fn fetch_json(client: &reqwest::Client, url: String) -> Option<Value> {
    let request = client.get(url).send();
    let response = timeout(REMOTE_TIMEOUT, request).await.ok()?.ok()?;
    if !response.status().is_success() {
        return None;
    }
    response.json::<Value>().await.ok()
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::{app, env_url, state_from_env, AuthRateLimiter, MySqlPoolOptions};

    #[test]
    fn env_url_removes_trailing_slashes() {
        std::env::set_var("ASHAT_TEST_URL", "https://example.test///");
        assert_eq!(env_url("ASHAT_TEST_URL", "unused"), "https://example.test");
        std::env::remove_var("ASHAT_TEST_URL");
    }

    #[test]
    fn app_registers_auth_and_galileo_routes() {
        let state = state_from_env().expect("test state should build");
        let _router = app(state);
    }

    #[test]
    fn auth_rate_limiter_blocks_and_clears_keys() {
        let limiter = AuthRateLimiter::new();
        assert_eq!(
            limiter.check("login:test", 2, Duration::from_secs(60)),
            None
        );
        assert_eq!(
            limiter.check("login:test", 2, Duration::from_secs(60)),
            None
        );
        assert!(limiter
            .check("login:test", 2, Duration::from_secs(60))
            .is_some());
        limiter.clear("login:test");
        assert_eq!(
            limiter.check("login:test", 2, Duration::from_secs(60)),
            None
        );
    }

    #[tokio::test]
    async fn configured_database_migrations_are_reproducible() {
        let Ok(url) = std::env::var("ASHAT_HUB_TEST_DATABASE_URL") else {
            return;
        };
        let pool = MySqlPoolOptions::new()
            .max_connections(2)
            .connect(&url)
            .await
            .expect("configured test database should accept connections");
        sqlx::migrate!()
            .run(&pool)
            .await
            .expect("configured test database should apply migrations");
    }
}
