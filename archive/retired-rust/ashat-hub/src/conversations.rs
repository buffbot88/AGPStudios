use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, MySqlPool};
use uuid::Uuid;

use crate::{auth, response::error_response, AppState};

#[derive(Debug, Deserialize)]
struct ArchivedQuery {
    archived: Option<u8>,
}

#[derive(Debug, Deserialize)]
struct CreateConversationRequest {
    project_id: String,
    title: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MessageInput {
    role: String,
    content: String,
    ts: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct AddMessagesRequest {
    messages: Vec<MessageInput>,
}

#[derive(Debug, Deserialize)]
struct RenameRequest {
    title: String,
}

#[derive(Debug, Deserialize)]
struct ArchiveRequest {
    archived: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct SyncRequest {
    project_id: String,
    conversations: Vec<LocalConversation>,
}

#[derive(Debug, Deserialize)]
struct LocalConversation {
    title: Option<String>,
    messages: Vec<MessageInput>,
}

#[derive(Debug, Serialize, FromRow)]
struct Conversation {
    id: String,
    title: String,
    archived: i8,
    created_at: String,
    updated_at: String,
}

#[derive(Debug, Serialize, FromRow)]
struct Message {
    role: String,
    content: String,
    created_at: String,
}

#[derive(Debug, Serialize)]
struct ConversationList {
    conversations: Vec<Conversation>,
}

#[derive(Debug, Serialize)]
struct MessagesResponse {
    messages: Vec<Message>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/conversations", post(create))
        .route("/api/galileo/conversations/sync", post(sync))
        .route(
            "/api/galileo/conversations/:project_id",
            get(list).delete(delete_conversation),
        )
        .route(
            "/api/galileo/conversations/:id/messages",
            get(messages).post(add_messages),
        )
        .route("/api/galileo/conversations/:id/rename", post(rename))
        .route("/api/galileo/conversations/:id/archive", post(archive))
}

async fn list(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
    Query(query): Query<ArchivedQuery>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !is_safe_project_id(&project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }

    let include_archived = query.archived == Some(1);
    let archived_filter = if include_archived {
        "AND archived = 1"
    } else {
        "AND archived = 0"
    };
    let sql = format!(
        "SELECT id, title, archived,
                DATE_FORMAT(created_at, '%Y-%m-%dT%H:%i:%sZ') AS created_at,
                DATE_FORMAT(updated_at, '%Y-%m-%dT%H:%i:%sZ') AS updated_at
         FROM conversations
         WHERE user_id = ? AND project_id = ? {}
         ORDER BY updated_at DESC LIMIT 50",
        archived_filter
    );
    match sqlx::query_as::<_, Conversation>(&sql)
        .bind(
            authenticated_user_id(pool, &state, &headers)
                .await
                .unwrap_or_default(),
        )
        .bind(project_id)
        .fetch_all(pool)
        .await
    {
        Ok(conversations) => Json(ConversationList { conversations }).into_response(),
        Err(error) => database_error(error),
    }
}

async fn create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<CreateConversationRequest>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let project_id = input.project_id.trim();
    if !project_id.is_empty() && !is_safe_project_id(project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }

    let title = input.title.as_deref().unwrap_or("Chat").trim();
    let title = if title.is_empty() { "Chat" } else { title };
    let title: String = title.chars().take(200).collect();
    let id = Uuid::new_v4().to_string();
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();

    if let Err(error) = sqlx::query(
        "INSERT INTO conversations (id, user_id, project_id, title, archived, created_at, updated_at)
         VALUES (?, ?, ?, ?, 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
    )
    .bind(&id)
    .bind(user_id)
    .bind(project_id)
    .bind(&title)
    .execute(pool)
    .await
    {
        return database_error(error);
    }

    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "id": id, "title": title })),
    )
        .into_response()
}

