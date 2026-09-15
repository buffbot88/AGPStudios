use std::{
    env, fs,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, MySqlPool};
use tokio::{
    process::Command,
    time::{timeout, Duration},
};
use uuid::Uuid;

use crate::{
    auth,
    response::{error_response, rate_limit_response},
    AppState,
};

const COMMUNITY_CATEGORIES: [&str; 5] = ["tools", "ai", "pipeline", "games", "general"];
const SUPPORT_CATEGORIES: [&str; 5] = ["bug", "feature", "account", "billing", "other"];
const SUPPORT_PRIORITIES: [&str; 4] = ["low", "normal", "high", "urgent"];

#[derive(Debug, Deserialize)]
struct CommunityQuery {
    q: Option<String>,
    category: Option<String>,
    page: Option<u32>,
}
#[derive(Debug, Deserialize)]
struct CommunitySubmit {
    project_id: String,
    title: String,
    description: String,
    category: String,
    tags: Option<String>,
    stack: Option<String>,
}
#[derive(Debug, Deserialize)]
struct CommunityUpdate {
    title: String,
    description: String,
    category: String,
    tags: Option<String>,
    stack: Option<String>,
}
#[derive(Debug, Serialize, FromRow, Clone)]
struct CommunityProject {
    id: String,
    slug: String,
    title: String,
    description: String,
    category: String,
    tags: String,
    stack: String,
    status: String,
    created_at: String,
    user_id: Option<String>,
    publisher_username: Option<String>,
    publisher_display_name: Option<String>,
    deployed_url: Option<String>,
}
#[derive(Debug, Serialize, FromRow)]
struct DocArticle {
    slug: String,
    category: String,
    title: String,
    summary: String,
    content: String,
    sort_order: i32,
}
#[derive(Debug, Deserialize)]
struct SupportCreate {
    subject: String,
    category: String,
    priority: String,
    message: String,
}
#[derive(Debug, Deserialize)]
struct SupportReply {
    message: String,
}
#[derive(Debug, Serialize, FromRow)]
struct TicketSummary {
    id: String,
    subject: String,
    status: String,
    priority: String,
    category: String,
    preview: String,
    created_at: String,
    updated_at: String,
}
#[derive(Debug, Serialize, FromRow)]
struct Ticket {
    id: String,
    user_id: String,
    subject: String,
    status: String,
    priority: String,
    category: String,
    message: String,
    created_at: String,
    updated_at: String,
}
#[derive(Debug, Serialize, FromRow)]
struct TicketReply {
    id: String,
    ticket_id: String,
    user_id: String,
    message: String,
    is_staff: i8,
    created_at: String,
    username: Option<String>,
    display_name: Option<String>,
    role: Option<String>,
}
#[derive(Debug, Deserialize)]
struct TelemetryRestart {
    server: String,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/api/community/projects",
            get(community_list).post(community_submit),
        )
        .route(
            "/api/community/projects/:slug",
            get(community_show)
                .put(community_update)
                .delete(community_delete),
        )
        .route(
            "/api/community/projects/:slug/resubmit",
            post(community_resubmit),
        )
        .route("/api/community/users/:username", get(community_publisher))
        .route("/api/docs", get(docs_list))
        .route("/api/docs/:slug", get(docs_show))
        .route("/api/support", get(support_list).post(support_create))
        .route("/api/support/:id", get(support_show))
        .route("/api/support/:id/reply", post(support_reply))
        .route("/api/account/summary", get(account_summary))
        .route("/api/account/github", post(account_github_unlink))
        .route("/api/account", delete(account_delete))
        .route("/api/admin/telemetry", get(admin_telemetry))
        .route("/api/admin/telemetry/refresh", post(admin_telemetry_refresh))
        .route("/api/admin/telemetry/clear", post(admin_telemetry_clear))
        .route("/api/admin/telemetry/restart", post(admin_restart))
}

