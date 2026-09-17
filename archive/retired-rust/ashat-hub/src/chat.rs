use std::{
    fs,
    path::{Component, Path, PathBuf},
};

use axum::{
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::{
    auth,
    response::{error_response, rate_limit_response},
    AppState,
};

#[derive(Debug, Deserialize, Serialize)]
struct ChatRequest {
    project_id: Option<String>,
    conversation_id: Option<String>,
    message: String,
    active_file: Option<String>,
    stream: Option<bool>,
}

#[derive(Debug, Serialize)]
struct ChatResponse {
    content: String,
}

#[derive(Debug, Deserialize)]
struct IcarusRequest {
    message: String,
    project_root: String,
    context: Value,
}

#[derive(Debug, Deserialize, Serialize)]
struct DiscoveryResponse {
    kind: String,
    content: String,
    plan: Option<Value>,
    plan_id: Option<String>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/chat", post(chat))
        .route("/api/galileo/chat/stream", post(chat))
        .route("/api/galileo/discovery", post(discovery))
        .route("/api/icarus/agent", post(icarus_agent))
}

async fn icarus_agent(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<IcarusRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = auth::authenticated_user(pool, &state, &headers)
        .await
        .ok()
        .flatten()
    else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };

    let message = input.message.trim();
    if message.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "message_required");
    }
    if message.chars().count() > 50_000
        || input.project_root.is_empty()
        || input.project_root.len() > 1_024
        || !input.project_root.is_ascii()
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_icarus_request");
    }
    let context_size = serde_json::to_string(&input.context)
        .map(|value| value.len())
        .unwrap_or(usize::MAX);
    if context_size > 200_000 || !input.context.is_object() {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "context_too_large");
    }
    if let Some(retry_after) = state.operation_rate_limiter.check(
        &format!("icarus:{}", user.id),
        30,
        std::time::Duration::from_secs(60),
    ) {
        state.metrics.record_rate_limited();
        return rate_limit_response(retry_after);
    }

    let Some(upstream) = state.chat_upstream.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "chat_engine_not_configured");
    };
    let prompt = format!(
        "LOCAL WORKSPACE METADATA (do not access this path; Icarus owns local filesystem operations):\\nroot: {}\\ncontext: {}\\n\\nUSER REQUEST:\\n{}",
        input.project_root,
        serde_json::to_string(&input.context).unwrap_or_default(),
        message
    );
    let body = serde_json::json!({
        "model": "local",
        "messages": [
            {"role": "system", "content": "You are Ashat, the coding AI. Give precise, practical answers using only the supplied workspace context."},
            {"role": "user", "content": prompt}
        ],
        "stream": false
    });
    let response = match state.client.post(upstream).json(&body).send().await {
        Ok(response) if response.status().is_success() => response,
        Ok(_) => return error_response(StatusCode::BAD_GATEWAY, "chat_engine_error"),
        Err(error) => {
            tracing::warn!(?error, "Icarus adapter upstream request failed");
            return error_response(StatusCode::BAD_GATEWAY, "chat_engine_unavailable");
        }
    };
    let body = match response.json::<Value>().await {
        Ok(body) => body,
        Err(_) => return error_response(StatusCode::BAD_GATEWAY, "chat_engine_invalid_response"),
    };
    let content = body
        .get("content")
        .and_then(Value::as_str)
        .or_else(|| body.get("choices").and_then(|value| value.get(0)).and_then(|value| value.get("message")).and_then(|value| value.get("content")).and_then(Value::as_str))
        .unwrap_or_default();
    if content.is_empty() {
        return error_response(StatusCode::BAD_GATEWAY, "chat_engine_empty_response");
    }
    Json(ChatResponse { content: content.to_owned() }).into_response()
}

