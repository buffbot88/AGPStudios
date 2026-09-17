use std::time::Duration;

use axum::{
    body::Body,
    extract::{FromRequestParts, Path as AxumPath, Query, State},
    http::{header, request::Parts, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, MySqlPool};
use uuid::Uuid;

use crate::{response::error_response, AppState};

// ─── Request / Response types ───────────────────────────────────────

#[derive(Debug, Deserialize)]
struct VesperLoginRequest {
    username: String,
    password: String,
}

#[derive(Debug, Serialize)]
struct VesperLoginResponse {
    session_token: String,
    user_id: String,
    username: String,
    role: String,
    quota_bytes: i64,
    expires_at: String,
}

#[derive(Debug, Serialize)]
struct VesperStatusResponse {
    authenticated: bool,
    user_id: Option<String>,
    username: Option<String>,
    role: Option<String>,
    quota_used: i64,
    quota_total: i64,
}

#[derive(Debug, Serialize)]
struct VesperOkResponse {
    ok: bool,
}

#[derive(Debug, Deserialize)]
struct UpdateCheckQuery {
    current: String,
    platform: String,
}

#[derive(Debug, Serialize)]
struct UpdateInfo {
    version: String,
    pub_date: String,
    notes: String,
    platforms: std::collections::HashMap<String, PlatformDownload>,
}

#[derive(Debug, Serialize)]
struct PlatformDownload {
    url: String,
    signature: String,
    file_size: i64,
}

#[derive(Debug, FromRow)]
struct VesperSessionRow {
    #[allow(dead_code)]
    id: String,
    user_id: String,
    #[allow(dead_code)]
    token_hash: String,
}

#[derive(Debug, FromRow)]
struct VesperUserRow {
    id: String,
    username: String,
    password_hash: String,
    role: String,
    is_active: i8,
    email_verified_at: Option<String>,
}

#[derive(Debug, FromRow)]
struct ReleaseRow {
    version: String,
    #[allow(dead_code)]
    platform_rid: String,
    pub_date: chrono::NaiveDateTime,
    notes: String,
    filename: String,
    signature: String,
    file_size: i64,
    download_url: String,
}

#[derive(Debug, FromRow)]
struct QuotaRow {
    used: Option<i64>,
}

// ─── Bearer token extractor ─────────────────────────────────────────

/// Authenticated Vearer user for the Vesper platform API.
#[allow(dead_code)]
pub(crate) struct VesperUser {
    pub user_id: String,
    pub username: String,
    pub role: String,
}

#[axum::async_trait]
impl FromRequestParts<AppState> for VesperUser {
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let Some(pool) = state.db.as_ref() else {
            return Err(error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "auth_unavailable",
            ));
        };

        let token = extract_bearer_token(&parts.headers)
            .ok_or_else(|| error_response(StatusCode::UNAUTHORIZED, "unauthenticated"))?;

        let token_hash = hash_token(&token);

        let row = sqlx::query_as::<_, VesperSessionRow>(
            "SELECT id, user_id, token_hash
             FROM vesper_sessions
             WHERE token_hash = ? AND expires_at > UTC_TIMESTAMP()
             LIMIT 1",
        )
        .bind(&token_hash)
        .fetch_optional(pool)
        .await;

        let session = match row {
            Ok(Some(session)) => session,
            Ok(None) => {
                return Err(error_response(StatusCode::UNAUTHORIZED, "unauthenticated"))
            }
            Err(error) => {
                tracing::error!(?error, "Vesper session lookup failed");
                return Err(error_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "auth_unavailable",
                ));
            }
        };

        // Touch last_seen.
        let _ = sqlx::query(
            "UPDATE vesper_sessions SET last_seen = UTC_TIMESTAMP() WHERE id = ?",
        )
        .bind(&session.id)
        .execute(pool)
        .await;

        // Look up user.
        let user = sqlx::query_as::<_, VesperUserRow>(
            "SELECT id, username, password_hash, role, is_active,
                    CAST(email_verified_at AS CHAR) AS email_verified_at
             FROM users WHERE id = ? AND is_active = 1 LIMIT 1",
        )
        .bind(&session.user_id)
        .fetch_optional(pool)
        .await;

        match user {
            Ok(Some(user)) => Ok(VesperUser {
                user_id: user.id,
                username: user.username,
                role: user.role,
            }),
            Ok(None) => Err(error_response(StatusCode::UNAUTHORIZED, "unauthenticated")),
            Err(error) => {
                tracing::error!(?error, "Vesper user lookup failed");
                Err(error_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "auth_unavailable",
                ))
            }
        }
    }
}

