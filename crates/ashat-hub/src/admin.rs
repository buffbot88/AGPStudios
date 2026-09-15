use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;
use tokio::{fs, process::Command};
use uuid::Uuid;

use crate::{auth, response::error_response, AppState};

#[derive(Debug, Deserialize)]
struct UserQuery {
    q: Option<String>,
    page: Option<u32>,
}

#[derive(Debug, Serialize, FromRow)]
struct AdminUserRow {
    id: String,
    username: String,
    email: String,
    display_name: String,
    role: String,
    is_active: i8,
    banned_at: Option<i64>,
    email_verified_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct UserRoleRequest {
    user_id: String,
    role: String,
}

#[derive(Debug, Deserialize)]
struct UserStatusRequest {
    user_id: String,
    is_active: bool,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/admin/summary", get(summary))
        .route("/api/admin/users", get(users))
        .route("/api/admin/users/role", post(update_role))
        .route("/api/admin/users/status", post(update_status))
        .route("/api/admin/users/ban", post(ban_user))
        .route("/api/admin/users/:user_id", delete(delete_user))
        .route("/api/admin/deployments", get(all_deployments))
        .route("/api/admin/deployments/:deployment_id", get(deployment_detail))
        .route("/api/admin/deployment/undeploy", post(admin_undeploy))
        .route("/api/admin/audit", get(audit_log))
        .route("/api/admin/database/status", get(database_status))
        .route("/api/admin/settings", get(settings))
        .route("/api/admin/github/push", post(github_push))
        .route("/api/admin/github/galileo-push", post(galileo_github_push))
        .route("/api/admin/system/update", post(system_update))
        .route("/api/admin/galileo/update", post(galileo_update))
        .route("/api/admin/repo-status", get(repo_status))
        .route("/api/admin/support", get(support))
}

async fn repo_pair(directory: &'static str, remote: &'static str) -> serde_json::Value {
    let local = Command::new("git").args(["-C", directory, "rev-parse", "HEAD"]).output().await;
    let repo = Command::new("git").args(["ls-remote", remote, "refs/heads/main"]).output().await;
    let live = String::from_utf8_lossy(&local.map(|o| o.stdout).unwrap_or_default()).trim().to_owned();
    let remote_sha = String::from_utf8_lossy(&repo.map(|o| o.stdout).unwrap_or_default()).split_whitespace().next().unwrap_or("").to_owned();
    let state = if live.is_empty() || remote_sha.is_empty() { "unknown" } else if live == remote_sha { "current" } else {
        let incoming = Command::new("git").args(["-C", directory, "merge-base", "--is-ancestor", "HEAD", &remote_sha]).status().await.map(|s| s.success()).unwrap_or(false);
        if incoming { "incoming" } else { "older" }
    };
    serde_json::json!({"live":live,"repo":remote_sha,"state":state})
}

async fn repo_status(
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    let (ashathub, galileo) = tokio::join!(repo_pair("/var/oled/data/AshatHub", "https://github.com/buffbot88/AshatHub.git"), repo_pair("/var/oled/data/Galileo", "https://github.com/buffbot88/Galileo.git"));
    Json(serde_json::json!({"ashathub":ashathub,"galileo":galileo})).into_response()
}

async fn galileo_update(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    let token = match sqlx::query_scalar::<_, Option<String>>("SELECT github_access_token FROM users WHERE id=?").bind(&admin.id).fetch_one(pool).await { Ok(Some(token)) if !token.is_empty() => token, _ => return error_response(StatusCode::BAD_REQUEST, "github_account_not_linked") };
    let script = r#"set -e
ask=$(mktemp); trap 'rm -f "$ask"' EXIT
printf '%s\n' '#!/bin/sh' 'case "$1" in *Username*) printf "%s\\n" "x-access-token" ;; *) printf "%s\\n" "$GITHUB_TOKEN" ;; esac' > "$ask"
chmod 700 "$ask"
GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git fetch "https://github.com/buffbot88/Galileo.git" main
GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git reset --hard FETCH_HEAD
corepack pnpm run build
sudo -n systemctl restart galileo.service
git rev-parse HEAD
"#;
    match Command::new("bash").arg("-c").arg(script).current_dir("/var/oled/data/Galileo").env("GITHUB_TOKEN", token).output().await {
        Ok(output) if output.status.success() => Json(serde_json::json!({"ok":true,"commit":String::from_utf8_lossy(&output.stdout).lines().last().unwrap_or("unknown")})).into_response(),
        Ok(output) => { tracing::error!(status=?output.status, stderr=%String::from_utf8_lossy(&output.stderr), "Galileo update failed"); error_response(StatusCode::BAD_GATEWAY, "galileo_update_failed") },
        Err(error) => { tracing::error!(?error, "Galileo update process failed"); error_response(StatusCode::BAD_GATEWAY, "galileo_update_failed") },
    }
}

async fn system_update(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    let token = match sqlx::query_scalar::<_, Option<String>>("SELECT github_access_token FROM users WHERE id=?").bind(&admin.id).fetch_one(pool).await { Ok(Some(token)) if !token.is_empty() => token, _ => return error_response(StatusCode::BAD_REQUEST, "github_account_not_linked") };
    let script = r#"set -e
ask=$(mktemp); trap 'rm -f "$ask" "$backup"' EXIT
printf '%s\n' '#!/bin/sh' 'case "$1" in *Username*) printf "%s\\n" "x-access-token" ;; *) printf "%s\\n" "$GITHUB_TOKEN" ;; esac' > "$ask"
chmod 700 "$ask"
backup=$(mktemp)
cp crates/alpha-server/config.toml "$backup"
GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git fetch "https://github.com/$GITHUB_REPOSITORY.git" main
if git merge-base --is-ancestor HEAD FETCH_HEAD; then
  GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git merge --ff-only FETCH_HEAD
elif git merge-base --is-ancestor FETCH_HEAD HEAD; then
  : # local committed changes are already ahead; never discard them
else
  echo 'local and GitHub histories diverged; push or reconcile before updating' >&2
  exit 1
fi
cp "$backup" crates/alpha-server/config.toml
npm run build --prefix apps/ashat-hub-web
/home/opc/.cargo/bin/cargo build -p ashat-hub --release
sudo -n install -m 755 target/release/ashat-hub /usr/local/libexec/ashat-hub/ashat-hub
nohup sh -c 'sleep 2; sudo -n systemctl restart ashat-hub-rust.service' >/dev/null 2>&1 &
git rev-parse HEAD
"#;
    let result = Command::new("bash").arg("-c").arg(script).current_dir("/var/oled/data/AshatHub").env("GITHUB_TOKEN", token).env("GITHUB_REPOSITORY", "buffbot88/AshatHub").output().await;
    match result { Ok(output) if output.status.success() => { let stdout = String::from_utf8_lossy(&output.stdout); let sha = stdout.lines().last().unwrap_or("unknown"); Json(serde_json::json!({"ok":true,"commit":sha})).into_response() }, Ok(output) => { tracing::error!(status=?output.status, stderr=%String::from_utf8_lossy(&output.stderr), "system update failed"); error_response(StatusCode::BAD_GATEWAY, "system_update_failed") }, Err(error) => { tracing::error!(?error, "system update process failed"); error_response(StatusCode::BAD_GATEWAY, "system_update_failed") } }
}

async fn galileo_github_push(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    let token = match sqlx::query_scalar::<_, Option<String>>("SELECT github_access_token FROM users WHERE id=?").bind(&admin.id).fetch_one(pool).await { Ok(Some(token)) if !token.is_empty() => token, _ => return error_response(StatusCode::BAD_REQUEST, "github_account_not_linked") };
    let script = r#"set -e
ask=$(mktemp); trap 'rm -f "$ask"' EXIT
printf '%s\n' '#!/bin/sh' 'case "$1" in *Username*) printf "%s\\n" "x-access-token" ;; *) printf "%s\\n" "$GITHUB_TOKEN" ;; esac' > "$ask"
chmod 700 "$ask"
GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git fetch "https://github.com/buffbot88/Galileo.git" main
if ! git merge-base --is-ancestor FETCH_HEAD HEAD; then
  git merge --no-edit FETCH_HEAD
fi
GIT_ASKPASS="$ask" GIT_TERMINAL_PROMPT=0 git push "https://github.com/buffbot88/Galileo.git" HEAD:main
"#;
    match Command::new("bash").arg("-c").arg(script).current_dir("/var/oled/data/Galileo").env("GITHUB_TOKEN", token).output().await {
        Ok(output) if output.status.success() => Json(serde_json::json!({"ok":true,"repository":"buffbot88/Galileo","branch":"main"})).into_response(),
        Ok(output) => { tracing::error!(status=?output.status, stderr=%String::from_utf8_lossy(&output.stderr), "Galileo GitHub push failed"); error_response(StatusCode::BAD_GATEWAY, "galileo_github_push_failed") },
        Err(error) => { tracing::error!(?error, "Galileo GitHub push process failed"); error_response(StatusCode::BAD_GATEWAY, "galileo_github_push_failed") },
    }
}

async fn github_push(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable"); };
    let token = match sqlx::query_scalar::<_, Option<String>>("SELECT github_access_token FROM users WHERE id=?").bind(&admin.id).fetch_one(pool).await {
        Ok(Some(token)) if !token.is_empty() => token,
        _ => return error_response(StatusCode::BAD_REQUEST, "github_account_not_linked"),
    };
    let repo = std::env::var("GITHUB_REPOSITORY").unwrap_or_else(|_| "buffbot88/AshatHub".to_owned());
    let askpass = std::env::temp_dir().join(format!("ashat-github-askpass-{}", Uuid::new_v4()));
    if fs::write(&askpass, format!("#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' 'x-access-token' ;; *) printf '%s\\n' '{}' ;; esac\n", token.replace('\'', "'\\''"))).await.is_err() { return error_response(StatusCode::SERVICE_UNAVAILABLE, "github_push_unavailable"); }
    let _ = Command::new("chmod").arg("700").arg(&askpass).status().await;
    let result = Command::new("git").current_dir("/var/oled/data/AshatHub").arg("push").arg(format!("https://github.com/{repo}.git")).arg("HEAD:main").env("GIT_ASKPASS", &askpass).env("GIT_TERMINAL_PROMPT", "0").output().await;
    let _ = fs::remove_file(&askpass).await;
    match result { Ok(output) if output.status.success() => Json(serde_json::json!({"ok":true,"repository":repo,"branch":"main"})).into_response(), Ok(output) => { tracing::error!(status=?output.status, stderr=%String::from_utf8_lossy(&output.stderr), "GitHub push failed"); error_response(StatusCode::BAD_GATEWAY, "github_push_failed") }, Err(error) => { tracing::error!(?error, "GitHub push process failed"); error_response(StatusCode::BAD_GATEWAY, "github_push_failed") } }
}

async fn summary(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let users = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM users")
        .fetch_one(pool)
        .await
        .unwrap_or(0);
    let active_users = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM users WHERE is_active=1")
        .fetch_one(pool)
        .await
        .unwrap_or(0);
    let disabled_users = users - active_users;
    let open_tickets = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM support_tickets WHERE status IN ('open','in_progress')",
    )
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    let active_deploys = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM galileo_deployments WHERE status='deployed'",
    )
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    let active_projects = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(DISTINCT project_id) FROM galileo_deployments WHERE status='deployed'",
    )
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    Json(serde_json::json!({
        "users": users,
        "active_users": active_users,
        "disabled_users": disabled_users,
        "open_tickets": open_tickets,
        "active_deploys": active_deploys,
        "active_projects": active_projects,
        "database_manager": "retired",
    }))
    .into_response()
}

