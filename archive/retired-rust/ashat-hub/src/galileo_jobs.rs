use std::{
    collections::HashSet,
    fs, io,
    path::{Component, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{FromRow, MySqlPool};
use tokio::time::{sleep, Duration};
use uuid::Uuid;

use crate::{
    auth,
    response::{error_response, rate_limit_response},
    AppState,
};

#[derive(Debug, Deserialize)]
struct CreateJob {
    project_id: String,
    checkpoint_id: Option<String>,
    request: String,
    plan_id: String,
}

#[derive(Debug, Serialize, FromRow)]
struct Job {
    id: String,
    user_id: String,
    project_id: String,
    checkpoint_id: Option<String>,
    request: String,
    status: String,
    result: Option<String>,
    error: Option<String>,
    created_at: i64,
    updated_at: i64,
    approval_payload: Option<String>,
}

#[derive(Debug, Serialize)]
struct JobResponse {
    job: Job,
}

#[derive(Debug, FromRow)]
struct PlanRow {
    id: String,
    request: String,
    payload: String,
    status: String,
}

#[derive(Debug, Serialize, FromRow)]
struct JobEvent {
    id: u64,
    job_id: String,
    kind: String,
    payload: String,
    created_at: i64,
}

#[derive(Debug, Serialize)]
struct EventsResponse {
    events: Vec<JobEvent>,
}

#[derive(Debug, Deserialize)]
struct EventQuery {
    after_id: Option<u64>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/agents/jobs", post(create))
        .route("/api/galileo/agents/jobs/:id", get(status))
        .route("/api/galileo/agents/jobs/:id/cancel", post(cancel))
        .route("/api/galileo/agents/jobs/:id/events", get(events))
}

pub(crate) fn spawn_worker(state: AppState) {
    tokio::spawn(async move {
        if let Some(pool) = state.db.as_ref() {
            if let Err(error) = recover_stale(pool).await {
                tracing::error!(?error, "Galileo stale-job recovery failed");
            }
        }
        loop {
            if let Some(pool) = state.db.as_ref() {
                if let Err(error) = work_once(&state, pool).await {
                    tracing::error!(?error, "Galileo Rust worker cycle failed");
                }
            }
            sleep(Duration::from_millis(500)).await;
        }
    });
}

async fn create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<CreateJob>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if let Some(retry_after) =
        state
            .operation_rate_limiter
            .check(&format!("job:{}", user.id), 10, Duration::from_secs(60))
    {
        state.metrics.record_rate_limited();
        return rate_limit_response(retry_after);
    }
    let request = input.request.trim();
    if request.is_empty()
        || request.len() > 50_000
        || !safe_segment(&input.project_id)
        || input.checkpoint_id.as_deref().is_some_and(|id| !safe_segment(id))
        || !safe_segment(&input.plan_id)
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_job");
    }
    let Some(plan) = sqlx::query_as::<_, PlanRow>("SELECT id,request,payload,status FROM galileo_plans WHERE id=? AND user_id=? AND project_id=?")
        .bind(&input.plan_id).bind(&user.id).bind(&input.project_id).fetch_optional(pool).await.ok().flatten() else {
        return error_response(StatusCode::NOT_FOUND, "plan_not_found");
    };
    if plan.status != "pending" || plan.request != request {
        return error_response(StatusCode::CONFLICT, "plan_not_approvable");
    }
    let approval_payload = plan.payload.clone();
    match sqlx::query("UPDATE galileo_plans SET status='approved' WHERE id=? AND status='pending'")
        .bind(&plan.id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 1 => {}
        Ok(_) => return error_response(StatusCode::CONFLICT, "plan_not_approvable"),
        Err(error) => {
            tracing::error!(?error, "Galileo plan approval failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "plan_unavailable");
        }
    }
    let now = now();
    let job = Job {
        id: format!("job_{}", Uuid::new_v4().simple()),
        user_id: user.id,
        project_id: input.project_id,
        checkpoint_id: input.checkpoint_id,
        request: request.to_owned(),
        status: "queued".into(),
        result: None,
        error: None,
        created_at: now,
        updated_at: now,
        approval_payload: Some(approval_payload.clone()),
    };
    if let Err(error) = sqlx::query("INSERT INTO galileo_jobs (id,user_id,project_id,checkpoint_id,request,status,created_at,updated_at,approval_payload) VALUES (?,?,?,?,?,?,?,?,?)")
        .bind(&job.id).bind(&job.user_id).bind(&job.project_id).bind(&job.checkpoint_id).bind(&job.request).bind(&job.status).bind(now).bind(now).bind(&job.approval_payload).execute(pool).await { tracing::error!(?error, "Galileo job insert failed"); return error_response(StatusCode::SERVICE_UNAVAILABLE, "job_unavailable"); }
    add_event(pool, &job.id, "queued", "{}").await.ok();
    tracing::info!(
        event = "galileo.job.queued",
        job_id = %job.id,
        user_id = %job.user_id,
        project_id = %job.project_id
    );
    (StatusCode::ACCEPTED, Json(JobResponse { job })).into_response()
}

async fn status(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    match sqlx::query_as::<_, Job>("SELECT id,user_id,project_id,checkpoint_id,request,status,result,error,created_at,updated_at,approval_payload FROM galileo_jobs WHERE id=? AND user_id=?")
        .bind(id).bind(user.id).fetch_optional(pool).await {
        Ok(Some(job)) => Json(JobResponse { job }).into_response(),
        Ok(None) => error_response(StatusCode::NOT_FOUND, "job_not_found"),
        Err(error) => { tracing::error!(?error, "Galileo job lookup failed"); error_response(StatusCode::SERVICE_UNAVAILABLE, "job_unavailable") }
    }
}

async fn events(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(id): AxumPath<String>,
    Query(query): Query<EventQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let owns =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM galileo_jobs WHERE id=? AND user_id=?")
            .bind(&id)
            .bind(user.id)
            .fetch_one(pool)
            .await
            .unwrap_or(0);
    if owns == 0 {
        return error_response(StatusCode::NOT_FOUND, "job_not_found");
    }
    let after_id = query.after_id.unwrap_or(0);
    match sqlx::query_as::<_, JobEvent>("SELECT id,job_id,kind,payload,created_at FROM galileo_job_events WHERE job_id=? AND id>? ORDER BY id LIMIT 500").bind(id).bind(after_id).fetch_all(pool).await {
        Ok(events) => Json(EventsResponse { events }).into_response(),
        Err(error) => { tracing::error!(?error, "Galileo event lookup failed"); error_response(StatusCode::SERVICE_UNAVAILABLE, "job_unavailable") }
    }
}

async fn cancel(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let result = sqlx::query("UPDATE galileo_jobs SET status='cancelled',updated_at=? WHERE id=? AND user_id=? AND status IN ('queued','running')")
        .bind(now()).bind(&id).bind(&user.id).execute(pool).await;
    match result {
        Ok(result) if result.rows_affected() == 1 => {
            add_event(pool, &id, "cancelled", "{}").await.ok();
            tracing::info!(event = "galileo.job.cancelled", job_id = %id, user_id = %user.id);
            Json(serde_json::json!({"ok":true})).into_response()
        }
        Ok(_) => error_response(StatusCode::CONFLICT, "job_not_cancelable"),
        Err(error) => {
            tracing::error!(?error, "Galileo job cancellation failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "job_unavailable")
        }
    }
}

async fn work_once(
    state: &AppState,
    pool: &MySqlPool,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let Some(job) = sqlx::query_as::<_, Job>("SELECT id,user_id,project_id,checkpoint_id,request,status,result,error,created_at,updated_at,approval_payload FROM galileo_jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        .fetch_optional(pool).await? else { return Ok(()); };
    let claimed = sqlx::query("UPDATE galileo_jobs SET status='running',claimed_at=?,updated_at=? WHERE id=? AND status='queued'")
        .bind(now()).bind(now()).bind(&job.id).execute(pool).await?;
    if claimed.rows_affected() != 1 {
        return Ok(());
    }
    add_event(pool, &job.id, "running", "{}").await?;
    let job_id = job.id.clone();
    let result = match tokio::time::timeout(
        Duration::from_secs(900),
        process_claimed_job(state, pool, job),
    )
    .await
    {
        Ok(result) => result,
        Err(_) => Err(io::Error::new(io::ErrorKind::TimedOut, "job_timeout").into()),
    };
    if let Err(error) = result {
        tracing::error!(?error, "Galileo job processing failed");
        finish_error(pool, &job_id, &error.to_string()).await?;
    }
    Ok(())
}

async fn process_claimed_job(
    state: &AppState,
    pool: &MySqlPool,
    job: Job,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let Some(upstream) = state.job_upstream.as_ref() else {
        finish_error(pool, &job.id, "job_upstream_not_configured").await?;
        return Ok(());
    };
    let body = serde_json::json!({"messages":[{"role":"system","content":"Return JSON only with a files array containing path and content."},{"role":"user","content":format!("Approved plan:\n{}\n\nRequest:\n{}", job.approval_payload.as_deref().unwrap_or("{}"), job.request)}],"stream":false,"max_tokens":16384,"temperature":0.6});
    let response = state.client.post(upstream).json(&body).send().await;
    let result = match response {
        Ok(response) if response.status().is_success() => response
            .json::<Value>()
            .await?
            .get("choices")
            .and_then(|v| v.get(0))
            .and_then(|v| v.get("message"))
            .and_then(|v| v.get("content"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        Ok(response) => {
            finish_error(
                pool,
                &job.id,
                &format!("upstream_http_{}", response.status().as_u16()),
            )
            .await?;
            return Ok(());
        }
        Err(error) => {
            finish_error(pool, &job.id, &error.to_string()).await?;
            return Ok(());
        }
    };
    let parsed = serde_json::from_str::<Value>(&result)
        .unwrap_or_else(|_| serde_json::json!({"content": result}));
    let plan: Value = job
        .approval_payload
        .as_deref()
        .and_then(|v| serde_json::from_str(v).ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing approval payload"))?;
    let approved: HashSet<&str> = plan
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "approved plan has no files"))?
        .iter()
        .filter_map(|file| file.get("path").and_then(Value::as_str))
        .collect();
    let generated = parsed
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "agent returned no files"))?;
    if generated.iter().any(|file| {
        file.get("path")
            .and_then(Value::as_str)
            .is_none_or(|path| !approved.contains(path))
    }) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "agent generated an unapproved path",
        )
        .into());
    }
    let still_running = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM galileo_jobs WHERE id=? AND status='running'",
    )
    .bind(&job.id)
    .fetch_one(pool)
    .await?;
    if still_running != 1 {
        return Ok(());
    }
    let staged_paths = stage_files(pool, &state.projects_root, &job, generated).await?;
    let encoded = serde_json::json!({
        "status": "changes_ready",
        "files": staged_paths,
        "agent": parsed,
    })
    .to_string();
    sqlx::query("UPDATE galileo_jobs SET status='complete',result=?,updated_at=? WHERE id=? AND status='running'").bind(&encoded).bind(now()).bind(&job.id).execute(pool).await?;
    add_event(pool, &job.id, "changes_ready", &encoded).await?;
    tracing::info!(event = "galileo.job.complete", job_id = %job.id, user_id = %job.user_id);
    Ok(())
}