// ─── Routes ─────────────────────────────────────────────────────────

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/v1/auth/login", post(vesper_login))
        // Mint a Vesper bearer session from an existing cookie session, so
        // already signed-in users can authorize the desktop app.
        .route("/api/v1/auth/token-from-session", post(vesper_token_from_session))
        .route("/api/v1/auth/status", get(vesper_status))
        .route("/api/v1/auth/logout", post(vesper_logout))
        .route("/api/v1/updates/check", get(vesper_check_update))
        .route("/api/v1/updates/download/{filename}", get(vesper_download))
        .route("/updates/check", get(vesper_check_update))
        .route("/updates/download/{filename}", get(vesper_download))
        .route("/api/v1/models/list", get(vesper_models_list))
        .route("/api/v1/models/download/{filename}", get(vesper_models_download))
        .route("/api/v1/models/announce", post(vesper_models_announce))
}

// ─── Auth endpoints ─────────────────────────────────────────────────

async fn vesper_login(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<VesperLoginRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let identifier = input.username.trim();
    if identifier.is_empty() || input.password.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "invalid_credentials");
    }

    // Rate limit.
    let rate_key = format!("vesper:login:{}", identifier.to_ascii_lowercase());
    if let Some(retry_after) = state
        .auth_rate_limiter
        .check(&rate_key, 10, Duration::from_secs(60))
    {
        return crate::response::rate_limit_response(retry_after);
    }

    // Look up user.
    let user = sqlx::query_as::<_, VesperUserRow>(
        "SELECT id, username, password_hash, role, is_active,
                CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE username = ? OR email = ? LIMIT 1",
    )
    .bind(identifier)
    .bind(identifier)
    .fetch_optional(pool)
    .await;

    let user = match user {
        Ok(Some(user)) => user,
        _ => {
            state.auth_rate_limiter.check(&rate_key, 1, Duration::from_secs(60));
            return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
        }
    };

    if user.is_active == 0 {
        return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
    }

    // Verify password.
    let password = input.password;
    let password_hash = user.password_hash.replace("$2y$", "$2b$");
    let valid =
        tokio::task::spawn_blocking(move || bcrypt::verify(&password, &password_hash).unwrap_or(false))
            .await
            .unwrap_or(false);

    if !valid {
        state.metrics.record_auth_failure();
        return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
    }
    if state.auth.email_verification_enabled && user.email_verified_at.is_none() {
        return error_response(StatusCode::FORBIDDEN, "email_verification_required");
    }

    state.auth_rate_limiter.clear(&rate_key);

    // Issue token.
    let session_token = new_token();
    let token_hash = hash_token(&session_token);
    let session_id = Uuid::new_v4().to_string();
    let lifetime_secs = state.auth.session_lifetime_seconds.max(30 * 24 * 3600); // at least 30 days

    let ip = client_ip(&headers, &state);
    let ua = headers
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();

    if let Err(error) = sqlx::query(
        "INSERT INTO vesper_sessions
            (id, user_id, token_hash, ip, user_agent, created_at, last_seen, expires_at)
         VALUES (?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND))",
    )
    .bind(&session_id)
    .bind(&user.id)
    .bind(&token_hash)
    .bind(&ip)
    .bind(&ua)
    .bind(lifetime_secs)
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Vesper session creation failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "session_unavailable");
    }

    // Compute quota.
    let quota_used = get_quota_used(pool, &user.id).await;
    let quota_total = user_quota_total(&user.role);

    let expires_at = chrono::Utc::now()
        .checked_add_signed(chrono::Duration::seconds(lifetime_secs))
        .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_default();

    Json(VesperLoginResponse {
        session_token,
        user_id: user.id,
        username: user.username,
        role: user.role,
        quota_bytes: quota_total - quota_used,
        expires_at,
    })
    .into_response()
}