async fn community_list(
    State(state): State<AppState>,
    Query(query): Query<CommunityQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let page = query.page.unwrap_or(1).clamp(1, 1000);
    let offset = (page - 1) * 24;
    let q = query.q.as_deref().unwrap_or("").trim();
    let category = query.category.as_deref().unwrap_or("").trim();
    if !category.is_empty() && !COMMUNITY_CATEGORIES.contains(&category) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_category");
    }
    let sql = "SELECT cp.id,cp.slug,cp.title,cp.description,cp.category,cp.tags,cp.stack,cp.status,DATE_FORMAT(cp.created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,cp.user_id,u.username AS publisher_username,u.display_name AS publisher_display_name,d.url AS deployed_url FROM community_projects cp LEFT JOIN users u ON u.id=cp.user_id LEFT JOIN galileo_community_links cl ON cl.community_id=cp.id LEFT JOIN galileo_deployments d ON d.user_id=cl.user_id AND d.project_id=cl.project_id AND d.status='deployed' WHERE (u.id IS NULL OR u.is_active=1) AND cp.status NOT IN ('pending','rejected') AND (cl.community_id IS NULL OR d.user_id IS NOT NULL) AND (?='' OR cp.category=?) AND (?='' OR cp.title LIKE CONCAT('%',?,'%') OR cp.description LIKE CONCAT('%',?,'%') OR cp.tags LIKE CONCAT('%',?,'%') OR u.username LIKE CONCAT('%',?,'%')) ORDER BY cp.created_at DESC LIMIT 24 OFFSET ?";
    match sqlx::query_as::<_, CommunityProject>(sql)
        .bind(category)
        .bind(category)
        .bind(q)
        .bind(q)
        .bind(q)
        .bind(q)
        .bind(q)
        .bind(offset)
        .fetch_all(pool)
        .await
    {
        Ok(projects) => {
            Json(serde_json::json!({ "projects": projects, "page": page, "page_size": 24 }))
                .into_response()
        }
        Err(error) => {
            tracing::error!(?error, "community listing failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable")
        }
    }
}

async fn community_show(
    State(state): State<AppState>,
    AxumPath(slug): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_slug(&slug) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    match community_by_slug(pool, &slug).await {
        Ok(Some(project)) => Json(serde_json::json!({ "project": project })).into_response(),
        Ok(None) => error_response(StatusCode::NOT_FOUND, "not_found"),
        Err(error) => {
            tracing::error!(?error, "community project lookup failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable")
        }
    }
}