async fn users(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    Query(query): Query<UserQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let page = query.page.unwrap_or(1).clamp(1, 1000);
    let offset = (page - 1) * 50;
    let search = query.q.unwrap_or_default();
    let result = sqlx::query_as::<_, AdminUserRow>(
        "SELECT id,username,email,display_name,role,is_active,CAST(banned_at AS SIGNED) AS banned_at,CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users
         WHERE (?='' OR username LIKE CONCAT('%',?,'%') OR email LIKE CONCAT('%',?,'%') OR display_name LIKE CONCAT('%',?,'%'))
         ORDER BY username LIMIT 50 OFFSET ?",
    )
    .bind(&search)
    .bind(&search)
    .bind(&search)
    .bind(&search)
    .bind(offset)
    .fetch_all(pool)
    .await;
    match result {
        Ok(rows) => {
            Json(serde_json::json!({"users":rows,"page":page,"page_size":50})).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "admin user listing failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "users_unavailable")
        }
    }
}

async fn update_role(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
    Json(input): Json<UserRoleRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_id(&input.user_id) || !["Member", "Pro", "Admin"].contains(&input.role.as_str()) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_user_role");
    }
    if input.user_id == admin.id && input.role != "Admin" {
        return error_response(StatusCode::CONFLICT, "cannot_demote_self");
    }
    match sqlx::query("UPDATE users SET role=?,updated_at=UTC_TIMESTAMP() WHERE id=?")
        .bind(&input.role)
        .bind(&input.user_id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 1 => {
            let timestamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            let _ = sqlx::query(
                "INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at)
                 VALUES (?,?,?,?,?,CONCAT('changed role to ',?),?)",
            )
            .bind(&admin.id)
            .bind(&admin.display_name)
            .bind("admin.role_change")
            .bind("user")
            .bind(&input.user_id)
            .bind(&input.role)
            .bind(timestamp)
            .execute(pool)
            .await;
            tracing::warn!(event="admin.user.role_changed", admin_id=%admin.id, user_id=%input.user_id, role=%input.role);
            Json(serde_json::json!({"ok":true,"user_id":input.user_id,"role":input.role}))
                .into_response()
        }
        Ok(_) => error_response(StatusCode::NOT_FOUND, "user_not_found"),
        Err(error) => {
            tracing::error!(?error, "admin role update failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "users_unavailable")
        }
    }
}