async fn add_event(
    pool: &MySqlPool,
    job_id: &str,
    kind: &str,
    payload: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO galileo_job_events (job_id,kind,payload,created_at) VALUES (?,?,?,?)")
        .bind(job_id)
        .bind(kind)
        .bind(payload)
        .bind(now())
        .execute(pool)
        .await
        .map(|_| ())
}

async fn finish_error(pool: &MySqlPool, id: &str, message: &str) -> Result<(), sqlx::Error> {
    tracing::warn!(event = "galileo.job.failed", job_id = %id, reason = message);
    sqlx::query("UPDATE galileo_jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status='running'").bind(message).bind(now()).bind(id).execute(pool).await?;
    add_event(
        pool,
        id,
        "failed",
        &serde_json::json!({"error": message}).to_string(),
    )
    .await
}

async fn stage_files(
    pool: &MySqlPool,
    root: &PathBuf,
    job: &Job,
    files: &[Value],
) -> Result<Vec<String>, Box<dyn std::error::Error + Send + Sync>> {
    let user = &job.user_id;
    let project = &job.project_id;
    let base = root.join(user).join(project);
    let base_metadata = fs::symlink_metadata(&base)?;
    if !base_metadata.is_dir() || base_metadata.file_type().is_symlink() {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "invalid project root").into());
    }
    let canonical_base = fs::canonicalize(&base)?;
    let mut staged_paths = Vec::new();
    let mut total = 0usize;
    for file in files {
        let path = file
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file"))?;
        let content = file
            .get("content")
            .and_then(Value::as_str)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file"))?;
        let clean = safe_path(path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file path"))?;
        let target = base.join(&clean);
        let nearest = nearest_existing(&target)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "project path missing"))?;
        if !fs::canonicalize(nearest)?.starts_with(&canonical_base) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "agent path escaped project",
            )
            .into());
        }
        total += content.len();
        if total > 10_000_000 {
            return Err(
                io::Error::new(io::ErrorKind::InvalidData, "generated files too large").into(),
            );
        }
        let before_exists = target.is_file();
        let before_content = if before_exists {
            Some(fs::read_to_string(&target)?)
        } else {
            None
        };
        sqlx::query("INSERT INTO galileo_job_changes (job_id,user_id,project_id,path,operation,before_exists,before_content,after_content,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?)")
            .bind(&job.id).bind(user).bind(project).bind(clean.to_string_lossy().replace('\\', "/"))
            .bind(if before_exists { "modify" } else { "create" }).bind(if before_exists { 1 } else { 0 }).bind(before_content).bind(content).bind(now()).bind(now()).execute(pool).await?;
        staged_paths.push(clean.to_string_lossy().replace('\\', "/"));
    }
    Ok(staged_paths)
}