async fn community_publisher(
    State(state): State<AppState>,
    AxumPath(username): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let user = sqlx::query_as::<_, (String, String, String)>(
        "SELECT id,username,display_name FROM users WHERE username=? AND is_active=1",
    )
    .bind(&username)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    let Some((id, username, display_name)) = user else {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    };
    match sqlx::query_as::<_, CommunityProject>("SELECT cp.id,cp.slug,cp.title,cp.description,cp.category,cp.tags,cp.stack,cp.status,DATE_FORMAT(cp.created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,cp.user_id,u.username AS publisher_username,u.display_name AS publisher_display_name,d.url AS deployed_url FROM community_projects cp LEFT JOIN users u ON u.id=cp.user_id LEFT JOIN galileo_community_links cl ON cl.community_id=cp.id LEFT JOIN galileo_deployments d ON d.user_id=cl.user_id AND d.project_id=cl.project_id AND d.status='deployed' WHERE cp.user_id=? AND cp.status NOT IN ('pending','rejected') ORDER BY cp.created_at DESC").bind(&id).fetch_all(pool).await {
        Ok(projects) => Json(serde_json::json!({ "publisher": {"username": username, "display_name": display_name}, "projects": projects })).into_response(),
        Err(error) => { tracing::error!(?error, "publisher lookup failed"); error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable") }
    }
}

async fn community_submit(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<CommunitySubmit>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if let Some(retry) = state.operation_rate_limiter.check(
        &format!("community:{}", user.id),
        10,
        Duration::from_secs(3600),
    ) {
        return rate_limit_response(retry);
    }
    let title = input.title.trim();
    let description = input.description.trim();
    let category = input.category.trim();
    if !safe_segment(&input.project_id)
        || title.is_empty()
        || title.len() > 200
        || description.is_empty()
        || description.len() > 5000
        || !COMMUNITY_CATEGORIES.contains(&category)
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_community_project");
    }
    let Some(deployed_url) = sqlx::query_scalar::<_, String>("SELECT url FROM galileo_deployments WHERE user_id=? AND project_id=? AND status='deployed'").bind(&user.id).bind(&input.project_id).fetch_optional(pool).await.ok().flatten() else { return error_response(StatusCode::CONFLICT, "project_must_be_deployed"); };
    let slug_base = slugify(title);
    let slug = unique_slug(pool, &slug_base).await.unwrap_or_else(|| {
        format!(
            "{}-{}",
            slug_base,
            &Uuid::new_v4().simple().to_string()[..8]
        )
    });
    let id = Uuid::new_v4().to_string();
    if let Err(error) = sqlx::query("INSERT INTO community_projects (id,user_id,title,slug,description,category,tags,stack,status) VALUES (?,?,?,?,?,?,?,?,'live')").bind(&id).bind(&user.id).bind(title).bind(&slug).bind(description).bind(category).bind(input.tags.as_deref().unwrap_or("")).bind(input.stack.as_deref().unwrap_or("")).execute(pool).await { tracing::error!(?error, "community submission failed"); return error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable"); }
    if let Err(error) = sqlx::query("INSERT INTO galileo_community_links (community_id,user_id,project_id,created_at) VALUES (?,?,?,?)").bind(&id).bind(&user.id).bind(&input.project_id).bind(now()).execute(pool).await { tracing::error!(?error, "community deployment link failed"); let _ = sqlx::query("DELETE FROM community_projects WHERE id=?").bind(&id).execute(pool).await; return error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable"); }
    record_activity(
        pool,
        &user.id,
        Some(&input.project_id),
        "community.published",
        &serde_json::json!({"slug":slug}),
        None,
    )
    .await;
    Json(serde_json::json!({ "ok": true, "slug": slug, "url": deployed_url, "status": "live" }))
        .into_response()
}

async fn community_update(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(slug): AxumPath<String>,
    Json(input): Json<CommunityUpdate>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let title = input.title.trim();
    let description = input.description.trim();
    let category = input.category.trim();
    if title.is_empty()
        || title.len() > 200
        || description.is_empty()
        || description.len() > 5000
        || !COMMUNITY_CATEGORIES.contains(&category)
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_community_project");
    }
    let result = sqlx::query("UPDATE community_projects SET title=?,description=?,category=?,tags=?,stack=? WHERE slug=? AND user_id=?").bind(title).bind(description).bind(category).bind(input.tags.as_deref().unwrap_or("")).bind(input.stack.as_deref().unwrap_or("")).bind(&slug).bind(&user.id).execute(pool).await;
    match result {
        Ok(result) if result.rows_affected() == 0 => {
            error_response(StatusCode::NOT_FOUND, "not_found")
        }
        Ok(_) => Json(serde_json::json!({"ok":true,"slug":slug})).into_response(),
        Err(error) => {
            tracing::error!(?error, "community update failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable")
        }
    }
}

async fn community_delete(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(slug): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let result =
        sqlx::query("DELETE cp FROM community_projects cp WHERE cp.slug=? AND cp.user_id=?")
            .bind(&slug)
            .bind(&user.id)
            .execute(pool)
            .await;
    match result {
        Ok(result) if result.rows_affected() == 0 => {
            error_response(StatusCode::NOT_FOUND, "not_found")
        }
        Ok(_) => Json(serde_json::json!({"ok":true})).into_response(),
        Err(_error) => error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable"),
    }
}

async fn community_resubmit(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(slug): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let result = sqlx::query("UPDATE community_projects SET status='live' WHERE slug=? AND user_id=? AND status='rejected'").bind(&slug).bind(&user.id).execute(pool).await;
    match result {
        Ok(result) if result.rows_affected() == 0 => {
            error_response(StatusCode::NOT_FOUND, "not_found")
        }
        Ok(_) => Json(serde_json::json!({"ok":true,"status":"live"})).into_response(),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "community_unavailable"),
    }
}

async fn docs_list(State(state): State<AppState>, Query(query): Query<DocQuery>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let q = query.q.unwrap_or_default();
    let category = query.category.unwrap_or_default();
    match sqlx::query_as::<_, DocArticle>("SELECT slug,category,title,summary,content,sort_order FROM docs_articles WHERE (?='' OR category=?) AND (?='' OR title LIKE CONCAT('%',?,'%') OR summary LIKE CONCAT('%',?,'%')) ORDER BY sort_order,title LIMIT 200").bind(&category).bind(&category).bind(&q).bind(&q).bind(&q).fetch_all(pool).await {
        Ok(articles) => Json(serde_json::json!({"articles":articles})).into_response(),
        Err(error) => { tracing::error!(?error, "docs listing failed"); error_response(StatusCode::SERVICE_UNAVAILABLE, "docs_unavailable") }
    }
}
#[derive(Debug, Deserialize)]
struct DocQuery {
    q: Option<String>,
    category: Option<String>,
}
async fn docs_show(State(state): State<AppState>, AxumPath(slug): AxumPath<String>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_slug(&slug) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    match sqlx::query_as::<_, DocArticle>(
        "SELECT slug,category,title,summary,content,sort_order FROM docs_articles WHERE slug=?",
    )
    .bind(slug)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(article)) => Json(serde_json::json!({"article":article})).into_response(),
        Ok(None) => error_response(StatusCode::NOT_FOUND, "not_found"),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "docs_unavailable"),
    }
}

