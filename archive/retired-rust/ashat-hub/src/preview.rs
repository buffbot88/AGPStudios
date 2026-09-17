use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek},
    path::{Path, PathBuf},
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use tokio::{
    net::TcpListener,
    process::{Child, Command},
    time::{timeout, Duration},
};

use crate::{auth, response::error_response, AppState};

const PORT_MIN: u16 = 51800;
const PORT_MAX: u16 = 51899;
const START_TIMEOUT: Duration = Duration::from_secs(15);
const LOG_MAX_BYTES: usize = 64 * 1024;

#[derive(Default)]
pub(crate) struct PreviewManager {
    processes: Mutex<HashMap<String, PreviewProcess>>,
}

struct PreviewProcess {
    child: Child,
    project_id: String,
    port: u16,
    log_file: PathBuf,
    started_at: i64,
}

#[derive(Debug, Deserialize)]
struct ProjectRequest {
    project_id: String,
}

#[derive(Debug, Deserialize)]
struct ProjectQuery {
    project_id: String,
}

#[derive(Debug, Deserialize)]
struct ContentQuery {
    project_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct PreviewStatus {
    project_id: String,
    status: String,
    url: Option<String>,
    port: Option<u16>,
    started_at: Option<i64>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/preview/start", post(start))
        .route("/api/galileo/preview/restart", post(restart))
        .route("/api/galileo/preview/stop", post(stop))
        .route("/api/galileo/preview/status", get(status))
        .route("/api/galileo/preview/log", get(log))
        .route("/api/galileo/preview/content", get(content))
        .route("/api/galileo/preview/content/*path", get(content_path))
}

async fn start(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    control(&state, &user.id, &input.project_id, false).await
}

async fn restart(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    let _ = state.preview.stop(&user.id, &input.project_id).await;
    control(&state, &user.id, &input.project_id, true).await
}

async fn stop(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    match state.preview.stop(&user.id, &input.project_id).await {
        Ok(()) => Json(serde_json::json!({ "ok": true, "status": "stopped" })).into_response(),
        Err(error) => {
            tracing::warn!(?error, "preview stop failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "preview_unavailable")
        }
    }
}

async fn status(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<ProjectQuery>,
) -> Response {
    if !safe_segment(&query.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    Json(state.preview.status(&user.id, &query.project_id).await).into_response()
}

async fn log(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<ProjectQuery>,
) -> Response {
    if !safe_segment(&query.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    match state.preview.log(&user.id, &query.project_id) {
        Ok(content) => {
            Json(serde_json::json!({ "project_id": query.project_id, "content": content }))
                .into_response()
        }
        Err(error) => {
            tracing::warn!(?error, "preview log read failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "preview_log_unavailable")
        }
    }
}

async fn content(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<ContentQuery>,
) -> Response {
    proxy_content(
        &state,
        &user.id,
        query.project_id.as_deref().unwrap_or_default(),
        "index.html",
    )
    .await
}

async fn content_path(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    axum::extract::Path(path): axum::extract::Path<String>,
    Query(query): Query<ContentQuery>,
) -> Response {
    let mut segments = path.splitn(2, '/');
    let path_project = segments.next().unwrap_or_default();
    let asset = segments
        .next()
        .filter(|value| !value.is_empty())
        .unwrap_or("index.html");
    let project_id = query
        .project_id
        .as_deref()
        .filter(|value| !value.is_empty())
        .unwrap_or(path_project);
    proxy_content(&state, &user.id, project_id, asset).await
}

async fn proxy_content(state: &AppState, user_id: &str, project_id: &str, path: &str) -> Response {
    if !safe_segment(project_id) || !safe_preview_path(path) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_preview_path");
    }
    let preview = state.preview.status(user_id, project_id).await;
    let Some(port) = preview.port else {
        return error_response(StatusCode::NOT_FOUND, "preview_not_running");
    };
    let url = format!("http://127.0.0.1:{}/{}", port, path);
    match state.client.get(url).send().await {
        Ok(response) if response.status().is_success() => {
            let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
            match response.bytes().await {
                Ok(bytes) => {
                    let mut output = Response::new(axum::body::Body::from(bytes));
                    if let Some(content_type) = content_type {
                        output
                            .headers_mut()
                            .insert(header::CONTENT_TYPE, content_type);
                    }
                    output
                }
                Err(_) => error_response(StatusCode::BAD_GATEWAY, "preview_read_failed"),
            }
        }
        Ok(response) if response.status() == StatusCode::NOT_FOUND && path != "index.html" => {
            match state
                .client
                .get(format!("http://127.0.0.1:{}/index.html", port))
                .send()
                .await
            {
                Ok(index) if index.status().is_success() => match index.bytes().await {
                    Ok(bytes) => (StatusCode::OK, [(header::CONTENT_TYPE, "text/html")], bytes)
                        .into_response(),
                    Err(_) => error_response(StatusCode::BAD_GATEWAY, "preview_read_failed"),
                },
                _ => error_response(StatusCode::NOT_FOUND, "preview_file_not_found"),
            }
        }
        Ok(_) => error_response(StatusCode::NOT_FOUND, "preview_file_not_found"),
        Err(error) => {
            tracing::debug!(?error, "preview proxy request failed");
            error_response(StatusCode::BAD_GATEWAY, "preview_unavailable")
        }
    }
}

async fn control(state: &AppState, user_id: &str, project_id: &str, _restart: bool) -> Response {
    if !safe_segment(project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    if !project_exists(state, user_id, project_id) {
        return error_response(StatusCode::NOT_FOUND, "project_not_found");
    }
    match state
        .preview
        .start(&state.projects_root, user_id, project_id)
        .await
    {
        Ok(status) => Json(status).into_response(),
        Err(error) => {
            tracing::warn!(?error, user_id, project_id, "preview start failed");
            error_response(StatusCode::BAD_GATEWAY, "preview_start_failed")
        }
    }
}

impl PreviewManager {
    async fn start(
        &self,
        projects_root: &Path,
        user_id: &str,
        project_id: &str,
    ) -> io::Result<PreviewStatus> {
        let key = key(user_id, project_id);
        {
            let mut processes = lock(&self.processes);
            if let Some(process) = processes.get_mut(&key) {
                if process.child.try_wait()?.is_none() {
                    return Ok(status_from(process));
                }
                processes.remove(&key);
            }
        }

        let root = projects_root.join(user_id).join(project_id);
        let metadata = fs::symlink_metadata(&root)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "invalid project root",
            ));
        }
        let port = self.allocate_port().await?;
        let log_dir = projects_root.join(".preview-logs").join(user_id);
        fs::create_dir_all(&log_dir)?;
        let log_file = log_dir.join(format!("{}.log", project_id));
        let stdout = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_file)?;
        let stderr = stdout.try_clone()?;
        let mut command = if has_dev_script(&root) {
            let mut command = Command::new("npm");
            command.args([
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--strictPort",
            ]);
            command
        } else {
            if !root.join("index.html").is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "project has no index.html or npm dev script",
                ));
            }
            let mut command = Command::new("python3");
            command.args([
                "-m",
                "http.server",
                &port.to_string(),
                "--bind",
                "127.0.0.1",
            ]);
            command
        };
        command
            .current_dir(&root)
            .stdin(std::process::Stdio::null())
            .stdout(stdout)
            .stderr(stderr);
        let child = command.spawn()?;
        let started_at = now();
        let pid = child.id();
        let process = PreviewProcess {
            child,
            project_id: project_id.to_owned(),
            port,
            log_file: log_file.clone(),
            started_at,
        };
        lock(&self.processes).insert(key, process);
        let ready = timeout(START_TIMEOUT, wait_for_port(port))
            .await
            .unwrap_or(false);
        if !ready {
            let _ = self.stop(user_id, project_id).await;
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "preview startup timed out",
            ));
        }
        tracing::info!(
            event = "galileo.preview.started",
            user_id,
            project_id,
            port,
            ?pid
        );
        Ok(PreviewStatus {
            project_id: project_id.to_owned(),
            status: "running".to_owned(),
            url: Some(format!("/api/galileo/preview/content/{}/", project_id)),
            port: Some(port),
            started_at: Some(started_at),
        })
    }

    async fn stop(&self, user_id: &str, project_id: &str) -> io::Result<()> {
        let key = key(user_id, project_id);
        let process = lock(&self.processes).remove(&key);
        if let Some(mut process) = process {
            let _ = process.child.kill().await;
            let _ = process.child.wait().await;
            tracing::info!(
                event = "galileo.preview.stopped",
                user_id,
                project_id,
                port = process.port
            );
        }
        Ok(())
    }

    async fn status(&self, user_id: &str, project_id: &str) -> PreviewStatus {
        let key = key(user_id, project_id);
        let mut processes = lock(&self.processes);
        if let Some(process) = processes.get_mut(&key) {
            match process.child.try_wait() {
                Ok(None) => return status_from(process),
                _ => {
                    processes.remove(&key);
                }
            }
        }
        PreviewStatus {
            project_id: project_id.to_owned(),
            status: "stopped".to_owned(),
            url: None,
            port: None,
            started_at: None,
        }
    }

    fn log(&self, user_id: &str, project_id: &str) -> io::Result<String> {
        let key = key(user_id, project_id);
        let processes = lock(&self.processes);
        let Some(process) = processes.get(&key) else {
            return Ok(String::new());
        };
        let mut file = File::open(&process.log_file)?;
        let size = file.metadata()?.len() as usize;
        if size > LOG_MAX_BYTES {
            file.seek(std::io::SeekFrom::End(-(LOG_MAX_BYTES as i64)))?;
        }
        let mut content = String::new();
        file.read_to_string(&mut content)?;
        Ok(content)
    }

    async fn allocate_port(&self) -> io::Result<u16> {
        let used: Vec<u16> = lock(&self.processes)
            .values()
            .map(|process| process.port)
            .collect();
        for port in PORT_MIN..=PORT_MAX {
            if used.contains(&port) {
                continue;
            }
            if let Ok(listener) = TcpListener::bind(("127.0.0.1", port)).await {
                drop(listener);
                return Ok(port);
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AddrInUse,
            "no preview ports available",
        ))
    }
}