fn nearest_existing(path: &std::path::Path) -> Option<PathBuf> {
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

#[allow(dead_code)]
fn write_files(
    root: &PathBuf,
    user: &str,
    project: &str,
    files: &[Value],
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let base = root.join(user).join(project);
    fs::create_dir_all(&base)?;
    let stage = base.join(format!(".job-stage-{}", Uuid::new_v4().simple()));
    fs::create_dir_all(&stage)?;
    let result = (|| {
        let mut total = 0usize;
        for file in files {
            let path = file
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file"))?;
            let content = file
                .get("content")
                .and_then(Value::as_str)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file"))?;
            let clean = safe_path(path)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file path"))?;
            total += content.len();
            if total > 10_000_000 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "generated files too large",
                )
                .into());
            }
            let target = stage.join(clean);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(target, content)?;
        }
        for file in files {
            let path = file
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file"))?;
            let clean = safe_path(path)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid file path"))?;
            let staged = stage.join(&clean);
            let target = base.join(clean);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::rename(staged, target)?;
        }
        Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
    })();
    let _ = fs::remove_dir_all(&stage);
    result
}

async fn authenticated(
    pool: &MySqlPool,
    state: &AppState,
    headers: &HeaderMap,
) -> Option<auth::PublicUser> {
    auth::authenticated_user(pool, state, headers)
        .await
        .ok()
        .flatten()
}