async fn support_list(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let is_admin = user.role.eq_ignore_ascii_case("admin");
    let result = if is_admin {
        sqlx::query_as::<_, TicketSummary>("SELECT id,subject,status,priority,category,SUBSTRING(message,1,120) AS preview,DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,DATE_FORMAT(updated_at,'%Y-%m-%dT%H:%i:%sZ') AS updated_at FROM support_tickets ORDER BY updated_at DESC LIMIT 100").fetch_all(pool).await
    } else {
        sqlx::query_as::<_, TicketSummary>("SELECT id,subject,status,priority,category,SUBSTRING(message,1,120) AS preview,DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,DATE_FORMAT(updated_at,'%Y-%m-%dT%H:%i:%sZ') AS updated_at FROM support_tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 100").bind(&user.id).fetch_all(pool).await
    };
    match result {
        Ok(tickets) => Json(serde_json::json!({"tickets":tickets})).into_response(),
        Err(error) => {
            tracing::error!(?error, "support listing failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "support_unavailable")
        }
    }
}

async fn support_create(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<SupportCreate>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if let Some(retry) = state.operation_rate_limiter.check(
        &format!("support:{}", user.id),
        5,
        Duration::from_secs(3600),
    ) {
        return rate_limit_response(retry);
    }
    if input.subject.trim().is_empty()
        || input.subject.len() > 200
        || input.message.trim().is_empty()
        || input.message.len() > 50_000
        || !SUPPORT_CATEGORIES.contains(&input.category.as_str())
        || !SUPPORT_PRIORITIES.contains(&input.priority.as_str())
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_ticket");
    }
    let id = Uuid::new_v4().to_string();
    match sqlx::query("INSERT INTO support_tickets (id,user_id,subject,status,priority,category,message) VALUES (?,?,?,'open',?,?,?)").bind(&id).bind(&user.id).bind(input.subject.trim()).bind(&input.priority).bind(&input.category).bind(input.message.trim()).execute(pool).await { Ok(_) => { record_activity(pool, &user.id, None, "support.created", &serde_json::json!({"ticket_id":id}), None).await; (StatusCode::CREATED, Json(serde_json::json!({"id":id,"status":"open"}))).into_response() }, Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "support_unavailable") }
}

async fn support_show(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let is_admin = user.role.eq_ignore_ascii_case("admin");
    let ticket = if is_admin {
        sqlx::query_as::<_, Ticket>("SELECT id,user_id,subject,status,priority,category,message,DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,DATE_FORMAT(updated_at,'%Y-%m-%dT%H:%i:%sZ') AS updated_at FROM support_tickets WHERE id=?").bind(&id).fetch_optional(pool).await
    } else {
        sqlx::query_as::<_, Ticket>("SELECT id,user_id,subject,status,priority,category,message,DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,DATE_FORMAT(updated_at,'%Y-%m-%dT%H:%i:%sZ') AS updated_at FROM support_tickets WHERE id=? AND user_id=?").bind(&id).bind(&user.id).fetch_optional(pool).await
    };
    let Ok(Some(ticket)) = ticket else {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    };
    match sqlx::query_as::<_, TicketReply>("SELECT r.id,r.ticket_id,r.user_id,r.message,r.is_staff,DATE_FORMAT(r.created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,u.username,u.display_name,u.role FROM support_ticket_replies r JOIN users u ON u.id=r.user_id WHERE r.ticket_id=? ORDER BY r.created_at ASC").bind(&id).fetch_all(pool).await { Ok(replies) => Json(serde_json::json!({"ticket":ticket,"replies":replies})).into_response(), Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "support_unavailable") }
}