/// POST /api/v1/auth/token-from-session
///
/// Returns a Vesper bearer session_token for the currently authenticated
/// cookie session, so already signed-in users can authorize the Vesper
/// desktop app from the web authorization page.
async fn vesper_token_from_session(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let Some(user) = crate::auth::authenticated_user(pool, &state, &headers)
        .await
        .ok()
        .flatten()
    else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };

    let session_token = new_token();
    let token_hash = hash_token(&session_token);
    let session_id = Uuid::new_v4().to_string();
    let lifetime_secs = state.auth.session_lifetime_seconds.max(30 * 24 * 3600);

    let ip = client_ip(&headers, &state);
    let ua = headers
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();

    if let Err(error) = sqlx::query(
        "INSERT INTO vesper_sessions
            (id, user_id, token_hash, ip, user_agent, created_at, last_seen, expires_at)
         VALUES (?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND))",
    )
    .bind(&session_id)
    .bind(&user.id)
    .bind(&token_hash)
    .bind(&ip)
    .bind(&ua)
    .bind(lifetime_secs)
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Vesper session mint from cookie failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "session_unavailable");
    }

    let quota_used = get_quota_used(pool, &user.id).await;
    let quota_total = user_quota_total(&user.role);

    let expires_at = chrono::Utc::now()
        .checked_add_signed(chrono::Duration::seconds(lifetime_secs))
        .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_default();

    Json(VesperLoginResponse {
        session_token,
        user_id: user.id,
        username: user.username,
        role: user.role,
        quota_bytes: quota_total - quota_used,
        expires_at,
    })
    .into_response()
}

async fn vesper_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let token = match extract_bearer_token(&headers) {
        Some(t) => t,
        None => {
            return Json(VesperStatusResponse {
                authenticated: false,
                user_id: None,
                username: None,
                role: None,
                quota_used: 0,
                quota_total: 0,
            })
            .into_response()
        }
    };

    let token_hash = hash_token(&token);
    let row = sqlx::query_as::<_, VesperSessionRow>(
        "SELECT id, user_id, token_hash
         FROM vesper_sessions
         WHERE token_hash = ? AND expires_at > UTC_TIMESTAMP()
         LIMIT 1",
    )
    .bind(&token_hash)
    .fetch_optional(pool)
    .await;

    let session = match row {
        Ok(Some(s)) => s,
        _ => {
            return Json(VesperStatusResponse {
                authenticated: false,
                user_id: None,
                username: None,
                role: None,
                quota_used: 0,
                quota_total: 0,
            })
            .into_response()
        }
    };

    let user = sqlx::query_as::<_, VesperUserRow>(
        "SELECT id, username, password_hash, role, is_active,
                CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE id = ? AND is_active = 1 LIMIT 1",
    )
    .bind(&session.user_id)
    .fetch_optional(pool)
    .await;

    match user {
        Ok(Some(user)) => {
            let quota_used = get_quota_used(pool, &user.id).await;
            let quota_total = user_quota_total(&user.role);
            Json(VesperStatusResponse {
                authenticated: true,
                user_id: Some(user.id),
                username: Some(user.username),
                role: Some(user.role),
                quota_used,
                quota_total,
            })
            .into_response()
        }
        _ => Json(VesperStatusResponse {
            authenticated: false,
            user_id: None,
            username: None,
            role: None,
            quota_used: 0,
            quota_total: 0,
        })
        .into_response(),
    }
}

async fn vesper_logout(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    if let Some(token) = extract_bearer_token(&headers) {
        let token_hash = hash_token(&token);
        let _ = sqlx::query("DELETE FROM vesper_sessions WHERE token_hash = ?")
            .bind(&token_hash)
            .execute(pool)
            .await;
    }

    Json(VesperOkResponse { ok: true }).into_response()
}