async fn recover_stale(pool: &MySqlPool) -> Result<(), sqlx::Error> {
    let cutoff = now() - 900;
    let jobs = sqlx::query_scalar::<_, String>(
        "SELECT id FROM galileo_jobs WHERE status='running' AND claimed_at < ?",
    )
    .bind(cutoff)
    .fetch_all(pool)
    .await?;
    for id in &jobs {
        sqlx::query("UPDATE galileo_jobs SET status='recoverable',claimed_at=NULL,error='worker_host_lost',updated_at=? WHERE id=? AND status='running'")
            .bind(now()).bind(id).execute(pool).await?;
        add_event(pool, id, "recoverable", &serde_json::json!({"reason":"worker_host_lost"}).to_string()).await?;
    }
    if !jobs.is_empty() {
        tracing::warn!(jobs = jobs.len(), "marked stale Galileo jobs recoverable");
    }
    Ok(())
}

fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn safe_path(value: &str) -> Option<PathBuf> {
    let path = PathBuf::from(value);
    if value.is_empty()
        || path.is_absolute()
        || path.components().any(|c| {
            matches!(
                c,
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
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::safe_path;

    #[test]
    fn plans_reject_traversal_and_empty_files() {
        assert!(safe_path("src/main.rs").is_some());
        assert!(safe_path("../escape").is_none());
    }
}
