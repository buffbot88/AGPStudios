use axum::{body::Body, extract::State, http::{header, Request, StatusCode}, response::Response, routing::{any, get}, Router};
use std::{env, sync::{Arc, atomic::{AtomicUsize, Ordering}}};
use tokio::process::Command;

const MAX_WORKERS: usize = 3;

#[derive(Clone)]
struct AppState {
    workers: Arc<Vec<String>>,
    next: Arc<AtomicUsize>,
    client: reqwest::Client,
}

fn setting(name: &str, default: &str) -> String { env::var(name).unwrap_or_else(|_| default.into()) }

async fn health(State(state): State<AppState>) -> Response {
    let mut ready = 0;
    for url in state.workers.iter() {
        if state.client.get(format!("{url}/health")).send().await.map(|r| r.status().is_success()).unwrap_or(false) { ready += 1; }
    }
    let status = if ready == state.workers.len() { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
    Response::builder().status(status).header(header::CONTENT_TYPE, "application/json").body(Body::from(format!(r#"{{"workers":{},"ready":{}}}"#, state.workers.len(), ready))).unwrap()
}

async fn status(State(state): State<AppState>) -> Response {
    Response::builder().header(header::CONTENT_TYPE, "application/json").body(Body::from(format!(r#"{{"workers":{},"max_workers":{}}}"#, state.workers.len(), MAX_WORKERS))).unwrap()
}

async fn forward(state: &AppState, request: Request<Body>, url: String) -> Response {
    let body = match axum::body::to_bytes(request.into_body(), 16 * 1024 * 1024).await { Ok(body) => body, Err(_) => return (StatusCode::BAD_REQUEST, "invalid request body").into_response() };
    let response = match state.client.post(url).header(header::CONTENT_TYPE, "application/json").body(body).send().await { Ok(response) => response, Err(_) => return (StatusCode::BAD_GATEWAY, "inference worker unavailable").into_response() };
    let mut builder = Response::builder().status(response.status());
    if let Some(value) = response.headers().get(header::CONTENT_TYPE) { builder = builder.header(header::CONTENT_TYPE, value); }
    builder.body(Body::from_stream(response.bytes_stream())).unwrap()
}

async fn completions(State(state): State<AppState>, request: Request<Body>) -> Response {
    if state.workers.is_empty() { return (StatusCode::SERVICE_UNAVAILABLE, "no inference workers").into_response(); }
    let url = format!("{}/v1/chat/completions", state.workers[state.next.fetch_add(1, Ordering::Relaxed) % state.workers.len()]);
    forward(&state, request, url).await
}

async fn vision(State(state): State<AppState>, request: Request<Body>) -> Response {
    forward(&state, request, format!("{}/v1/chat/completions", setting("OMEGA_VISION_ENDPOINT", "http://127.0.0.1:19000"))).await
}

use axum::response::IntoResponse;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt().with_env_filter(setting("RUST_LOG", "info")).init();
    let bind = setting("OMEGA_BIND", "0.0.0.0:8080");
    let model = setting("OMEGA_MODEL", "models/LFM2.5-1.2B-Instruct-Q4_K_M.gguf");
    let llama = setting("OMEGA_LLAMA_BIN", "llama-server");
    let requested = setting("OMEGA_WORKERS", "3").parse::<usize>().unwrap_or(3).clamp(1, MAX_WORKERS);
    let base_port = setting("OMEGA_WORKER_BASE_PORT", "18080").parse::<u16>()?;
    let mut children = Vec::with_capacity(requested);
    let mut workers = Vec::with_capacity(requested);
    for index in 0..requested {
        let port = base_port + index as u16;
        let child = Command::new(&llama).args(["--model", &model, "--host", "127.0.0.1", "--port", &port.to_string(), "--jinja", "--ctx-size", "16384", "--reasoning-format", "deepseek"]).spawn()?;
        children.push(child);
        workers.push(format!("http://127.0.0.1:{port}"));
    }
    let state = AppState { workers: Arc::new(workers), next: Arc::new(AtomicUsize::new(0)), client: reqwest::Client::new() };
    let monitor_model = model.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            for (index, child) in children.iter_mut().enumerate() {
                if child.try_wait().ok().flatten().is_some() {
                    let port = base_port + index as u16;
                    match Command::new(&llama).args(["--model", &monitor_model, "--host", "127.0.0.1", "--port", &port.to_string(), "--jinja", "--ctx-size", "16384", "--reasoning-format", "deepseek"]).spawn() {
                        Ok(replacement) => *child = replacement,
                        Err(error) => tracing::error!(%error, port, "failed to restart Omega worker"),
                    }
                }
            }
        }
    });
    let app = Router::new().route("/health", get(health)).route("/ready", get(health)).route("/status", get(status)).route("/v1/chat/completions", any(completions)).route("/v1/vision/chat/completions", any(vision)).with_state(state.clone());
    tracing::info!(workers = state.workers.len(), %bind, "Omega agent listening");
    axum::serve(tokio::net::TcpListener::bind(&bind).await?, app).await?;
    Ok(())
}