fn status_from(process: &PreviewProcess) -> PreviewStatus {
    PreviewStatus {
        project_id: process.project_id.clone(),
        status: "running".to_owned(),
        url: Some(format!(
            "/api/galileo/preview/content/{}/",
            process.project_id
        )),
        port: Some(process.port),
        started_at: Some(process.started_at),
    }
}

fn project_exists(state: &AppState, user_id: &str, project_id: &str) -> bool {
    if !safe_segment(user_id) || !safe_segment(project_id) {
        return false;
    }
    let path = state.projects_root.join(user_id).join(project_id);
    fs::symlink_metadata(path)
        .map(|metadata| metadata.is_dir() && !metadata.file_type().is_symlink())
        .unwrap_or(false)
}

fn safe_preview_path(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('/')
        && !value.split('/').any(|part| part == ".." || part.is_empty())
}

fn has_dev_script(root: &Path) -> bool {
    let package = root.join("package.json");
    let Ok(content) = fs::read_to_string(package) else {
        return false;
    };
    serde_json::from_str::<serde_json::Value>(&content)
        .ok()
        .map(|value| {
            value
                .get("scripts")
                .and_then(|scripts| scripts.get("dev"))
                .and_then(serde_json::Value::as_str)
                .is_some()
        })
        .unwrap_or(false)
}

async fn wait_for_port(port: u16) -> bool {
    for _ in 0..30 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    false
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn key(user_id: &str, project_id: &str) -> String {
    format!("{}:{}", user_id, project_id)
}
fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}
