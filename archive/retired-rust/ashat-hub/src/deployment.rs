use std::{
    fs, io,
    path::{Path, PathBuf},
};

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{auth, response::error_response, AppState};

#[derive(Debug, Deserialize)]
struct ProjectRequest {
    project_id: String,
    subdomain: Option<String>,
    checkpoint_id: Option<String>,
}
#[derive(Debug, Deserialize)]
struct ProjectQuery {
    project_id: String,
}
#[derive(Debug, Serialize)]
struct DeploymentResponse {
    ok: bool,
    project_id: String,
    status: String,
    url: Option<String>,
    backup_url: Option<String>,
    subdomain: Option<String>,
    deployment_id: Option<String>,
    file_count: Option<usize>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/deploy", post(deploy))
        .route("/api/galileo/deploy/redeploy", post(deploy))
        .route("/api/galileo/deploy/status", post(status).get(status_get))
        .route("/api/galileo/deploy/undeploy", post(undeploy))
        .route("/api/galileo/deploy/rollback", post(rollback))
        .route("/api/galileo/deployments", get(history))
}

async fn deploy(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    publish(&state, &user.id, &input.project_id, input.subdomain.as_deref(), input.checkpoint_id.as_deref()).await
}

async fn status(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    deployment_status(&state, &user.id, &input.project_id).await
}

async fn status_get(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(input): Query<ProjectQuery>,
) -> Response {
    deployment_status(&state, &user.id, &input.project_id).await
}