async fn discovery(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<ChatRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = auth::authenticated_user(pool, &state, &headers)
        .await
        .ok()
        .flatten()
    else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let message = input.message.trim();
    if message.is_empty() || message.len() > 50_000 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_message");
    }
    let Some(project_id) = input
        .project_id
        .as_deref()
        .filter(|value| !value.is_empty())
    else {
        return error_response(StatusCode::BAD_REQUEST, "project_required");
    };
    if !safe_segment(project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    if let Some(retry_after) = state.operation_rate_limiter.check(
        &format!("discovery:{}", user.id),
        20,
        std::time::Duration::from_secs(60),
    ) {
        state.metrics.record_rate_limited();
        return rate_limit_response(retry_after);
    }
    if message.chars().count() > 2_000 {
        if let Err(error) = save_request_artifact(&state, &user.id, project_id, message) {
            tracing::warn!(?error, "unable to save Galileo request artifact");
        }
    }
    let context = inspect_project(&state, &user.id, project_id);
    let prompt = format!(
        "Review this project literally. Do not invent requirements.\n\n{}\n\nUSER REQUEST:\n{}\n\nIf requirements are missing, return JSON {{\"kind\":\"clarification\",\"content\":\"questions\"}}. If sufficient, return JSON {{\"kind\":\"plan\",\"content\":\"summary\",\"plan\":{{\"summary\":\"...\",\"architecture\":\"...\",\"files\":[{{\"path\":\"relative/path\",\"purpose\":\"...\"}}]}}}}.",
        context, message
    );
    let Some(upstream) = state.planner_upstream.as_ref() else {
        return Json(DiscoveryResponse {
            kind: "clarification".to_owned(),
            content: fallback_questions(message, &context),
            plan: None,
            plan_id: None,
        })
        .into_response();
    };
    let body = serde_json::json!({
        "messages": [
            {"role": "system", "content": "You are Ashat, a careful software architect. Return JSON only."},
            {"role": "user", "content": prompt}
        ],
        "stream": false,
        "temperature": 0.2,
        "max_tokens": 2048
    });
    let response = match state.client.post(upstream).json(&body).send().await {
        Ok(response) if response.status().is_success() => response,
        Ok(_) => return error_response(StatusCode::BAD_GATEWAY, "planner_error"),
        Err(error) => {
            tracing::warn!(?error, "Galileo planner request failed");
            return error_response(StatusCode::BAD_GATEWAY, "planner_unavailable");
        }
    };
    let parsed = match response.json::<Value>().await {
        Ok(value) => value,
        Err(_) => return error_response(StatusCode::BAD_GATEWAY, "planner_invalid_response"),
    };
    let text = parsed
        .get("choices")
        .and_then(|v| v.get(0))
        .and_then(|v| v.get("message"))
        .and_then(|v| v.get("content"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_default();
    let mut result =
        serde_json::from_str::<DiscoveryResponse>(&text).unwrap_or(DiscoveryResponse {
            kind: "clarification".to_owned(),
            content: text.clone(),
            plan: None,
            plan_id: None,
        });
    if result.kind == "plan" {
        let Some(plan) = result.plan.as_ref() else {
            return error_response(StatusCode::BAD_GATEWAY, "planner_invalid_plan");
        };
        if !valid_plan(Some(plan)) {
            return error_response(StatusCode::BAD_GATEWAY, "planner_invalid_plan");
        }
        let plan_id = format!("plan_{}", Uuid::new_v4().simple());
        if let Err(error) = save_plan(pool, &plan_id, &user.id, project_id, message, plan).await {
            tracing::error!(?error, "Galileo plan persistence failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "plan_unavailable");
        }
        result.plan_id = Some(plan_id);
    }
    Json(result).into_response()
}

async fn chat(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<ChatRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    match auth::authenticated_user(pool, &state, &headers).await {
        Ok(Some(_)) => {}
        Ok(None) => return error_response(StatusCode::UNAUTHORIZED, "unauthenticated"),
        Err(error) => {
            tracing::error!(?error, "chat authentication lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    }

    let message = input.message.trim().to_owned();
    if message.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "message_required");
    }
    if message.chars().count() > 50_000 {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "message_too_large");
    }
    if let Some(project_id) = input.project_id.as_deref() {
        if !project_id.is_empty()
            && !project_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
        {
            return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
        }
    }

    let Some(user) = auth::authenticated_user(pool, &state, &headers)
        .await
        .ok()
        .flatten()
    else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let conversation_id = input.conversation_id.clone().unwrap_or_default();
    if !conversation_id.is_empty() && !safe_id(&conversation_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_conversation_id");
    }
    if !conversation_id.is_empty() {
        let owned = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM conversations WHERE id=? AND user_id=? AND project_id=?",
        )
        .bind(&conversation_id)
        .bind(&user.id)
        .bind(input.project_id.as_deref().unwrap_or_default())
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if owned != 1 {
            return error_response(StatusCode::NOT_FOUND, "conversation_not_found");
        }
    }
    let stream = input.stream.unwrap_or(false);
    let _ = append_message(pool, &conversation_id, &user.id, "user", &message).await;
    tracing::info!(event = "galileo.chat.request", user_id = %user.id, conversation_id = %conversation_id);
    if let Some(retry_after) = state.operation_rate_limiter.check(
        &format!("chat:{}", user.id),
        30,
        std::time::Duration::from_secs(60),
    ) {
        state.metrics.record_rate_limited();
        return rate_limit_response(retry_after);
    }

    let Some(upstream_url) = state.chat_upstream.as_ref() else {
        return error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "chat_engine_not_configured",
        );
    };

    let mut request = input;
    request.message = message;
    request.stream = Some(stream);
    let mut upstream = state
        .client
        .post(upstream_url)
        .header(header::CONTENT_TYPE, "application/json")
        .header(
            header::ACCEPT,
            if stream {
                "text/event-stream"
            } else {
                "application/json"
            },
        )
        .json(&request);

    // Preserve the legacy PHP session during the strangler migration. The
    // Rust session remains authoritative for this gateway, but the temporary
    // engine adapter may still be the PHP chat endpoint.
    if let Some(cookie) = headers.get(header::COOKIE) {
        upstream = upstream.header(header::COOKIE, cookie.clone());
    }
    if let Some(csrf) = headers.get("x-csrf-token") {
        upstream = upstream.header("x-csrf-token", csrf.clone());
    }

    let response = match upstream.send().await {
        Ok(response) => response,
        Err(error) => {
            tracing::warn!(?error, "Galileo chat engine request failed");
            return error_response(StatusCode::BAD_GATEWAY, "chat_engine_unavailable");
        }
    };
    if !response.status().is_success() {
        tracing::warn!(status = %response.status(), "Galileo chat engine returned an error");
        return error_response(StatusCode::BAD_GATEWAY, "chat_engine_error");
    }

    if !stream {
        let body = match response.json::<Value>().await {
            Ok(body) => body,
            Err(error) => {
                tracing::warn!(?error, "Galileo chat engine returned invalid JSON");
                return error_response(StatusCode::BAD_GATEWAY, "chat_engine_invalid_response");
            }
        };
        let content = body
            .get("content")
            .and_then(Value::as_str)
            .or_else(|| {
                body.get("choices")
                    .and_then(|value| value.get(0))
                    .and_then(|value| value.get("message"))
                    .and_then(|value| value.get("content"))
                    .and_then(Value::as_str)
            })
            .unwrap_or_default()
            .to_owned();
        if content.is_empty() {
            return error_response(StatusCode::BAD_GATEWAY, "chat_engine_empty_response");
        }
        let _ = append_message(pool, &conversation_id, &user.id, "assistant", &content).await;
        return Json(ChatResponse { content }).into_response();
    }

    let mut builder = Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/event-stream")
        .header(header::CACHE_CONTROL, "no-cache")
        .header("x-accel-buffering", "no")
        .header(header::CONNECTION, "keep-alive");
    if let Some(value) = response.headers().get(header::CONTENT_TYPE) {
        if let Ok(value) = value.to_str() {
            if value.starts_with("text/event-stream") {
                builder = builder.header(header::CONTENT_TYPE, value);
            }
        }
    }
    match builder.body(axum::body::Body::from_stream(response.bytes_stream())) {
        Ok(response) => response,
        Err(error) => {
            tracing::error!(?error, "unable to construct chat stream response");
            error_response(StatusCode::INTERNAL_SERVER_ERROR, "chat_stream_error")
        }
    }
}

async fn save_plan(
    pool: &sqlx::MySqlPool,
    id: &str,
    user_id: &str,
    project_id: &str,
    request: &str,
    plan: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO galileo_plans (id,user_id,project_id,request,payload,status,created_at) VALUES (?,?,?,?,?,'pending',?)")
        .bind(id).bind(user_id).bind(project_id).bind(request).bind(serde_json::to_string(plan).unwrap_or_default()).bind(now()).execute(pool).await?;
    Ok(())
}

fn save_request_artifact(
    state: &AppState,
    user_id: &str,
    project_id: &str,
    request: &str,
) -> std::io::Result<()> {
    let root = state.projects_root.join(user_id).join(project_id);
    let metadata = fs::symlink_metadata(&root)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "project root is not a directory",
        ));
    }
    let filename = if root.join("Spec.md").exists() {
        "Build.md"
    } else {
        "Spec.md"
    };
    let bounded: String = request.chars().take(200_000).collect();
    fs::write(root.join(filename), bounded)
}

