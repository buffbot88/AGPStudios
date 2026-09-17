use std::{
    fs,
    path::{Component, Path, PathBuf},
};

use axum::{
    extract::{Path as AxumPath, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, MySqlPool};

use crate::{auth, response::error_response, AppState};

const MAX_PROJECT_BYTES: u64 = 50 * 1024 * 1024;

#[derive(Debug, Serialize, FromRow, Clone)]
struct Change {
    id: u64,
    job_id: String,
    project_id: String,
    path: String,
    operation: String,
    before_exists: i8,
    #[serde(skip_serializing)]
    before_content: Option<String>,
    #[serde(skip_serializing)]
    after_content: Option<String>,
    state: String,
    created_at: i64,
    updated_at: i64,
}

#[derive(Debug, Deserialize)]
struct ChangeRequest {
    path: Option<String>,
}

#[derive(Debug, Serialize)]
struct ChangeView {
    id: u64,
    job_id: String,
    project_id: String,
    path: String,
    operation: String,
    state: String,
    diff: String,
    created_at: i64,
    updated_at: i64,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/agents/jobs/:id/changes", get(list))
        .route("/api/galileo/agents/jobs/:id/changes/accept", post(accept))
        .route("/api/galileo/agents/jobs/:id/changes/revert", post(revert))
}

async fn list(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !owns_job(pool, &id, &user.id).await {
        return error_response(StatusCode::NOT_FOUND, "job_not_found");
    }
    match changes_for(pool, &id, &user.id).await {
        Ok(changes) => Json(
            serde_json::json!({ "changes": changes.iter().map(change_view).collect::<Vec<_>>() }),
        )
        .into_response(),
        Err(error) => {
            tracing::error!(?error, "job changes lookup failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "changes_unavailable")
        }
    }
}

async fn accept(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(id): AxumPath<String>,
    Json(input): Json<ChangeRequest>,
) -> Response {
    resolve(&state, &user.id, &id, input.path.as_deref(), true).await
}

async fn revert(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(id): AxumPath<String>,
    Json(input): Json<ChangeRequest>,
) -> Response {
    resolve(&state, &user.id, &id, input.path.as_deref(), false).await
}

async fn resolve(
    state: &AppState,
    user_id: &str,
    job_id: &str,
    selected_path: Option<&str>,
    accept: bool,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !owns_job(pool, job_id, user_id).await {
        return error_response(StatusCode::NOT_FOUND, "job_not_found");
    }
    let changes = match changes_for(pool, job_id, user_id).await {
        Ok(changes) => changes,
        Err(error) => {
            tracing::error!(?error, "job changes lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "changes_unavailable");
        }
    };
    let selected: Vec<Change> = changes
        .into_iter()
        .filter(|change| {
            selected_path
                .map(|path| path == change.path)
                .unwrap_or(true)
                && if accept {
                    change.state == "pending"
                } else {
                    change.state == "pending" || change.state == "accepted"
                }
        })
        .collect();
    if selected.is_empty() {
        return error_response(StatusCode::CONFLICT, "no_changes_to_resolve");
    }
    for change in &selected {
        if accept {
            if let Err(error) = apply_content(
                state,
                user_id,
                &change.project_id,
                &change.path,
                change.after_content.as_deref(),
                change.operation.as_str(),
                true,
            ) {
                tracing::error!(?error, "agent change application failed");
                return error_response(StatusCode::CONFLICT, "change_apply_failed");
            }
        } else if change.state == "accepted" {
            if let Err(error) = apply_content(
                state,
                user_id,
                &change.project_id,
                &change.path,
                change.before_content.as_deref(),
                change.operation.as_str(),
                change.before_exists != 0,
            ) {
                tracing::error!(?error, "agent change revert failed");
                return error_response(StatusCode::CONFLICT, "change_revert_failed");
            }
        }
        let next_state = if accept { "accepted" } else { "reverted" };
        if let Err(error) = sqlx::query(
            "UPDATE galileo_job_changes SET state=?,updated_at=? WHERE id=? AND user_id=?",
        )
        .bind(next_state)
        .bind(now())
        .bind(change.id)
        .bind(user_id)
        .execute(pool)
        .await
        {
            tracing::error!(?error, "agent change state update failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "changes_unavailable");
        }
    }
    Json(serde_json::json!({ "ok": true, "resolved": selected.len(), "state": if accept { "accepted" } else { "reverted" } })).into_response()
}

fn change_view(change: &Change) -> ChangeView {
    ChangeView {
        id: change.id,
        job_id: change.job_id.clone(),
        project_id: change.project_id.clone(),
        path: change.path.clone(),
        operation: change.operation.clone(),
        state: change.state.clone(),
        diff: bounded_diff(change),
        created_at: change.created_at,
        updated_at: change.updated_at,
    }
}

fn bounded_diff(change: &Change) -> String {
    let before = redact(change.before_content.as_deref().unwrap_or_default());
    let after = redact(change.after_content.as_deref().unwrap_or_default());
    let mut diff = format!("--- {}\n+++ {}\n", change.path, change.path);
    for line in before.lines() {
        diff.push_str(&format!("-{}\n", line));
    }
    for line in after.lines() {
        diff.push_str(&format!("+{}\n", line));
    }
    diff.chars().take(12_000).collect()
}

fn redact(content: &str) -> String {
    content
        .lines()
        .map(|line| {
            let lower = line.to_ascii_lowercase();
            if [
                "api_key",
                "apikey",
                "secret",
                "password",
                "access_token",
                "private_key",
            ]
            .iter()
            .any(|needle| lower.contains(needle))
            {
                "[redacted sensitive-looking line]".to_owned()
            } else {
                line.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

async fn changes_for(
    pool: &MySqlPool,
    job_id: &str,
    user_id: &str,
) -> Result<Vec<Change>, sqlx::Error> {
    sqlx::query_as::<_, Change>("SELECT id,job_id,project_id,path,operation,before_exists,before_content,after_content,state,created_at,updated_at FROM galileo_job_changes WHERE job_id=? AND user_id=? ORDER BY id")
        .bind(job_id).bind(user_id).fetch_all(pool).await
}

async fn owns_job(pool: &MySqlPool, job_id: &str, user_id: &str) -> bool {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM galileo_jobs WHERE id=? AND user_id=?")
        .bind(job_id)
        .bind(user_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0)
        == 1
}

fn apply_content(
    state: &AppState,
    user_id: &str,
    project_id: &str,
    path: &str,
    content: Option<&str>,
    operation: &str,
    exists: bool,
) -> std::io::Result<()> {
    // The project id is validated against the job before this function is called.
    // Resolve it from the job path in stage_changes; this guard still prevents
    // arbitrary filesystem access if a malformed row is encountered.
    let project_id = if safe_segment(project_id) {
        project_id
    } else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "invalid project",
        ));
    };
    let root = state.projects_root.join(user_id).join(project_id);
    let root = fs::canonicalize(root)?;
    let relative = safe_path(path)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidInput, "invalid path"))?;
    let target = root.join(relative);
    let existing = nearest_existing(&target)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "project path missing"))?;
    if !fs::canonicalize(existing)?.starts_with(&root) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "path escaped project",
        ));
    }
    if operation == "delete" || !exists {
        if target.is_file() {
            fs::remove_file(target)?;
        }
        return Ok(());
    }
    let current_size = directory_size(&root)?;
    let previous_size = fs::metadata(&target)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let next_size = current_size
        .saturating_sub(previous_size)
        .saturating_add(content.unwrap_or_default().len() as u64);
    if next_size > MAX_PROJECT_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::FileTooLarge,
            "project quota exceeded",
        ));
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(target, content.unwrap_or_default())
}

fn directory_size(root: &Path) -> std::io::Result<u64> {
    let mut total = 0;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if entry.file_type()?.is_symlink() || entry.file_name() == ".meta.json" {
            continue;
        }
        if entry.path().is_dir() {
            total += directory_size(&entry.path())?;
        } else {
            total += entry.metadata()?.len();
        }
    }
    Ok(total)
}
fn nearest_existing(path: &Path) -> Option<PathBuf> {
    let mut current = path.to_owned();
    loop {
        if current.exists() {
            return Some(current);
        }
        if !current.pop() {
            return None;
        }
    }
}
fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}
fn safe_path(value: &str) -> Option<PathBuf> {
    let path = PathBuf::from(value);
    if value.is_empty()
        || path.is_absolute()
        || path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        None
    } else {
        Some(path)
    }
}
fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}
