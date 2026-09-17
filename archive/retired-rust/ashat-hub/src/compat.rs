use std::{
    fs,
    path::{Component, Path, PathBuf},
};

use crate::{auth, response::error_response, AppState};
use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, Row};

const MAX_CONTEXT_BYTES: usize = 24_000;
const MAX_SKILL_CONTENT: usize = 200_000;

#[derive(Debug, Deserialize)]
struct FileQuery {
    project_id: Option<String>,
    path: Option<String>,
}

#[derive(Debug, Deserialize)]
struct LegacyFileRequest {
    project_id: String,
    path: String,
    content: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SkillQuery {
    name: Option<String>,
    category: Option<String>,
    q: Option<String>,
    limit: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct SkillWrite {
    name: String,
    category: Option<String>,
    content: String,
}

#[derive(Debug, Deserialize)]
struct SsoRequest {
    session_id: String,
}

#[derive(Debug, Serialize, FromRow)]
struct SkillSummary {
    name: String,
    category: String,
    tokens_estimated: i64,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/context", get(context))
        .route("/api/files", get(file_list).post(file_save))
        .route("/api/files/read", get(file_read))
        .route("/api/files/rename", post(file_rename))
        .route("/api/files/duplicate", post(file_duplicate))
        .route("/api/files/tree", delete(file_tree_delete))
        .route("/api/files/:id", delete(file_delete))
        .route("/api/skills", get(skill_list))
        .route("/api/skills", post(skill_write))
        .route("/api/skills/:name", delete(skill_delete))
        .route("/api/sso/verify-session", post(sso_verify_session))
}

async fn context(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<FileQuery>,
) -> Response {
    let Some(root) = selected_project(&state, &user.id, query.project_id.as_deref()) else {
        return Json(serde_json::json!({
            "context": {"files": [], "stats": {"files": 0}}
        }))
        .into_response();
    };
    let mut files = Vec::new();
    collect_context(&root, &root, &mut files, MAX_CONTEXT_BYTES);
    Json(serde_json::json!({
        "context": {
            "files": files,
            "stats": {"files": files.len()}
        }
    }))
    .into_response()
}

async fn file_list(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<FileQuery>,
) -> Response {
    let Some(root) = selected_project(&state, &user.id, query.project_id.as_deref()) else {
        return error_response(StatusCode::NOT_FOUND, "project_not_found");
    };
    let mut files = Vec::new();
    collect_file_entries(&root, &root, &mut files);
    Json(serde_json::json!({"files": files})).into_response()
}

async fn file_read(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Query(query): Query<FileQuery>,
) -> Response {
    let Some(project_id) = query.project_id.as_deref() else {
        return error_response(StatusCode::BAD_REQUEST, "project_required");
    };
    let Some(path) = query.path.as_deref() else {
        return error_response(StatusCode::BAD_REQUEST, "path_required");
    };
    let Some(target) = safe_project_path(&state, &user.id, project_id, path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::read_to_string(&target) {
        Ok(content) if content.len() <= 2_000_000 => {
            Json(serde_json::json!({"path": path, "content": content})).into_response()
        }
        Ok(_) => error_response(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable"),
    }
}

async fn file_save(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<LegacyFileRequest>,
) -> Response {
    let Some(content) = input.content else {
        return error_response(StatusCode::BAD_REQUEST, "content_required");
    };
    if content.len() > 2_000_000 {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large");
    }
    let Some(target) = safe_project_path(&state, &user.id, &input.project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    let Some(parent) = target.parent() else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    if fs::create_dir_all(parent).is_err() || fs::write(&target, content).is_err() {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable");
    }
    Json(serde_json::json!({"ok": true, "path": input.path})).into_response()
}

async fn file_rename(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<MoveRequest>,
) -> Response {
    move_file(&state, &user.id, &input, false)
}

async fn file_duplicate(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<MoveRequest>,
) -> Response {
    move_file(&state, &user.id, &input, true)
}

async fn file_delete(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Response {
    // Legacy clients used the database file id. Rust projects use relative
    // paths, so accept only an explicitly prefixed project/path identifier.
    let Some((project_id, path)) = id.split_once(':') else {
        return error_response(StatusCode::BAD_REQUEST, "path_identifier_required");
    };
    let Some(target) = safe_project_path(&state, &user.id, project_id, path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::remove_file(target) {
        Ok(()) => Json(serde_json::json!({"ok": true})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable"),
    }
}

async fn file_tree_delete(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<PathRequest>,
) -> Response {
    let Some(target) = safe_project_path(&state, &user.id, &input.project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::remove_dir_all(target) {
        Ok(()) => Json(serde_json::json!({"ok": true})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "folder_not_found")
        }
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable"),
    }
}

#[derive(Debug, Deserialize)]
struct PathRequest {
    project_id: String,
    path: String,
}

#[derive(Debug, Deserialize)]
struct MoveRequest {
    project_id: String,
    path: String,
    new_path: String,
}

fn move_file(state: &AppState, user_id: &str, input: &MoveRequest, duplicate: bool) -> Response {
    let Some(source) = safe_project_path(state, user_id, &input.project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    let Some(target) = safe_project_path(state, user_id, &input.project_id, &input.new_path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    if target.exists() {
        return error_response(StatusCode::CONFLICT, "file_exists");
    }
    if let Some(parent) = target.parent() {
        if fs::create_dir_all(parent).is_err() {
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable");
        }
    }
    let result = if duplicate {
        fs::copy(source, target).map(|_| ())
    } else {
        fs::rename(source, target)
    };
    match result {
        Ok(()) => Json(serde_json::json!({"ok": true, "path": input.new_path})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "file_unavailable"),
    }
}

async fn skill_list(State(state): State<AppState>, Query(query): Query<SkillQuery>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let limit = query.limit.unwrap_or(10).clamp(1, 50) as i64;
    if let Some(name) = query
        .name
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        match sqlx::query("SELECT name,category,content,tokens_estimated FROM agent_skills WHERE name=? LIMIT 1")
            .bind(name.trim()).fetch_optional(pool).await {
            Ok(Some(row)) => Json(serde_json::json!({
                "ok": true,
                "skill": {
                    "name": row.try_get::<String, _>("name").unwrap_or_default(),
                    "category": row.try_get::<String, _>("category").unwrap_or_default(),
                    "content": row.try_get::<String, _>("content").unwrap_or_default(),
                    "tokens_estimated": row.try_get::<i64, _>("tokens_estimated").unwrap_or_default()
                }
            })).into_response(),
            Ok(None) => error_response(StatusCode::NOT_FOUND, "skill_not_found"),
            Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "skills_unavailable"),
        }
    } else {
        let q = query.q.unwrap_or_default().trim().to_owned();
        let category = query.category.unwrap_or_default().trim().to_owned();
        let result = sqlx::query_as::<_, SkillSummary>(
            "SELECT name,category,tokens_estimated FROM agent_skills
             WHERE (?='' OR category=?)
               AND (?='' OR name LIKE CONCAT('%',?,'%') OR content LIKE CONCAT('%',?,'%'))
             ORDER BY category,name LIMIT ?",
        )
        .bind(&category)
        .bind(&category)
        .bind(&q)
        .bind(&q)
        .bind(&q)
        .bind(limit)
        .fetch_all(pool)
        .await;
        match result {
            Ok(skills) => {
                Json(serde_json::json!({"ok": true, "skills": skills, "count": skills.len()}))
                    .into_response()
            }
            Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "skills_unavailable"),
        }
    }
}

async fn skill_write(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    Json(input): Json<SkillWrite>,
) -> Response {
    let name = input.name.trim();
    let category = input.category.as_deref().unwrap_or("general").trim();
    if name.is_empty()
        || name.len() > 191
        || category.is_empty()
        || category.len() > 64
        || input.content.is_empty()
        || input.content.len() > MAX_SKILL_CONTENT
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_skill");
    }
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let tokens = ((input.content.len() as f64) / 4.0).ceil() as i64;
    match sqlx::query("INSERT INTO agent_skills (name,category,content,tokens_estimated) VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE category=VALUES(category),content=VALUES(content),tokens_estimated=VALUES(tokens_estimated)")
        .bind(name).bind(category).bind(&input.content).bind(tokens).execute(pool).await {
        Ok(_) => Json(serde_json::json!({"ok": true, "name": name, "tokens_estimated": tokens})).into_response(),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "skills_unavailable"),
    }
}

async fn skill_delete(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    axum::extract::Path(name): axum::extract::Path<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    match sqlx::query("DELETE FROM agent_skills WHERE name=?")
        .bind(&name)
        .execute(pool)
        .await
    {
        Ok(result) => Json(
            serde_json::json!({"ok": true, "deleted": result.rows_affected() > 0, "name": name}),
        )
        .into_response(),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "skills_unavailable"),
    }
}

async fn sso_verify_session(
    State(state): State<AppState>,
    _: auth::ServiceRequest,
    Json(input): Json<SsoRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    if input.session_id.trim().is_empty() || input.session_id.len() > 191 {
        return error_response(StatusCode::BAD_REQUEST, "missing_session_id");
    }
    let row = sqlx::query("SELECT u.id,u.username,u.email,u.display_name,u.role,u.is_active,s.expires_at FROM sessions s INNER JOIN users u ON u.id=s.user_id WHERE s.id=? LIMIT 1")
        .bind(input.session_id.trim()).fetch_optional(pool).await;
    match row {
        Ok(Some(row)) if row.try_get::<i8, _>("is_active").unwrap_or(0) == 1 => {
            Json(serde_json::json!({
                "valid": true,
                "user_id": row.try_get::<String, _>("id").unwrap_or_default(),
                "username": row.try_get::<String, _>("username").unwrap_or_default(),
                "role": row.try_get::<String, _>("role").unwrap_or_default(),
                "display_name": row.try_get::<String, _>("display_name").unwrap_or_default(),
                "session_expires_at": row.try_get::<String, _>("expires_at").unwrap_or_default()
            }))
            .into_response()
        }
        Ok(Some(_)) | Ok(None) => {
            Json(serde_json::json!({"valid": false, "reason": "not_found_or_expired"}))
                .into_response()
        }
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable"),
    }
}

fn selected_project(state: &AppState, user_id: &str, project_id: Option<&str>) -> Option<PathBuf> {
    let user_root = state.projects_root.join(user_id);
    if let Some(project_id) = project_id {
        if !safe_segment(project_id) {
            return None;
        }
        let root = user_root.join(project_id);
        return valid_dir(&root).then_some(root);
    }
    let mut projects = fs::read_dir(user_root)
        .ok()?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let path = entry.path();
            valid_dir(&path).then_some(path)
        })
        .collect::<Vec<_>>();
    projects.sort();
    projects.into_iter().next()
}

fn safe_project_path(
    state: &AppState,
    user_id: &str,
    project_id: &str,
    relative: &str,
) -> Option<PathBuf> {
    if !safe_segment(user_id) || !safe_segment(project_id) || !safe_relative(relative) {
        return None;
    }
    let root = state.projects_root.join(user_id).join(project_id);
    if !valid_dir(&root) {
        return None;
    }
    let candidate = root.join(relative);
    let existing = nearest_existing(&candidate)?;
    let canonical_root = fs::canonicalize(root).ok()?;
    let canonical_existing = fs::canonicalize(existing).ok()?;
    canonical_existing
        .starts_with(canonical_root)
        .then_some(candidate)
}

fn valid_dir(path: &Path) -> bool {
    fs::symlink_metadata(path)
        .map(|metadata| metadata.is_dir() && !metadata.file_type().is_symlink())
        .unwrap_or(false)
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
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn safe_relative(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value
            .split('/')
            .any(|segment| segment.is_empty() || segment.starts_with('.') || segment == "..")
        && !Path::new(value).is_absolute()
        && !Path::new(value).components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
}

fn collect_context(root: &Path, current: &Path, files: &mut Vec<serde_json::Value>, budget: usize) {
    let Ok(entries) = fs::read_dir(current) else {
        return;
    };
    let mut entries = entries.filter_map(Result::ok).collect::<Vec<_>>();
    entries.sort_by_key(|entry| entry.file_name());
    let used = files
        .iter()
        .filter_map(|item| item.get("content").and_then(|value| value.as_str()))
        .map(str::len)
        .sum::<usize>();
    for entry in entries {
        if used >= budget {
            return;
        }
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_symlink()
            || entry.file_name() == ".meta.json"
            || entry.file_name().to_string_lossy().starts_with('.')
        {
            continue;
        }
        if file_type.is_dir() {
            collect_context(root, &path, files, budget);
            continue;
        }
        if !file_type.is_file() {
            continue;
        }
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };
        let relative = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        let remaining = budget.saturating_sub(
            files
                .iter()
                .filter_map(|item| item.get("content").and_then(|value| value.as_str()))
                .map(str::len)
                .sum::<usize>(),
        );
        let excerpt = content
            .chars()
            .take(remaining.min(2_000))
            .collect::<String>();
        files.push(serde_json::json!({"path": relative, "content": excerpt}));
    }
}

fn collect_file_entries(root: &Path, current: &Path, files: &mut Vec<serde_json::Value>) {
    let Ok(entries) = fs::read_dir(current) else {
        return;
    };
    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_symlink()
            || entry.file_name() == ".meta.json"
            || entry.file_name().to_string_lossy().starts_with('.')
        {
            continue;
        }
        if file_type.is_dir() {
            collect_file_entries(root, &path, files);
        } else if file_type.is_file() {
            files.push(serde_json::json!({"path": path.strip_prefix(root).unwrap_or(&path).to_string_lossy().replace('\\', "/"), "size": entry.metadata().map(|metadata| metadata.len()).unwrap_or(0)}));
        }
    }
}