async fn support_reply(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    AxumPath(id): AxumPath<String>,
    Json(input): Json<SupportReply>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if input.message.trim().is_empty() || input.message.len() > 50_000 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_reply");
    }
    let is_admin = user.role.eq_ignore_ascii_case("admin");
    let owns = if is_admin {
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM support_tickets WHERE id=?")
            .bind(&id)
            .fetch_one(pool)
            .await
            .unwrap_or(0)
    } else {
        sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM support_tickets WHERE id=? AND user_id=?",
        )
        .bind(&id)
        .bind(&user.id)
        .fetch_one(pool)
        .await
        .unwrap_or(0)
    };
    if owns != 1 {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let reply_id = Uuid::new_v4().to_string();
    match sqlx::query("INSERT INTO support_ticket_replies (id,ticket_id,user_id,message,is_staff) VALUES (?,?,?,?,?)").bind(&reply_id).bind(&id).bind(&user.id).bind(input.message.trim()).bind(if is_admin {1} else {0}).execute(pool).await { Ok(_) => { let _ = sqlx::query("UPDATE support_tickets SET updated_at=UTC_TIMESTAMP() WHERE id=?").bind(&id).execute(pool).await; record_activity(pool, &user.id, None, "support.replied", &serde_json::json!({"ticket_id":id}), None).await; Json(serde_json::json!({"ok":true,"id":reply_id})).into_response() }, Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "support_unavailable") }
}

async fn account_summary(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let projects_root = state.projects_root.join(&user.id);
    let projects = fs::read_dir(projects_root)
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .filter(|entry| entry.path().is_dir())
                .count()
        })
        .unwrap_or(0);
    let deployments = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM galileo_deployments WHERE user_id=? AND status='deployed'",
    )
    .bind(&user.id)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    let files = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM conversation_messages cm JOIN conversations c ON c.id=cm.conversation_id WHERE c.user_id=?").bind(&user.id).fetch_one(pool).await.unwrap_or(0);
    let github_linked = sqlx::query_scalar::<_, Option<String>>("SELECT github_login FROM users WHERE id=?").bind(&user.id).fetch_one(pool).await.unwrap_or(None).is_some();
    Json(serde_json::json!({"user":user,"github_linked":github_linked,"stats":{"projects":projects,"deployments":deployments,"conversation_messages":files}})).into_response()
}

async fn account_github_unlink(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    match sqlx::query("UPDATE users SET github_id=NULL,github_login=NULL,github_access_token=NULL,github_refresh_token=NULL,github_token_expires_at=NULL WHERE id=?").bind(&user.id).execute(pool).await {
        Ok(_) => Json(serde_json::json!({"ok":true,"github_linked":false})).into_response(),
        Err(_) => error_response(StatusCode::SERVICE_UNAVAILABLE, "github_unlink_failed"),
    }
}

async fn account_delete(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    let tables = ["conversation_messages", "conversations", "galileo_job_events", "galileo_job_changes", "galileo_jobs", "galileo_plans", "galileo_deployment_history", "galileo_deployments", "galileo_activity", "galileo_community_links", "support_ticket_replies", "support_tickets", "password_resets", "email_verifications", "rust_sessions", "google_accounts", "icarus_devices", "admin_audit_events"];
    for table in tables { let _ = sqlx::query(&format!("DELETE FROM {table} WHERE user_id=?")).bind(&user.id).execute(pool).await; }
    let _ = sqlx::query("DELETE FROM users WHERE id=?").bind(&user.id).execute(pool).await;
    let _ = tokio::fs::remove_dir_all(state.projects_root.join(&user.id)).await;
    Json(serde_json::json!({"ok":true})).into_response()
}

async fn admin_telemetry(
    State(state): State<AppState>,
    auth::AdminUser(user): auth::AdminUser,
) -> Response {
    let telemetry = crate::collect_telemetry(&state).await;
    Json(serde_json::json!({
        "ok": true,
        "admin": user.username,
        "servers": telemetry.servers,
        "updated_at": telemetry.updated_at,
    }))
    .into_response()
}

async fn admin_telemetry_refresh(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    Json(serde_json::json!({"ok": true, "telemetry": crate::collect_telemetry(&state).await})).into_response()
}

async fn admin_telemetry_clear(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    // Telemetry is fetched with no-store and has no server-side cache.
    Json(serde_json::json!({"ok": true, "telemetry": crate::collect_telemetry(&state).await})).into_response()
}