// ─── Updates ────────────────────────────────────────────────────────

async fn vesper_check_update(
    State(state): State<AppState>,
    Query(query): Query<UpdateCheckQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let current = query.current.trim().to_owned();
    let platform = query.platform.trim().to_owned();

    if current.is_empty() || platform.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "missing_parameters");
    }

    // Find the latest release for this platform where version > current.
    let rows = sqlx::query_as::<_, ReleaseRow>(
        "SELECT version, platform_rid, pub_date, notes, filename, signature, file_size, download_url
         FROM vesper_releases
         WHERE platform_rid = ?
         ORDER BY
           CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(version, '.', 1), '.', -1) AS UNSIGNED) DESC,
           CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(version, '.', 2), '.', -1) AS UNSIGNED) DESC,
           CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(version, '.', 3), '.', -1) AS UNSIGNED) DESC
         LIMIT 20",
    )
    .bind(&platform)
    .fetch_all(pool)
    .await;

    let rows = match rows {
        Ok(rows) => rows,
        Err(error) => {
            tracing::error!(?error, "Vesper release lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "updates_unavailable");
        }
    };

    // Find the first release newer than current.
    let newer = rows.iter().find(|r| semver_gt(&r.version, &current));

    let Some(latest) = newer else {
        // Up to date — return empty object.
        return Json(serde_json::json!({})).into_response();
    };

    // Build download URL.
    let download_url = if !latest.download_url.is_empty() {
        latest.download_url.clone()
    } else {
        format!(
            "{}/api/v1/updates/download/{}",
            state
                .hub_public_url
                .as_deref()
                .unwrap_or_default(),
            urlencoding::encode(&latest.filename)
        )
    };

    let mut platforms = std::collections::HashMap::new();
    platforms.insert(
        platform.clone(),
        PlatformDownload {
            url: download_url,
            signature: latest.signature.clone(),
            file_size: latest.file_size,
        },
    );

    Json(UpdateInfo {
        version: latest.version.clone(),
        pub_date: latest.pub_date.format("%Y-%m-%dT%H:%M:%S").to_string(),
        notes: latest.notes.clone(),
        platforms,
    })
    .into_response()
}

// ─── Downloads ──────────────────────────────────────────────────────

async fn vesper_download(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(filename): AxumPath<String>,
) -> Response {
    let ip = client_ip(&headers, &state);
    if let Some(retry_after) = state.auth_rate_limiter.check(
        &format!("vesper:download:{ip}"),
        10,
        Duration::from_secs(60),
    ) {
        return crate::response::rate_limit_response(retry_after);
    }

    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    // Validate filename (no path traversal).
    if filename.contains("..") || filename.contains('/') || filename.contains('\\') {
        return error_response(StatusCode::BAD_REQUEST, "invalid_filename");
    }

    // Look up the release.
    let release = sqlx::query_as::<_, ReleaseRow>(
        "SELECT version, platform_rid, pub_date, notes, filename, signature, file_size, download_url
         FROM vesper_releases WHERE filename = ? LIMIT 1",
    )
    .bind(&filename)
    .fetch_optional(pool)
    .await;

    let release = match release {
        Ok(Some(r)) => r,
        _ => {
            return error_response(StatusCode::NOT_FOUND, "release_not_found");
        }
    };

    // If a presigned URL exists, redirect.
    if !release.download_url.is_empty() {
        let mut response = Redirect::temporary(&release.download_url).into_response();
        response.headers_mut().insert(
            "cache-control",
            HeaderValue::from_static("public, max-age=86400"),
        );
        return response;
    }

    // Serve from local filesystem.
    let releases_dir = state.releases_dir.clone();
    let file_path = releases_dir.join(&filename);

    if !file_path.exists() || !file_path.is_file() {
        return error_response(StatusCode::NOT_FOUND, "file_not_found");
    }

    // Determine content type.
    let content_type = match file_path
        .extension()
        .and_then(|e| e.to_str())
    {
        Some("zip") => "application/zip",
        Some("gz") | Some("tar") => "application/gzip",
        Some("msi") => "application/x-msi",
        Some("dmg") => "application/x-apple-diskimage",
        Some("AppImage") => "application/x-appimage",
        Some("deb") => "application/x-debian-package",
        Some("rpm") => "application/x-rpm",
        _ => "application/octet-stream",
    };

    let file_size = release.file_size as u64;

    // Stream the file.
    match tokio::fs::read(&file_path).await {
        Ok(bytes) => {
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, content_type)
                .header(header::CONTENT_LENGTH, file_size)
                .header("cache-control", "public, max-age=86400")
                .header(
                    "content-disposition",
                    format!("attachment; filename=\"{}\"", filename),
                )
                .body(Body::from(bytes))
                .unwrap_or_else(|_| error_response(StatusCode::INTERNAL_SERVER_ERROR, "build_response_failed"))
        }
        Err(error) => {
            tracing::error!(?error, %filename, "Failed to read release file");
            error_response(StatusCode::INTERNAL_SERVER_ERROR, "file_read_failed")
        }
    }
}