fn inspect_project(state: &AppState, user_id: &str, project_id: &str) -> String {
    let root = state.projects_root.join(user_id).join(project_id);
    let mut files = Vec::new();
    collect_context(&root, &root, &mut files, 24_000);
    if files.is_empty() {
        return "PROJECT INVENTORY: empty".to_owned();
    }
    format!("PROJECT INVENTORY:\n{}", files.join("\n"))
}

fn collect_context(root: &Path, current: &Path, files: &mut Vec<String>, budget: usize) {
    let Ok(entries) = fs::read_dir(current) else {
        return;
    };
    let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        if files.join("\n").len() >= budget {
            return;
        }
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_symlink() || entry.file_name() == ".meta.json" {
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
        let entry = format!(
            "--- {} ---\n{}",
            relative,
            content.chars().take(4_000).collect::<String>()
        );
        if files.join("\n").len() + entry.len() <= budget {
            files.push(entry);
        }
    }
}

fn fallback_questions(message: &str, context: &str) -> String {
    let lower = format!("{} {}", message, context).to_lowercase();
    let mut questions = Vec::new();
    if !lower.contains("vite") && !lower.contains("react") && !lower.contains("html") {
        questions.push("What frontend stack should this use?");
    }
    if !lower.contains("auth") && !lower.contains("public") {
        questions.push("Should this require authentication or be public?");
    }
    if !lower.contains("database") && !lower.contains("api") {
        questions.push("Does it need persistent data or an external API?");
    }
    if questions.is_empty() {
        "Please confirm the required behavior and acceptance criteria.".to_owned()
    } else {
        questions.join("\\n")
    }
}