#[derive(Debug, Deserialize)]
struct HistoryQuery {
    project_id: Option<String>,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
struct DeploymentHistoryRow {
    id: i64,
    project_id: String,
    deployment_id: String,
    url: String,
    subdomain: Option<String>,
    status: String,
    file_count: i64,
    message: String,
    created_at: i64,
}

async fn history(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(input): Query<HistoryQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let result = match &input.project_id {
        Some(project_id) if safe_segment(project_id) => {
            sqlx::query_as::<_, DeploymentHistoryRow>(
                "SELECT id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at
                 FROM galileo_deployment_history
                 WHERE user_id=? AND project_id=?
                 ORDER BY created_at DESC, id DESC LIMIT 50",
            )
            .bind(&user.id)
            .bind(project_id)
            .fetch_all(pool)
            .await
        }
        _ => sqlx::query_as::<_, DeploymentHistoryRow>(
            "SELECT id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at
             FROM galileo_deployment_history
             WHERE user_id=?
             ORDER BY created_at DESC, id DESC LIMIT 100",
        )
        .bind(&user.id)
        .fetch_all(pool)
        .await,
    };
    match result {
        Ok(deployments) => {
            Json(serde_json::json!({ "ok": true, "deployments": deployments })).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "deployment history query failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "deployments_unavailable")
        }
    }
}

async fn undeploy(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    if !safe_segment(&input.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    let target = state.deploy_root.join(&user.id).join(&input.project_id);
    if target.exists() {
        if let Err(error) = fs::remove_dir_all(&target) {
            tracing::error!(?error, "deployment removal failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
        }
    }
    if let Some(pool) = state.db.as_ref() {
        let timestamp = now();
        let _ = sqlx::query("UPDATE galileo_deployments SET status='undeployed',deployed_at=? WHERE user_id=? AND project_id=?").bind(timestamp).bind(&user.id).bind(&input.project_id).execute(pool).await;
        let _ = sqlx::query(
            "INSERT INTO galileo_deployment_history (user_id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at)
             VALUES (?,?,?,?,NULL,'undeployed',0,'undeployed',?)",
        )
        .bind(&user.id)
        .bind(&input.project_id)
        .bind(format!("dep_undeploy_{timestamp}"))
        .bind("")
        .bind(timestamp)
        .execute(pool)
        .await;
    }
    tracing::info!(event = "galileo.deployment.undeployed", user_id = %user.id, project_id = %input.project_id);
    Json(DeploymentResponse {
        ok: true,
        project_id: input.project_id,
        status: "undeployed".to_owned(),
        url: None,
        backup_url: None,
        subdomain: None,
        deployment_id: None,
        file_count: None,
    })
    .into_response()
}

async fn rollback(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<ProjectRequest>,
) -> Response {
    if !safe_segment(&input.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    let backup_dir = state
        .deploy_backup_root
        .join(&user.id)
        .join(&input.project_id);
    let Ok(entries) = fs::read_dir(&backup_dir) else {
        return error_response(StatusCode::NOT_FOUND, "deployment_backup_not_found");
    };
    let mut backups: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    backups.sort();
    let Some(previous) = backups.pop() else {
        return error_response(StatusCode::NOT_FOUND, "deployment_backup_not_found");
    };
    let target = state.deploy_root.join(&user.id).join(&input.project_id);
    let current_backup = backup_dir.join(format!("rollback-{}", now()));
    if target.is_dir() {
        fs::rename(&target, &current_backup).map_err(|_| ()).ok();
    }
    if let Err(error) = fs::rename(&previous, &target) {
        tracing::error!(?error, "deployment rollback failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
    }
    let url = deployment_url(&state, &user.id, &input.project_id, None);
    let backup_url = backup_deployment_url(&state, &input.project_id);
    if let Some(pool) = state.db.as_ref() {
        let timestamp = now();
        let _ = sqlx::query("UPDATE galileo_deployments SET status='deployed',url=?,deployed_at=? WHERE user_id=? AND project_id=?").bind(&url).bind(timestamp).bind(&user.id).bind(&input.project_id).execute(pool).await;
        let _ = sqlx::query(
            "INSERT INTO galileo_deployment_history (user_id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at)
             VALUES (?,?,?,?,NULL,'deployed',0,'rollback',?)",
        )
        .bind(&user.id)
        .bind(&input.project_id)
        .bind("rollback")
        .bind(&url)
        .bind(timestamp)
        .execute(pool)
        .await;
    }
    Json(DeploymentResponse {
        ok: true,
        project_id: input.project_id,
        status: "deployed".to_owned(),
        url: Some(url),
        backup_url,
        subdomain: None,
        deployment_id: Some("rollback".to_owned()),
        file_count: None,
    })
    .into_response()
}

async fn publish(
    state: &AppState,
    user_id: &str,
    project_id: &str,
    requested_subdomain: Option<&str>,
    checkpoint_id: Option<&str>,
) -> Response {
    if !safe_segment(project_id) || !safe_segment(user_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    let subdomain = match requested_subdomain {
        Some(value) => match normalize_subdomain(value) {
            Some(value) => Some(value),
            None => return error_response(StatusCode::BAD_REQUEST, "invalid_subdomain"),
        },
        None => None,
    };
    let project_root = state.projects_root.join(user_id).join(project_id);
    let source = match checkpoint_id {
        Some(id) if safe_segment(id) => project_root.join(".checkpoints").join(id),
        Some(_) => return error_response(StatusCode::BAD_REQUEST, "invalid_checkpoint_id"),
        None => project_root.clone(),
    };
    let metadata = match fs::symlink_metadata(&source) {
        Ok(metadata) => metadata,
        Err(_) => return error_response(StatusCode::NOT_FOUND, "project_not_found"),
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return error_response(StatusCode::FORBIDDEN, "project_storage_invalid");
    }
    let target_parent = state.deploy_root.join(user_id);
    let target = target_parent.join(project_id);
    let stage = target_parent.join(format!(".stage-{}", Uuid::new_v4().simple()));
    let backup_dir = state.deploy_backup_root.join(user_id).join(project_id);
    if let Err(error) = fs::create_dir_all(&stage) {
        tracing::error!(?error, "deployment staging failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
    }
    let copied = match copy_project(&source, &stage) {
        Ok(count) => count,
        Err(error) => {
            let _ = fs::remove_dir_all(&stage);
            tracing::warn!(?error, "deployment copy failed");
            return error_response(StatusCode::BAD_REQUEST, "deployment_copy_failed");
        }
    };
    if copied == 0 {
        let _ = fs::remove_dir_all(&stage);
        return error_response(StatusCode::BAD_REQUEST, "project_empty");
    }
    if let Err(error) = fs::create_dir_all(&backup_dir) {
        let _ = fs::remove_dir_all(&stage);
        tracing::error!(?error, "deployment backup directory failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
    }
    if target.exists() {
        let backup = backup_dir.join(format!("{}-{}", now(), Uuid::new_v4().simple()));
        if let Err(error) = fs::rename(&target, backup) {
            let _ = fs::remove_dir_all(&stage);
            tracing::error!(?error, "deployment backup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
        }
    }
    if let Err(error) = fs::rename(&stage, &target) {
        let _ = fs::remove_dir_all(&stage);
        tracing::error!(?error, "deployment publish failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
    }
    let deployment_id = format!("dep_{}", Uuid::new_v4().simple());
    let url = deployment_url(state, user_id, project_id, subdomain.as_deref());
    let backup_url = backup_deployment_url(state, project_id);
    if let Some(pool) = state.db.as_ref() {
        let timestamp = now();
        let result = sqlx::query("INSERT INTO galileo_deployments (user_id,project_id,deployment_id,url,subdomain,status,file_count,deployed_at) VALUES (?,?,?,?,?, 'deployed',?,?) ON DUPLICATE KEY UPDATE deployment_id=VALUES(deployment_id),url=VALUES(url),subdomain=VALUES(subdomain),status='deployed',file_count=VALUES(file_count),deployed_at=VALUES(deployed_at)").bind(user_id).bind(project_id).bind(&deployment_id).bind(&url).bind(&subdomain).bind(copied as i64).bind(timestamp).execute(pool).await;
        if let Err(error) = result {
            tracing::warn!(?error, "deployment record unavailable");
        }
        let _ = sqlx::query(
            "INSERT INTO galileo_deployment_history (user_id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at)
             VALUES (?,?,?,?,?, 'deployed',?,?,?)",
        )
        .bind(user_id)
        .bind(project_id)
        .bind(&deployment_id)
        .bind(&url)
        .bind(&subdomain)
        .bind(copied as i64)
        .bind("published")
        .bind(timestamp)
        .execute(pool)
        .await;
    }
    tracing::info!(
        event = "galileo.deployment.published",
        user_id,
        project_id,
        files = copied
    );
    Json(DeploymentResponse {
        ok: true,
        project_id: project_id.to_owned(),
        status: "deployed".to_owned(),
        url: Some(url),
        backup_url,
        subdomain,
        deployment_id: Some(deployment_id),
        file_count: Some(copied),
    })
    .into_response()
}

async fn deployment_status(state: &AppState, user_id: &str, project_id: &str) -> Response {
    if !safe_segment(project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    let target = state.deploy_root.join(user_id).join(project_id);
    let deployed = target.is_dir();
    let subdomain = if deployed {
        match state.db.as_ref() {
            Some(pool) => futures_subdomain(pool, user_id, project_id).await,
            None => None,
        }
    } else {
        None
    };
    let url = deployed.then(|| deployment_url(state, user_id, project_id, subdomain.as_deref()));
    let backup_url = deployed.then(|| backup_deployment_url(state, project_id)).flatten();
    Json(DeploymentResponse {
        ok: true,
        project_id: project_id.to_owned(),
        status: if deployed { "deployed" } else { "undeployed" }.to_owned(),
        url,
        backup_url,
        subdomain,
        deployment_id: None,
        file_count: deployed.then(|| count_files(&target)),
    })
    .into_response()
}

async fn futures_subdomain(
    pool: &sqlx::MySqlPool,
    user_id: &str,
    project_id: &str,
) -> Option<String> {
    sqlx::query_scalar(
        "SELECT subdomain FROM galileo_deployments WHERE user_id=? AND project_id=? AND status='deployed'",
    )
    .bind(user_id)
    .bind(project_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
}

fn copy_project(source: &Path, target: &Path) -> io::Result<usize> {
    let mut count = 0;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if file_type.is_symlink()
            || entry.file_name() == ".meta.json"
            || entry.file_name() == ".preview.log"
            || entry.file_name() == ".checkpoint.json"
            || entry.file_name() == ".checkpoints"
        {
            continue;
        }
        let source_path = entry.path();
        let target_path = target.join(entry.file_name());
        if file_type.is_dir() {
            fs::create_dir_all(&target_path)?;
            count += copy_project(&source_path, &target_path)?;
        } else if file_type.is_file() {
            if let Some(parent) = target_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::copy(source_path, target_path)?;
            count += 1;
        }
    }
    Ok(count)
}
fn count_files(root: &Path) -> usize {
    fs::read_dir(root)
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|entry| {
                    if entry.path().is_dir() {
                        count_files(&entry.path())
                    } else if entry.path().is_file() {
                        1
                    } else {
                        0
                    }
                })
                .sum()
        })
        .unwrap_or_default()
}
fn deployment_url(
    state: &AppState,
    user_id: &str,
    project_id: &str,
    subdomain: Option<&str>,
) -> String {
    match (state.deploy_domain.as_deref(), subdomain) {
        (Some(domain), Some(subdomain)) => format!("https://{}.{}", subdomain, domain),
        _ => format!("/host/{}/{}", user_id, project_id),
    }
}

fn normalize_subdomain(value: &str) -> Option<String> {
    let value = value.trim().to_ascii_lowercase();
    if safe_segment(&value)
        && value.len() <= 63
        && !matches!(value.as_str(), "www" | "api" | "ashat" | "mail")
    {
        Some(value)
    } else {
        None
    }
}

fn backup_deployment_url(state: &AppState, project_id: &str) -> Option<String> {
    state
        .backup_public_url
        .as_ref()
        .map(|base| format!("{}/x/{}", base, project_id))
}
fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}
fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}