async fn messages(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !is_safe_id(&id) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    if !conversation_owned(pool, &id, &user_id)
        .await
        .unwrap_or(false)
    {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }

    match sqlx::query_as::<_, Message>(
        "SELECT role, content,
                DATE_FORMAT(created_at, '%Y-%m-%dT%H:%i:%sZ') AS created_at
         FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC",
    )
    .bind(id)
    .fetch_all(pool)
    .await
    {
        Ok(messages) => Json(MessagesResponse { messages }).into_response(),
        Err(error) => database_error(error),
    }
}

async fn add_messages(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(input): Json<AddMessagesRequest>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !is_safe_id(&id) || input.messages.is_empty() || input.messages.len() > 100 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_messages");
    }
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    if !conversation_owned(pool, &id, &user_id)
        .await
        .unwrap_or(false)
    {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }

    let mut saved = 0usize;
    let mut first_user_message = None;
    for message in input.messages {
        let role = message.role.trim();
        if !matches!(role, "user" | "assistant" | "system") {
            continue;
        }
        let content: String = message.content.chars().take(500_000).collect();
        if content.is_empty() {
            continue;
        }
        if first_user_message.is_none() && role == "user" {
            first_user_message = Some(content.clone());
        }
        let result = if let Some(ts) = message.ts.filter(|value| *value > 0) {
            sqlx::query(
                "INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                 VALUES (?, ?, ?, FROM_UNIXTIME(? / 1000))",
            )
            .bind(&id)
            .bind(role)
            .bind(content)
            .bind(ts)
            .execute(pool)
            .await
        } else {
            sqlx::query(
                "INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                 VALUES (?, ?, ?, UTC_TIMESTAMP())",
            )
            .bind(&id)
            .bind(role)
            .bind(content)
            .execute(pool)
            .await
        };
        if let Err(error) = result {
            return database_error(error);
        }
        saved += 1;
    }

    if let Some(message) = first_user_message {
        let generic = sqlx::query_scalar::<_, String>(
            "SELECT title FROM conversations WHERE id = ? AND user_id = ? LIMIT 1",
        )
        .bind(&id)
        .bind(&user_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .is_some_and(|title| title == "Chat");
        if generic {
            let title: String = message.trim().chars().take(50).collect();
            let _ = sqlx::query("UPDATE conversations SET title = ?, updated_at = UTC_TIMESTAMP() WHERE id = ? AND user_id = ?")
                .bind(title)
                .bind(&id)
                .bind(&user_id)
                .execute(pool)
                .await;
        } else {
            let _ = touch_conversation(pool, &id, &user_id).await;
        }
    } else {
        let _ = touch_conversation(pool, &id, &user_id).await;
    }

    Json(serde_json::json!({ "ok": true, "saved": saved })).into_response()
}

async fn rename(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(input): Json<RenameRequest>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let title = input.title.trim();
    if !is_safe_id(&id) || title.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "title_required");
    }
    let title: String = title.chars().take(200).collect();
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    let result = sqlx::query("UPDATE conversations SET title = ?, updated_at = UTC_TIMESTAMP() WHERE id = ? AND user_id = ?")
        .bind(&title)
        .bind(&id)
        .bind(&user_id)
        .execute(pool)
        .await;
    match result {
        Ok(result) if result.rows_affected() == 0 => {
            error_response(StatusCode::NOT_FOUND, "not_found")
        }
        Ok(_) => Json(serde_json::json!({ "ok": true, "title": title })).into_response(),
        Err(error) => database_error(error),
    }
}

async fn archive(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(input): Json<ArchiveRequest>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !is_safe_id(&id) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    let archived = input.archived.unwrap_or(true);
    match sqlx::query("UPDATE conversations SET archived = ?, updated_at = UTC_TIMESTAMP() WHERE id = ? AND user_id = ?")
        .bind(if archived { 1 } else { 0 })
        .bind(&id)
        .bind(&user_id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 0 => error_response(StatusCode::NOT_FOUND, "not_found"),
        Ok(_) => Json(serde_json::json!({ "ok": true, "archived": archived })).into_response(),
        Err(error) => database_error(error),
    }
}