// ─── Helpers ────────────────────────────────────────────────────────

fn extract_bearer_token(headers: &HeaderMap) -> Option<String> {
    let value = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let token = value.strip_prefix("Bearer ")?;
    if token.is_empty() || token.len() > 512 {
        return None;
    }
    Some(token.to_owned())
}

fn hash_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn new_token() -> String {
    use rand::Rng;
    let suffix: String = rand::thread_rng()
        .sample_iter(&rand::distributions::Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    format!("{}{}", Uuid::new_v4().simple(), suffix)
}

fn client_ip(headers: &HeaderMap, state: &AppState) -> String {
    if state.auth.trust_proxy_headers {
        for name in ["x-real-ip", "x-forwarded-for"] {
            if let Some(value) = headers.get(name).and_then(|v| v.to_str().ok()) {
                let candidate = value.split(',').next().unwrap_or_default().trim();
                if !candidate.is_empty() && candidate.len() <= 64 && candidate.is_ascii() {
                    return candidate.to_owned();
                }
            }
        }
    }
    "unknown".to_owned()
}

async fn get_quota_used(pool: &MySqlPool, user_id: &str) -> i64 {
    // Sum of file_size from galileo_projects for this user.
    sqlx::query_as::<_, QuotaRow>(
        "SELECT COALESCE(SUM(file_size), 0) as used
         FROM galileo_projects WHERE user_id = ?",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
    .and_then(|r| r.used)
    .unwrap_or(0)
}

fn user_quota_total(role: &str) -> i64 {
    match role {
        "Admin" => 10 * 1024 * 1024 * 1024, // 10 GB
        "Pro" => 5 * 1024 * 1024 * 1024,     // 5 GB
        _ => 1 * 1024 * 1024 * 1024,          // 1 GB
    }
}

/// Simple semver comparison: returns true if `a > b`.
fn semver_gt(a: &str, b: &str) -> bool {
    let parse = |v: &str| -> (u32, u32, u32) {
        let parts: Vec<u32> = v
            .split('.')
            .filter_map(|s| s.parse().ok())
            .collect();
        (
            parts.first().copied().unwrap_or(0),
            parts.get(1).copied().unwrap_or(0),
            parts.get(2).copied().unwrap_or(0),
        )
    };
    let (a1, a2, a3) = parse(a);
    let (b1, b2, b3) = parse(b);
    (a1, a2, a3) > (b1, b2, b3)
}

use axum::response::Redirect;

// ─── Models ────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct ModelListQuery {
    r#type: Option<String>,
    platform: Option<String>,
}

#[derive(Debug, Serialize)]
struct ModelInfo {
    id: String,
    name: String,
    slug: String,
    description: String,
    model_type: String,
    version: String,
    filename: String,
    signature: String,
    file_size: i64,
    download_url: String,
    platform_rid: String,
    min_ram_mb: i32,
    quantization: String,
    size_bytes: i64,
    checksum: String,
    origin: String,
    seeds: Vec<String>,
}

#[derive(Debug, FromRow)]
struct ModelRow {
    id: String,
    name: String,
    slug: String,
    description: String,
    model_type: String,
    version: String,
    filename: String,
    signature: String,
    file_size: i64,
    download_url: String,
    platform_rid: String,
    min_ram_mb: i32,
    quantization: String,
    checksum: String,
    origin_url: String,
}

#[derive(Debug, Serialize)]
struct ModelListResponse {
    models: Vec<ModelInfo>,
    total: usize,
}

async fn vesper_models_list(
    State(state): State<AppState>,
    Query(query): Query<ModelListQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
    };

    // Build query with optional filters.
    let mut sql = String::from(
        "SELECT id, name, slug, description, model_type, version, filename, signature,
                file_size, download_url, platform_rid, min_ram_mb, quantization,
                checksum, origin_url
         FROM vesper_models WHERE is_active = 1",
    );

    let type_filter = query.r#type.as_deref().filter(|t| !t.is_empty());
    let platform_filter = query.platform.as_deref().filter(|p| !p.is_empty());

    if type_filter.is_some() {
        sql.push_str(" AND model_type = ?");
    }
    if platform_filter.is_some() {
        sql.push_str(" AND (platform_rid = '' OR platform_rid = ?)");
    }
    sql.push_str(" ORDER BY model_type, name");

    let mut query_builder = sqlx::query_as::<_, ModelRow>(&sql);
    if let Some(t) = type_filter {
        query_builder = query_builder.bind(t);
    }
    if let Some(p) = platform_filter {
        query_builder = query_builder.bind(p);
    }

    let rows = match query_builder.fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => {
            tracing::error!(?error, "Vesper models list failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
        }
    };

    let mut models = Vec::with_capacity(rows.len());
    for r in rows {
        let seeds = sqlx::query_scalar::<_, String>(
            "SELECT origin_url FROM vesper_model_seeds WHERE model_id = ? ORDER BY last_seen DESC",
        )
        .bind(&r.id)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        models.push(ModelInfo {
            id: r.id,
            name: r.name,
            slug: r.slug,
            description: r.description,
            model_type: r.model_type,
            version: r.version,
            filename: r.filename,
            signature: r.signature,
            file_size: r.file_size,
            download_url: r.download_url,
            platform_rid: r.platform_rid,
            min_ram_mb: r.min_ram_mb,
            quantization: r.quantization,
            size_bytes: r.file_size,
            checksum: r.checksum,
            origin: r.origin_url,
            seeds,
        });
    }

    let total = models.len();
    Json(ModelListResponse { models, total }).into_response()
}