async fn update_status(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
    Json(input): Json<UserStatusRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_id(&input.user_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_user");
    }
    if input.user_id == admin.id && !input.is_active {
        return error_response(StatusCode::CONFLICT, "cannot_disable_self");
    }
    match sqlx::query("UPDATE users SET is_active=?,updated_at=UTC_TIMESTAMP() WHERE id=?")
        .bind(input.is_active)
        .bind(&input.user_id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 1 => {
            if !input.is_active {
                let _ = sqlx::query("DELETE FROM rust_sessions WHERE user_id=?")
                    .bind(&input.user_id)
                    .execute(pool)
                    .await;
            }
            let timestamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            let _ = sqlx::query(
                "INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at)
                 VALUES (?,?,?,?,?,?,?)",
            )
            .bind(&admin.id)
            .bind(&admin.display_name)
            .bind("admin.status_change")
            .bind("user")
            .bind(&input.user_id)
            .bind(if input.is_active { "enabled" } else { "disabled" })
            .bind(timestamp)
            .execute(pool)
            .await;
            tracing::warn!(event="admin.user.status_changed", admin_id=%admin.id, user_id=%input.user_id, is_active=input.is_active);
            Json(serde_json::json!({"ok":true,"user_id":input.user_id,"is_active":input.is_active}))
                .into_response()
        }
        Ok(_) => error_response(StatusCode::NOT_FOUND, "user_not_found"),
        Err(error) => {
            tracing::error!(?error, "admin status update failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "users_unavailable")
        }
    }
}

// ── Ban user ──

#[derive(Debug, Deserialize)]
struct BanUserRequest {
    user_id: String,
    banned: bool,
}

async fn ban_user(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
    Json(input): Json<BanUserRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_id(&input.user_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_user");
    }
    if input.user_id == admin.id {
        return error_response(StatusCode::CONFLICT, "cannot_ban_self");
    }
    let banned_at: Option<i64> = if input.banned {
        Some(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0))
    } else {
        None
    };
    match sqlx::query("UPDATE users SET is_active=?, banned_at=?, updated_at=UTC_TIMESTAMP() WHERE id=?")
        .bind(!input.banned)
        .bind(banned_at)
        .bind(&input.user_id)
        .execute(pool)
        .await
    {
        Ok(result) if result.rows_affected() == 1 => {
            if input.banned {
                let _ = sqlx::query("DELETE FROM rust_sessions WHERE user_id=?")
                    .bind(&input.user_id)
                    .execute(pool)
                    .await;
            }
            let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
            let _ = sqlx::query(
                "INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at)
                 VALUES (?,?,?,?,?,?,?)",
            )
            .bind(&admin.id)
            .bind(&admin.display_name)
            .bind(if input.banned { "admin.user.banned" } else { "admin.user.unbanned" })
            .bind("user")
            .bind(&input.user_id)
            .bind(if input.banned { "banned" } else { "unbanned" })
            .bind(timestamp)
            .execute(pool)
            .await;
            tracing::warn!(event="admin.user.banned", admin_id=%admin.id, user_id=%input.user_id, banned=input.banned);
            Json(serde_json::json!({"ok":true,"user_id":input.user_id,"banned":input.banned})).into_response()
        }
        Ok(_) => error_response(StatusCode::NOT_FOUND, "user_not_found"),
        Err(error) => {
            tracing::error!(?error, "admin ban update failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "users_unavailable")
        }
    }
}

// ── Delete user (hard) ──

async fn delete_user(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
    Path(user_id): Path<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_id(&user_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_user");
    }
    if user_id == admin.id {
        return error_response(StatusCode::CONFLICT, "cannot_delete_self");
    }
    // Verify the user exists first
    let exists = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM users WHERE id=?")
        .bind(&user_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
    if exists == 0 {
        return error_response(StatusCode::NOT_FOUND, "user_not_found");
    }
    // Collect username before deletion for the audit record
    let username = sqlx::query_scalar::<_, String>("SELECT username FROM users WHERE id=?")
        .bind(&user_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .unwrap_or_default();
    // Cascade-delete related data
    let _ = sqlx::query("DELETE FROM rust_sessions WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM email_verifications WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM conversations WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM galileo_plans WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM galileo_jobs WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM galileo_deployment_history WHERE user_id=?").bind(&user_id).execute(pool).await;
    let _ = sqlx::query("DELETE FROM galileo_deployments WHERE user_id=?").bind(&user_id).execute(pool).await;
    // Delete the user row itself
    match sqlx::query("DELETE FROM users WHERE id=?").bind(&user_id).execute(pool).await {
        Ok(result) if result.rows_affected() >= 1 => {
            let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
            let _ = sqlx::query(
                "INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at)
                 VALUES (?,?,?,?,?,?,?)",
            )
            .bind(&admin.id)
            .bind(&admin.display_name)
            .bind("admin.user.deleted")
            .bind("user")
            .bind(&user_id)
            .bind(format!("deleted @{username}"))
            .bind(timestamp)
            .execute(pool)
            .await;
            tracing::warn!(event="admin.user.deleted", admin_id=%admin.id, user_id=%user_id, username=%username);
            Json(serde_json::json!({"ok":true,"user_id":user_id})).into_response()
        }
        Ok(_) => error_response(StatusCode::NOT_FOUND, "user_not_found"),
        Err(error) => {
            tracing::error!(?error, "admin user delete failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "users_unavailable")
        }
    }
}

async fn database_status(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let version = sqlx::query_scalar::<_, String>("SELECT VERSION()")
        .fetch_one(pool)
        .await
        .unwrap_or_else(|_| "unknown".to_owned());
    let migrations = sqlx::query_as::<_, (i64, String, bool)>(
        "SELECT version,description,success FROM _sqlx_migrations ORDER BY version",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    Json(serde_json::json!({
        "version": version,
        "migrations": migrations,
        "maintenance": "migration-driven",
        "arbitrary_sql": "retired",
    }))
    .into_response()
}

async fn settings(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    Json(serde_json::json!({
        "email_verification_enabled": state.auth.email_verification_enabled,
        "secure_cookie": state.auth.secure_cookie,
        "trusted_proxy_headers": state.auth.trust_proxy_headers,
    }))
    .into_response()
}

async fn support(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
) -> Response {
    // The member support handler already applies admin visibility rules.
    // Keep this endpoint explicit for the Rust/Vite admin navigation contract.
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    match sqlx::query("SELECT id,subject,status,priority,category,created_at,updated_at FROM support_tickets ORDER BY updated_at DESC LIMIT 100").fetch_all(pool).await {
        Ok(rows) => {
            let tickets = rows.into_iter().map(|row| {
                use sqlx::Row;
                serde_json::json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "subject": row.try_get::<String, _>("subject").unwrap_or_default(),
                    "status": row.try_get::<String, _>("status").unwrap_or_default(),
                    "priority": row.try_get::<String, _>("priority").unwrap_or_default(),
                    "category": row.try_get::<String, _>("category").unwrap_or_default(),
                    "created_at": row.try_get::<String, _>("created_at").unwrap_or_default(),
                    "updated_at": row.try_get::<String, _>("updated_at").unwrap_or_default(),
                })
            }).collect::<Vec<_>>();
            Json(serde_json::json!({"tickets":tickets})).into_response()
        }
        Err(error) => { tracing::error!(?error, "admin support listing failed"); error_response(StatusCode::SERVICE_UNAVAILABLE, "support_unavailable") }
    }
}

fn safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 191
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

// ── Admin deployments ──

#[derive(Debug, Serialize, FromRow)]
struct AdminDeploymentRow {
    id: i64,
    user_id: String,
    project_id: String,
    deployment_id: String,
    url: String,
    subdomain: Option<String>,
    status: String,
    file_count: i64,
    message: String,
    created_at: i64,
    username: String,
    display_name: String,
}

#[derive(Debug, Deserialize)]
struct DeploymentQuery {
    status: Option<String>,
    user: Option<String>,
    project: Option<String>,
    page: Option<u32>,
}

async fn all_deployments(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    Query(query): Query<DeploymentQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let page = query.page.unwrap_or(1).clamp(1, 1000);
    let offset = (page - 1) * 50;
    let status_filter = query.status.unwrap_or_default();
    let user_filter = query.user.unwrap_or_default();
    let project_filter = query.project.unwrap_or_default();
    let result = sqlx::query_as::<_, AdminDeploymentRow>(
        "SELECT h.id,h.user_id,h.project_id,h.deployment_id,h.url,h.subdomain,h.status,h.file_count,h.message,h.created_at,
                u.username,u.display_name
         FROM galileo_deployment_history h
         JOIN users u ON u.id = h.user_id
         WHERE (?='' OR h.status=?)
           AND (?='' OR h.user_id LIKE CONCAT('%',?,'%') OR u.username LIKE CONCAT('%',?,'%'))
           AND (?='' OR h.project_id LIKE CONCAT('%',?,'%'))
         ORDER BY h.created_at DESC, h.id DESC
         LIMIT 50 OFFSET ?",
    )
    .bind(&status_filter)
    .bind(&status_filter)
    .bind(&user_filter)
    .bind(&user_filter)
    .bind(&user_filter)
    .bind(&project_filter)
    .bind(&project_filter)
    .bind(offset)
    .fetch_all(pool)
    .await;
    match result {
        Ok(rows) => {
            Json(serde_json::json!({"deployments": rows, "page": page, "page_size": 50})).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "admin deployment listing failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "deployments_unavailable")
        }
    }
}

async fn deployment_detail(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    Path(deployment_id): Path<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let result = sqlx::query_as::<_, AdminDeploymentRow>(
        "SELECT h.id,h.user_id,h.project_id,h.deployment_id,h.url,h.subdomain,h.status,h.file_count,h.message,h.created_at,
                u.username,u.display_name
         FROM galileo_deployment_history h
         JOIN users u ON u.id = h.user_id
         WHERE h.deployment_id=?
         ORDER BY h.created_at DESC
         LIMIT 50",
    )
    .bind(&deployment_id)
    .fetch_all(pool)
    .await;
    match result {
        Ok(rows) => {
            Json(serde_json::json!({"entries": rows})).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "admin deployment detail failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable")
        }
    }
}

#[derive(Debug, Deserialize)]
struct AdminUndeployRequest {
    user_id: String,
    project_id: String,
}

async fn admin_undeploy(
    State(state): State<AppState>,
    auth::AdminUser(admin): auth::AdminUser,
    Json(input): Json<AdminUndeployRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    if !safe_id(&input.user_id) || !safe_id(&input.project_id) {
        return error_response(StatusCode::BAD_REQUEST, "invalid_input");
    }
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or_default();
    let _ = sqlx::query(
        "UPDATE galileo_deployments SET status='undeployed',deployed_at=? WHERE user_id=? AND project_id=?",
    )
    .bind(timestamp)
    .bind(&input.user_id)
    .bind(&input.project_id)
    .execute(pool)
    .await;
    let _ = sqlx::query(
        "INSERT INTO galileo_deployment_history (user_id,project_id,deployment_id,url,subdomain,status,file_count,message,created_at)
         VALUES (?,?,?,?,NULL,'undeployed',0,'admin undeploy',?)",
    )
    .bind(&input.user_id)
    .bind(&input.project_id)
    .bind(format!("dep_admin_undeploy_{timestamp}"))
    .bind("")
    .bind(timestamp)
    .execute(pool)
    .await;
    let _ = sqlx::query(
        "INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at)
         VALUES (?,?,?,?,?,?,?)",
    )
    .bind(&admin.id)
    .bind(&admin.display_name)
    .bind("admin.undeploy")
    .bind("deployment")
    .bind(&input.project_id)
    .bind(format!("admin undeployed project {}", &input.project_id))
    .bind(timestamp)
    .execute(pool)
    .await;
    tracing::warn!(event="admin.deployment.undeployed", admin_id=%admin.id, user_id=%input.user_id, project_id=%input.project_id);
    Json(serde_json::json!({"ok": true})).into_response()
}

// ── Audit log ──

#[derive(Debug, Serialize, FromRow)]
struct AuditEventRow {
    id: i64,
    actor_id: String,
    actor_name: String,
    action: String,
    target_type: String,
    target_id: String,
    detail: Option<String>,
    created_at: i64,
}

#[derive(Debug, Deserialize)]
struct AuditQuery {
    page: Option<u32>,
}

async fn audit_log(
    State(state): State<AppState>,
    auth::AdminUser(_admin): auth::AdminUser,
    Query(query): Query<AuditQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_unavailable");
    };
    let page = query.page.unwrap_or(1).clamp(1, 1000);
    let offset = (page - 1) * 50;
    let result = sqlx::query_as::<_, AuditEventRow>(
        "SELECT id,actor_id,actor_name,action,target_type,target_id,detail,created_at
         FROM admin_audit_events
         ORDER BY created_at DESC, id DESC
         LIMIT 50 OFFSET ?",
    )
    .bind(offset)
    .fetch_all(pool)
    .await;
    match result {
        Ok(rows) => {
            Json(serde_json::json!({"events": rows, "page": page, "page_size": 50})).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "admin audit log failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "audit_unavailable")
        }
    }
}