fn valid_plan(plan: Option<&Value>) -> bool {
    plan.and_then(|value| value.get("files"))
        .and_then(Value::as_array)
        .is_some_and(|files| {
            !files.is_empty()
                && files.iter().all(|file| {
                    file.get("path")
                        .and_then(Value::as_str)
                        .is_some_and(|path| safe_path(path).is_some())
                        && file
                            .get("purpose")
                            .and_then(Value::as_str)
                            .is_some_and(|purpose| !purpose.trim().is_empty())
                })
        })
}

async fn append_message(
    pool: &sqlx::MySqlPool,
    conversation_id: &str,
    user_id: &str,
    role: &str,
    content: &str,
) -> Result<(), sqlx::Error> {
    if conversation_id.is_empty() || content.is_empty() {
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO conversation_messages (conversation_id, role, content, created_at)
         SELECT ?, ?, ?, UTC_TIMESTAMP() FROM conversations WHERE id=? AND user_id=?",
    )
    .bind(conversation_id)
    .bind(role)
    .bind(content)
    .bind(conversation_id)
    .bind(user_id)
    .execute(pool)
    .await
    .map(|_| ())?;
    sqlx::query("UPDATE conversations SET updated_at=UTC_TIMESTAMP() WHERE id=? AND user_id=?")
        .bind(conversation_id)
        .bind(user_id)
        .execute(pool)
        .await
        .map(|_| ())
}

fn safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() || byte == b'-' || byte == b'_')
}

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
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

#[cfg(test)]
mod tests {
    use super::{safe_id, safe_path};

    #[test]
    fn chat_paths_and_conversation_ids_are_bounded() {
        assert!(safe_path("src/main.rs").is_some());
        assert!(safe_path("../escape").is_none());
        assert!(safe_id("550e8400-e29b-41d4-a716-446655440000"));
        assert!(!safe_id("../conversation"));
        assert!(!safe_id(""));
    }
}