#[derive(Debug, Deserialize)]
struct ModelAnnounceRequest {
    filename: String,
    size_bytes: i64,
    checksum: String,
    origin: String,
}

async fn vesper_models_announce(
    State(state): State<AppState>,
    VesperUser { user_id, .. }: VesperUser,
    Json(input): Json<ModelAnnounceRequest>,
) -> Response {
    let filename = input.filename.trim();
    let checksum = input.checksum.trim();
    let origin = input.origin.trim();
    if filename.is_empty()
        || filename.len() > 255
        || filename.contains("..")
        || filename.contains('/')
        || filename.contains('\\')
        || input.size_bytes <= 0
        || checksum.is_empty()
        || checksum.len() > 128
        || !checksum.is_ascii()
        || !(origin.starts_with("http://") || origin.starts_with("https://"))
        || origin.len() > 512
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_model_announcement");
    }
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
    };

    let model_id = match sqlx::query_scalar::<_, String>(
        "SELECT id FROM vesper_models WHERE filename = ? LIMIT 1",
    )
    .bind(filename)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(id)) => id,
        Ok(None) => {
            let id = format!("vm-{}", Uuid::new_v4().simple());
            let slug = format!("seed-{}", Uuid::new_v4().simple());
            if sqlx::query(
                "INSERT INTO vesper_models
                 (id,name,slug,description,model_type,version,filename,checksum,file_size,origin_url,is_active)
                 VALUES (?,?,?,'','llm','1.0.0',?,?,?, ?,1)",
            )
            .bind(&id)
            .bind(filename)
            .bind(&slug)
            .bind(filename)
            .bind(checksum)
            .bind(input.size_bytes)
            .bind(origin)
            .execute(pool)
            .await
            .is_err()
            {
                return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
            }
            id
        }
        Err(error) => {
            tracing::error!(?error, "Vesper model lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
        }
    };

    if let Err(error) = sqlx::query(
        "INSERT INTO vesper_model_seeds (id,model_id,user_id,origin_url,last_seen)
         VALUES (?,?,?,?,UTC_TIMESTAMP())
         ON DUPLICATE KEY UPDATE origin_url=VALUES(origin_url), last_seen=UTC_TIMESTAMP()",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&model_id)
    .bind(&user_id)
    .bind(origin)
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Vesper model seed announcement failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
    }

    Json(serde_json::json!({"ok": true, "model_id": model_id})).into_response()
}