async fn delete_conversation(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !is_safe_id(&id) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    match sqlx::query("DELETE FROM conversations WHERE id = ? AND user_id = ?")
        .bind(&id)
        .bind(&user_id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 0 => {
            error_response(StatusCode::NOT_FOUND, "not_found")
        }
        Ok(_) => Json(serde_json::json!({ "ok": true })).into_response(),
        Err(error) => database_error(error),
    }
}

async fn sync(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<SyncRequest>,
) -> Response {
    let Some(pool) = authenticated_pool(&state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !input.project_id.is_empty() && !is_safe_project_id(&input.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    }
    let user_id = authenticated_user_id(pool, &state, &headers)
        .await
        .unwrap_or_default();
    let mut synced = 0usize;
    for local in input.conversations.into_iter().take(50) {
        if local.messages.is_empty() {
            continue;
        }
        let title = local.title.as_deref().unwrap_or("Chat").trim();
        let title: String = if title.is_empty() { "Chat" } else { title }
            .chars()
            .take(200)
            .collect();
        let existing = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM conversations WHERE user_id = ? AND project_id = ? AND title = ?",
        )
        .bind(&user_id)
        .bind(&input.project_id)
        .bind(&title)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if existing > 0 {
            continue;
        }
        let id = Uuid::new_v4().to_string();
        if let Err(error) = sqlx::query(
            "INSERT INTO conversations (id, user_id, project_id, title, archived, created_at, updated_at)
             VALUES (?, ?, ?, ?, 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
        )
        .bind(&id)
        .bind(&user_id)
        .bind(&input.project_id)
        .bind(&title)
        .execute(pool)
        .await
        {
            return database_error(error);
        }
        let request = AddMessagesRequest {
            messages: local.messages,
        };
        for message in request.messages.into_iter().take(100) {
            let content: String = message.content.chars().take(500_000).collect();
            if content.is_empty()
                || !matches!(message.role.as_str(), "user" | "assistant" | "system")
            {
                continue;
            }
            if let Err(error) = sqlx::query(
                "INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                 VALUES (?, ?, ?, UTC_TIMESTAMP())",
            )
            .bind(&id)
            .bind(message.role)
            .bind(content)
            .execute(pool)
            .await
            {
                return database_error(error);
            }
        }
        synced += 1;
    }
    Json(serde_json::json!({ "ok": true, "synced": synced })).into_response()
}

async fn authenticated_pool<'a>(state: &'a AppState, headers: &HeaderMap) -> Option<&'a MySqlPool> {
    let pool = state.db.as_ref()?;
    match auth::authenticated_user(pool, state, headers).await {
        Ok(Some(_)) => Some(pool),
        _ => None,
    }
}

async fn authenticated_user_id(
    pool: &MySqlPool,
    state: &AppState,
    headers: &HeaderMap,
) -> Option<String> {
    auth::authenticated_user(pool, state, headers)
        .await
        .ok()
        .flatten()
        .map(|user| user.id)
}

async fn conversation_owned(
    pool: &MySqlPool,
    id: &str,
    user_id: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM conversations WHERE id = ? AND user_id = ?")
        .bind(id)
        .bind(user_id)
        .fetch_one(pool)
        .await
        .map(|count| count > 0)
}

async fn touch_conversation(pool: &MySqlPool, id: &str, user_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE conversations SET updated_at = UTC_TIMESTAMP() WHERE id = ? AND user_id = ?",
    )
    .bind(id)
    .bind(user_id)
    .execute(pool)
    .await
    .map(|_| ())
}

fn is_safe_id(value: &str) -> bool {
    value.len() <= 100
        && !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() || byte == b'-' || byte == b'_')
}

fn is_safe_project_id(value: &str) -> bool {
    value.len() <= 120
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn database_error(error: sqlx::Error) -> Response {
    tracing::error!(?error, "Galileo conversation database error");
    error_response(
        StatusCode::SERVICE_UNAVAILABLE,
        "conversation_storage_unavailable",
    )
}