async fn admin_restart(
    State(state): State<AppState>,
    auth::AdminUser(user): auth::AdminUser,
    headers: axum::http::HeaderMap,
    Json(input): Json<TelemetryRestart>,
) -> Response {
    if input.server != "omega" {
        return error_response(StatusCode::BAD_REQUEST, "invalid_server");
    }
    if let Some(retry) = state.operation_rate_limiter.check(
        &format!("telemetry-restart:{}:{}", user.id, input.server),
        1,
        Duration::from_secs(30),
    ) {
        return rate_limit_response(retry);
    }
    let helper = env::var("ASHAT_AGENT_RESTART_HELPER")
        .unwrap_or_else(|_| "/usr/local/sbin/ashat-agent-restart".to_owned());
    if !Path::new(&helper).is_file() {
        return error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "restart_control_unavailable",
        );
    }
    let result = timeout(
        Duration::from_secs(25),
        Command::new(helper).arg(&input.server).output(),
    )
    .await;
    match result {
        Ok(Ok(output)) if output.status.success() => {
            let request_id = headers
                .get("x-request-id")
                .and_then(|value| value.to_str().ok());
            if let Some(pool) = state.db.as_ref() {
                record_activity(
                    pool,
                    &user.id,
                    None,
                    "telemetry.restart",
                    &serde_json::json!({"server": input.server}),
                    request_id,
                )
                .await;
            }
            tracing::info!(event="telemetry.restart.requested", user_id=%user.id, server=%input.server, request_id=?request_id);
            Json(serde_json::json!({"ok":true,"server":input.server})).into_response()
        }
        _ => error_response(StatusCode::BAD_GATEWAY, "restart_failed"),
    }
}

async fn community_by_slug(
    pool: &MySqlPool,
    slug: &str,
) -> Result<Option<CommunityProject>, sqlx::Error> {
    sqlx::query_as::<_, CommunityProject>("SELECT cp.id,cp.slug,cp.title,cp.description,cp.category,cp.tags,cp.stack,cp.status,DATE_FORMAT(cp.created_at,'%Y-%m-%dT%H:%i:%sZ') AS created_at,cp.user_id,u.username AS publisher_username,u.display_name AS publisher_display_name,d.url AS deployed_url FROM community_projects cp LEFT JOIN users u ON u.id=cp.user_id LEFT JOIN galileo_community_links cl ON cl.community_id=cp.id LEFT JOIN galileo_deployments d ON d.user_id=cl.user_id AND d.project_id=cl.project_id AND d.status='deployed' WHERE cp.slug=? AND (u.id IS NULL OR u.is_active=1) AND cp.status NOT IN ('pending','rejected') AND (cl.community_id IS NULL OR d.user_id IS NOT NULL)").bind(slug).fetch_optional(pool).await
}
async fn unique_slug(pool: &MySqlPool, base: &str) -> Option<String> {
    if sqlx::query_scalar::<_, String>("SELECT slug FROM community_projects WHERE slug=?")
        .bind(base)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .is_none()
    {
        Some(base.to_owned())
    } else {
        Some(format!(
            "{}-{}",
            base,
            &Uuid::new_v4().simple().to_string()[..8]
        ))
    }
}
async fn record_activity(
    pool: &MySqlPool,
    user_id: &str,
    project_id: Option<&str>,
    action: &str,
    metadata: &serde_json::Value,
    request_id: Option<&str>,
) {
    let _ = sqlx::query("INSERT INTO galileo_activity (id,user_id,project_id,action,metadata,request_id,created_at) VALUES (?,?,?,?,?,?,?)").bind(Uuid::new_v4().to_string()).bind(user_id).bind(project_id).bind(action).bind(metadata.to_string()).bind(request_id).bind(now()).execute(pool).await;
}
fn slugify(value: &str) -> String {
    let mut result = String::new();
    for character in value.chars() {
        if character.is_ascii_alphanumeric() {
            result.push(character.to_ascii_lowercase());
        } else if !result.ends_with('-') && !result.is_empty() {
            result.push('-');
        }
    }
    result.trim_matches('-').to_owned()
}
fn safe_slug(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
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

#[cfg(test)]
mod tests {
    use super::{safe_segment, safe_slug, slugify, COMMUNITY_CATEGORIES};

    #[test]
    fn community_slugs_are_normalized_and_bounded() {
        assert_eq!(slugify("  My First App! "), "my-first-app");
        assert!(safe_slug("my-first-app"));
        assert!(!safe_slug("../private"));
        assert!(!safe_slug(""));
    }

    #[test]
    fn member_project_segments_reject_paths() {
        assert!(safe_segment("project_123"));
        assert!(!safe_segment("user/project"));
        assert!(!safe_segment(".."));
        assert!(COMMUNITY_CATEGORIES.contains(&"pipeline"));
    }
}