async fn vesper_models_download(
    State(state): State<AppState>,
    AxumPath(filename): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "models_unavailable");
    };

    // Validate filename.
    if filename.contains("..") || filename.contains('/') || filename.contains('\\') {
        return error_response(StatusCode::BAD_REQUEST, "invalid_filename");
    }

    // Look up the model.
    let model = sqlx::query_as::<_, ModelRow>(
        "SELECT id, name, slug, description, model_type, version, filename, signature,
                file_size, download_url, platform_rid, min_ram_mb, quantization,
                checksum, origin_url
         FROM vesper_models WHERE filename = ? AND is_active = 1 LIMIT 1",
    )
    .bind(&filename)
    .fetch_optional(pool)
    .await;

    let model = match model {
        Ok(Some(m)) => m,
        _ => {
            return error_response(StatusCode::NOT_FOUND, "model_not_found");
        }
    };

    // If a presigned URL exists, redirect.
    if !model.download_url.is_empty() {
        let mut response = Redirect::temporary(&model.download_url).into_response();
        response.headers_mut().insert(
            "cache-control",
            HeaderValue::from_static("public, max-age=86400"),
        );
        return response;
    }

    // Serve from local filesystem.
    let models_dir = state.releases_dir.join("models");
    let file_path = models_dir.join(&filename);

    if !file_path.exists() || !file_path.is_file() {
        return error_response(StatusCode::NOT_FOUND, "file_not_found");
    }

    let content_type = match file_path.extension().and_then(|e| e.to_str()) {
        Some("gguf") => "application/gguf",
        Some("bin") => "application/octet-stream",
        Some("onnx") => "application/onnx",
        Some("pt") | Some("pth") => "application/pytorch",
        _ => "application/octet-stream",
    };

    let file_size = model.file_size as u64;

    match tokio::fs::read(&file_path).await {
        Ok(bytes) => {
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, content_type)
                .header(header::CONTENT_LENGTH, file_size)
                .header("cache-control", "public, max-age=86400")
                .header(
                    "content-disposition",
                    format!("attachment; filename=\"{}\"", filename),
                )
                .body(Body::from(bytes))
                .unwrap_or_else(|_| error_response(StatusCode::INTERNAL_SERVER_ERROR, "build_response_failed"))
        }
        Err(error) => {
            tracing::error!(?error, %filename, "Failed to read model file");
            error_response(StatusCode::INTERNAL_SERVER_ERROR, "file_read_failed")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::semver_gt;

    #[test]
    fn semver_comparison() {
        assert!(semver_gt("1.1.0", "1.0.9"));
        assert!(semver_gt("2.0.0", "1.99.99"));
        assert!(!semver_gt("1.0.8", "1.0.9"));
        assert!(!semver_gt("1.0.9", "1.0.9"));
        assert!(semver_gt("1.0.10", "1.0.9"));
        assert!(semver_gt("0.9.0", "0.8.99"));
    }
}
