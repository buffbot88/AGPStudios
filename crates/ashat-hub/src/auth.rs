use std::{net::SocketAddr, time::Duration};

use axum::{
    extract::{ConnectInfo, FromRequestParts, Query, State},
    http::{header, request::Parts, HeaderMap, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Redirect, Response},
    routing::{get, post},
    Json, Router,
};
use bcrypt::{hash, verify, DEFAULT_COST};
use rand::{distributions::Alphanumeric, Rng};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, MySqlPool};
use uuid::Uuid;

use crate::{
    response::{error_response, rate_limit_response},
    AppState,
};

#[derive(Debug, Deserialize)]
pub(crate) struct LoginRequest {
    pub identifier: String,
    pub password: String,
}

#[derive(Debug, Deserialize)]
struct ProfileUpdate {
    username: Option<String>,
    tag_name: Option<String>,
    display_name: Option<String>,
    email: Option<String>,
    discord_tag: Option<String>,
    location: Option<String>,
    interests: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RegisterRequest {
    username: String,
    email: String,
    password: String,
    display_name: Option<String>,
}

#[derive(Debug, Deserialize)]
struct VerifyEmailQuery {
    token: String,
}

#[derive(Debug, Deserialize)]
struct ResendVerificationRequest {
    identifier: String,
}

#[derive(Debug, Deserialize)]
struct PasswordResetRequest {
    identifier: String,
}

#[derive(Debug, Deserialize)]
struct PasswordResetConfirm {
    token: String,
    password: String,
}

#[derive(Debug, Serialize)]
struct AuthResponse {
    authenticated: bool,
    user: Option<PublicUser>,
    csrf: Option<String>,
}

#[derive(Debug, Serialize)]
struct LoginResponse {
    authenticated: bool,
    user: PublicUser,
    csrf: String,
}

#[derive(Debug, Serialize)]
struct OkResponse {
    ok: bool,
}

#[derive(Debug, Clone, Serialize, FromRow)]
pub(crate) struct PublicUser {
    pub id: String,
    pub username: String,
    pub email: String,
    pub display_name: String,
    pub tag_name: Option<String>,
    pub discord_tag: Option<String>,
    pub location: Option<String>,
    pub interests: Option<String>,
    pub role: String,
}

#[derive(Debug, Clone)]
pub(crate) struct AuthenticatedUser(pub(crate) PublicUser);

#[axum::async_trait]
impl FromRequestParts<AppState> for AuthenticatedUser {
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
        match authenticated_user(pool, state, &parts.headers).await {
            Ok(Some(user)) => Ok(Self(user)),
            Ok(None) => {
                state.metrics.record_auth_failure();
                Err(error_response(StatusCode::UNAUTHORIZED, "unauthenticated"))
            }
            Err(error) => {
                tracing::error!(?error, "authenticated user lookup failed");
                Err(error_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "auth_unavailable",
                ))
            }
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct AdminUser(pub(crate) PublicUser);

#[axum::async_trait]
impl FromRequestParts<AppState> for AdminUser {
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
        match authenticated_user(pool, state, &parts.headers).await {
            Ok(Some(user)) if user.role.eq_ignore_ascii_case("admin") => Ok(Self(user)),
            Ok(Some(_)) => Err(error_response(StatusCode::FORBIDDEN, "admin_required")),
            Ok(None) => {
                state.metrics.record_auth_failure();
                Err(error_response(StatusCode::UNAUTHORIZED, "unauthenticated"))
            }
            Err(error) => {
                tracing::error!(?error, "admin user lookup failed");
                Err(error_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "auth_unavailable",
                ))
            }
        }
    }
}

#[allow(dead_code)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct ServiceRequest;

#[axum::async_trait]
impl FromRequestParts<AppState> for ServiceRequest {
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let Some(expected) = state.auth.service_token.as_deref() else {
            return Err(error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "service_auth_unavailable",
            ));
        };
        let Some(provided) = parts
            .headers
            .get("x-ashat-service-token")
            .and_then(|value| value.to_str().ok())
        else {
            return Err(error_response(
                StatusCode::UNAUTHORIZED,
                "service_unauthenticated",
            ));
        };
        if !constant_time_equal(provided, expected) {
            return Err(error_response(
                StatusCode::UNAUTHORIZED,
                "service_unauthenticated",
            ));
        }
        Ok(Self)
    }
}

#[derive(Debug, FromRow)]
struct UserWithPassword {
    id: String,
    username: String,
    email: String,
    password_hash: String,
    display_name: String,
    tag_name: Option<String>,
    discord_tag: Option<String>,
    location: Option<String>,
    interests: Option<String>,
    role: String,
    is_active: i8,
    email_verified_at: Option<String>,
}

#[derive(Debug, FromRow)]
struct RustSessionUser {
    id: String,
    username: String,
    email: String,
    display_name: String,
    tag_name: Option<String>,
    discord_tag: Option<String>,
    location: Option<String>,
    interests: Option<String>,
    role: String,
    email_verified_at: Option<String>,
    csrf_hash: String,
}

#[derive(Debug, FromRow)]
struct LegacySessionUser {
    id: String,
    username: String,
    email: String,
    display_name: String,
    tag_name: Option<String>,
    discord_tag: Option<String>,
    location: Option<String>,
    interests: Option<String>,
    role: String,
    email_verified_at: Option<String>,
}

#[derive(Debug)]
enum SessionKind {
    Rust { csrf_hash: String },
    Legacy,
}

#[derive(Debug)]
struct CurrentAuth {
    user: PublicUser,
    kind: SessionKind,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/auth/session", get(session))
        .route("/api/auth/me", get(me))
        .route("/api/me", get(me))
        .route("/api/auth/github", get(github_authorize))
        .route("/api/github/callback", get(github_callback))
        .route("/api/auth/login", post(login))
        .route("/api/auth/register", post(register))
        .route("/api/auth/verify-email", get(verify_email))
        .route("/api/auth/verify-email/resend", post(resend_verification))
        .route("/api/auth/password-reset/request", post(request_password_reset))
        .route("/api/auth/password-reset/confirm", post(confirm_password_reset))
        .route("/api/auth/logout", post(logout))
        .route("/api/account", get(account).put(update_account))
}

#[derive(Debug, Deserialize)]
struct GithubCallback { code: String, state: String }
#[derive(Debug, Deserialize)]
struct GithubToken { access_token: String, refresh_token: Option<String>, expires_in: Option<i64> }
#[derive(Debug, Deserialize)]
struct GithubUser { id: i64, login: String, name: Option<String>, email: Option<String> }
#[derive(Debug, Deserialize)]
struct GithubEmail { email: String, primary: bool, verified: bool }

async fn github_authorize(State(state): State<AppState>) -> Response {
    let Some(client_id) = std::env::var("GITHUB_CLIENT_ID").ok().filter(|v| !v.is_empty()) else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "github_auth_unavailable");
    };
    let state_token = new_token();
    let url = format!("https://github.com/login/oauth/authorize?client_id={client_id}&redirect_uri=https%3A%2F%2Fagpstudios.org%2Fapi%2Fgithub%2Fcallback&scope=user%3Aemail%20repo&state={state_token}");
    let mut response = Redirect::to(&url).into_response();
    response.headers_mut().append(header::SET_COOKIE, HeaderValue::from_str(&format!("ashat_github_state={state_token}; Domain=agpstudios.org; Path=/; Max-Age=600; HttpOnly; SameSite=Lax{}", if state.auth.secure_cookie { "; Secure" } else { "" })).unwrap());
    response
}

async fn github_callback(State(state): State<AppState>, headers: HeaderMap, Query(input): Query<GithubCallback>) -> Response {
    let Some(expected) = cookie_value(&headers, "ashat_github_state") else { return error_response(StatusCode::BAD_REQUEST, "github_state_invalid"); };
    if !constant_time_equal(&expected, &input.state) { return error_response(StatusCode::BAD_REQUEST, "github_state_invalid"); }
    let (Some(client_id), Some(client_secret)) = (std::env::var("GITHUB_CLIENT_ID").ok(), std::env::var("GITHUB_CLIENT_SECRET").ok()) else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "github_auth_unavailable"); };
    let token = match state.client.post("https://github.com/login/oauth/access_token").header(header::ACCEPT, "application/json").form(&serde_json::json!({"client_id": client_id, "client_secret": client_secret, "code": input.code, "redirect_uri": "https://agpstudios.org/api/github/callback"})).send().await.and_then(|r| r.error_for_status()) { Ok(response) => match response.json::<GithubToken>().await { Ok(token) => token, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_token_failed") }, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_token_failed") };
    let profile = match state.client.get("https://api.github.com/user").header(header::USER_AGENT, "AshatHub").bearer_auth(&token.access_token).send().await.and_then(|r| r.error_for_status()) { Ok(response) => match response.json::<GithubUser>().await { Ok(user) => user, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_profile_failed") }, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_profile_failed") };
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable"); };
    let current = current_auth(pool, &state, &headers).await.ok().flatten();
    let user_id = if let Some(current) = current { current.user.id } else if let Ok(Some(id)) = sqlx::query_scalar::<_, String>("SELECT id FROM users WHERE github_id=?").bind(profile.id.to_string()).fetch_optional(pool).await { id } else {
        let email = match profile.email.filter(|email| !email.is_empty()) { Some(email) => email, None => match state.client.get("https://api.github.com/user/emails").header(header::USER_AGENT, "AshatHub").bearer_auth(&token.access_token).send().await.and_then(|r| r.error_for_status()) { Ok(response) => match response.json::<Vec<GithubEmail>>().await { Ok(emails) => match emails.iter().find(|email| email.primary && email.verified).or_else(|| emails.iter().find(|email| email.verified)) { Some(email) => email.email.clone(), None => return error_response(StatusCode::BAD_REQUEST, "github_email_unavailable") }, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_email_failed") }, Err(_) => return error_response(StatusCode::BAD_GATEWAY, "github_email_failed") } };
        if let Ok(Some(id)) = sqlx::query_scalar::<_, String>("SELECT id FROM users WHERE email=?").bind(&email).fetch_optional(pool).await { id } else {
            let id = Uuid::new_v4().to_string(); let username = format!("{}-{}", profile.login, &id[..8]); let password = hash(new_token(), DEFAULT_COST).unwrap(); let display = profile.name.unwrap_or_else(|| profile.login.clone());
            if sqlx::query("INSERT INTO users (id,username,email,password_hash,display_name,role,is_active,email_verified_at,github_id,github_login) VALUES (?,?,?,? ,?,'member',1,UTC_TIMESTAMP(),?,?)").bind(&id).bind(&username).bind(&email).bind(&password).bind(&display).bind(profile.id.to_string()).bind(&profile.login).execute(pool).await.is_err() { return error_response(StatusCode::CONFLICT, "github_account_failed"); } id
        }
    };
    let expires = token.expires_in.unwrap_or(28800);
    let _ = sqlx::query("UPDATE users SET github_id=?,github_login=?,github_access_token=?,github_refresh_token=?,github_token_expires_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND),email_verified_at=COALESCE(email_verified_at,UTC_TIMESTAMP()) WHERE id=?").bind(profile.id.to_string()).bind(&profile.login).bind(&token.access_token).bind(token.refresh_token.as_deref()).bind(expires).bind(&user_id).execute(pool).await;
    let session = new_token(); let csrf = new_token(); let lifetime = state.auth.session_lifetime_seconds;
    if sqlx::query("INSERT INTO rust_sessions (session_hash,user_id,csrf_hash,created_at,last_seen,expires_at) VALUES (?,?,?,UTC_TIMESTAMP(),UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL ? SECOND))").bind(hash_token(&session)).bind(&user_id).bind(hash_token(&csrf)).bind(lifetime).execute(pool).await.is_err() { return error_response(StatusCode::SERVICE_UNAVAILABLE, "session_unavailable"); }
    let mut response = Redirect::to("/").into_response(); response.headers_mut().append(header::SET_COOKIE, HeaderValue::from_str("ashat_github_state=; Domain=agpstudios.org; Path=/; Max-Age=0; HttpOnly; SameSite=Lax").unwrap()); with_auth_cookies(response, &state, &session, &csrf, lifetime)
}

async fn login(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(input): Json<LoginRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let identifier = input.identifier.trim();
    if identifier.is_empty() || identifier.len() > 254 || input.password.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "invalid_credentials");
    }

    let rate_key = format!(
        "login:{}:{}",
        client_key(&headers, &state.auth, peer),
        identifier.to_ascii_lowercase()
    );
    if let Some(retry_after) = state
        .auth_rate_limiter
        .check(&rate_key, 10, Duration::from_secs(60))
    {
        state.metrics.record_rate_limited();
        return rate_limit_response(retry_after);
    }

    let user = match sqlx::query_as::<_, UserWithPassword>(
        "SELECT id, username, email, password_hash, display_name, tag_name, discord_tag, location, interests, role, is_active,
                CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE username = ? OR email = ? LIMIT 1",
    )
    .bind(identifier)
    .bind(identifier)
    .fetch_optional(pool)
    .await
    {
        Ok(user) => user,
        Err(error) => {
            tracing::error!(?error, "authentication user lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    };

    let Some(user) = user else {
        state.metrics.record_auth_failure();
        tracing::warn!(
            event = "auth.login.failure",
            request_id = %request_id(&headers),
            reason = "invalid_credentials"
        );
        return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
    };
    if user.is_active == 0 {
        state.metrics.record_auth_failure();
        tracing::warn!(
            event = "auth.login.failure",
            request_id = %request_id(&headers),
            reason = "inactive_account"
        );
        return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
    }
    let password = input.password;
    // PHP password_hash(PASSWORD_BCRYPT) commonly emits $2y$. The Rust
    // bcrypt implementation accepts the equivalent $2b$ prefix.
    let password_hash = user.password_hash.replace("$2y$", "$2b$");
    let valid =
        tokio::task::spawn_blocking(move || verify(password, &password_hash).unwrap_or(false))
            .await
            .unwrap_or(false);
    if !valid {
        state.metrics.record_auth_failure();
        tracing::warn!(
            event = "auth.login.failure",
            request_id = %request_id(&headers),
            reason = "invalid_credentials"
        );
        return error_response(StatusCode::UNAUTHORIZED, "invalid_credentials");
    }

    if state.auth.email_verification_enabled && user.email_verified_at.is_none() {
        return error_response(StatusCode::FORBIDDEN, "email_verification_required");
    }

    tracing::info!(
        event = "auth.login.success",
        request_id = %request_id(&headers),
        user_id = %user.id
    );
    state.auth_rate_limiter.clear(&rate_key);
    let session_token = new_token();
    let csrf_token = new_token();
    let session_hash = hash_token(&session_token);
    let csrf_hash = hash_token(&csrf_token);
    let lifetime = state.auth.session_lifetime_seconds;

    if let Err(error) = sqlx::query(
        "INSERT INTO rust_sessions
            (session_hash, user_id, csrf_hash, ip, user_agent, created_at, last_seen, expires_at)
         VALUES (?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND))",
    )
    .bind(&session_hash)
    .bind(&user.id)
    .bind(&csrf_hash)
    .bind(client_key(&headers, &state.auth, peer))
    .bind(headers.get(header::USER_AGENT).and_then(|value| value.to_str().ok()).map(|value| value.to_owned()))
    .bind(lifetime)
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Rust session creation failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "session_unavailable");
    }

    let response = Json(LoginResponse {
        authenticated: true,
        user: public_user(&user),
        csrf: csrf_token.clone(),
    })
    .into_response();
    with_auth_cookies(response, &state, &session_token, &csrf_token, lifetime)
}

async fn register(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(input): Json<RegisterRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let rate_key = format!("register:{}", client_key(&headers, &state.auth, peer));
    if let Some(retry_after) = state
        .auth_rate_limiter
        .check(&rate_key, 5, Duration::from_secs(3600))
    {
        return rate_limit_response(retry_after);
    }

    let username = input.username.trim().to_owned();
    let email = input.email.trim().to_owned();
    let display_name = input
        .display_name
        .as_deref()
        .unwrap_or(username.as_str())
        .trim()
        .to_owned();
    if username.len() < 3
        || username.len() > 30
        || !username
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        || !valid_email(&email)
        || input.password.len() < 8
        || display_name.is_empty()
        || display_name.chars().count() > 200
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_registration");
    }

    let exists = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM users WHERE username=? OR email=?",
    )
    .bind(&username)
    .bind(&email)
    .fetch_one(pool)
    .await
    .unwrap_or(1);
    if exists != 0 {
        return error_response(StatusCode::CONFLICT, "username_or_email_taken");
    }

    let password_hash = match tokio::task::spawn_blocking(move || hash(input.password, DEFAULT_COST)).await {
        Ok(Ok(value)) => value,
        _ => return error_response(StatusCode::SERVICE_UNAVAILABLE, "registration_unavailable"),
    };
    let id = Uuid::new_v4().to_string();

    if !state.auth.email_verification_enabled {
        if let Err(error) = sqlx::query(
            "INSERT INTO users (id,username,email,password_hash,display_name,role,is_active)
             VALUES (?,?,?,?,?,'Member',1)",
        )
        .bind(&id)
        .bind(&username)
        .bind(&email)
        .bind(password_hash)
        .bind(&display_name)
        .execute(pool)
        .await
        {
            tracing::error!(?error, "registration insert failed");
            return error_response(StatusCode::CONFLICT, "username_or_email_taken");
        }
        tracing::info!(
            event = "auth.register.success",
            request_id = %request_id(&headers),
            user_id = %id
        );
        return (StatusCode::CREATED, Json(serde_json::json!({
            "registered": true,
            "verification_required": false,
            "user": {
                "id": id,
                "username": username,
                "email": email,
                "display_name": display_name,
                "role": "Member"
            }
        })))
        .into_response();
    }

    let token = new_token();
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => {
            tracing::error!(?error, "registration transaction failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "registration_unavailable");
        }
    };
    if let Err(error) = sqlx::query(
        "INSERT INTO users (id,username,email,password_hash,display_name,role,is_active)
         VALUES (?,?,?,?,?,'Member',1)",
    )
    .bind(&id)
    .bind(&username)
    .bind(&email)
    .bind(password_hash)
    .bind(&display_name)
    .execute(&mut *transaction)
    .await
    {
        tracing::error!(?error, "registration insert failed");
        return error_response(StatusCode::CONFLICT, "username_or_email_taken");
    }
    let _ = sqlx::query("DELETE FROM email_verifications WHERE user_id=? AND used=0")
        .bind(&id)
        .execute(&mut *transaction)
        .await;
    if let Err(error) = sqlx::query(
        "INSERT INTO email_verifications (id,user_id,token_hash,expires_at,used)
         VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(), INTERVAL 24 HOUR),0)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&id)
    .bind(hash_token(&token))
    .execute(&mut *transaction)
    .await
    {
        tracing::error!(?error, "verification token insert failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "registration_unavailable");
    }

    if let Err(error) = send_verification_email(&state, &email, &display_name, &token).await {
        tracing::error!(?error, user_id = %id, "verification email delivery failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "email_delivery_unavailable");
    }
    if let Err(error) = transaction.commit().await {
        tracing::error!(?error, "registration transaction commit failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "registration_unavailable");
    }

    tracing::info!(
        event = "auth.register.pending_verification",
        request_id = %request_id(&headers),
        user_id = %id
    );
    (StatusCode::ACCEPTED, Json(serde_json::json!({
        "registered": true,
        "verification_required": true,
        "email": email
    })))
    .into_response()
}

async fn verify_email(
    State(state): State<AppState>,
    Query(query): Query<VerifyEmailQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    if query.token.len() < 32 || query.token.len() > 128 {
        return error_response(StatusCode::BAD_REQUEST, "verification_invalid");
    }

    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => {
            tracing::error!(?error, "verification transaction failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    };
    let row = sqlx::query_as::<_, (String,)>(
        "SELECT user_id FROM email_verifications
         WHERE token_hash=? AND used=0 AND expires_at > UTC_TIMESTAMP()
         FOR UPDATE",
    )
    .bind(hash_token(&query.token))
    .fetch_optional(&mut *transaction)
    .await;
    let Some((user_id,)) = (match row {
        Ok(row) => row,
        Err(error) => {
            tracing::error!(?error, "verification lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    }) else {
        return error_response(StatusCode::BAD_REQUEST, "verification_invalid");
    };

    if let Err(error) = sqlx::query("UPDATE users SET email_verified_at=UTC_TIMESTAMP() WHERE id=?")
        .bind(&user_id)
        .execute(&mut *transaction)
        .await
    {
        tracing::error!(?error, "email verification update failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    }
    if let Err(error) = sqlx::query(
        "UPDATE email_verifications SET used=1 WHERE token_hash=?",
    )
    .bind(hash_token(&query.token))
    .execute(&mut *transaction)
    .await
    {
        tracing::error!(?error, "email verification token update failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    }
    if let Err(error) = transaction.commit().await {
        tracing::error!(?error, "email verification commit failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    }
    Json(serde_json::json!({"verified": true})).into_response()
}

async fn resend_verification(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(input): Json<ResendVerificationRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let identifier = input.identifier.trim();
    if identifier.is_empty() || identifier.len() > 254 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_identifier");
    }
    let rate_key = format!(
        "verification-resend:{}:{}",
        client_key(&headers, &state.auth, peer),
        identifier.to_ascii_lowercase()
    );
    if let Some(retry_after) = state
        .auth_rate_limiter
        .check(&rate_key, 3, Duration::from_secs(3600))
    {
        return rate_limit_response(retry_after);
    }

    let user = sqlx::query_as::<_, (String, String, String, Option<String>)>(
        "SELECT id,email,display_name,CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE (username=? OR email=?) AND is_active=1 LIMIT 1",
    )
    .bind(identifier)
    .bind(identifier)
    .fetch_optional(pool)
    .await;
    if let Ok(Some((user_id, email, display_name, verified_at))) = user {
        if verified_at.is_none() {
            let token = new_token();
            let _ = sqlx::query("DELETE FROM email_verifications WHERE user_id=? AND used=0")
                .bind(&user_id)
                .execute(pool)
                .await;
            if let Err(error) = sqlx::query(
                "INSERT INTO email_verifications (id,user_id,token_hash,expires_at,used)
                 VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(), INTERVAL 24 HOUR),0)",
            )
            .bind(Uuid::new_v4().to_string())
            .bind(&user_id)
            .bind(hash_token(&token))
            .execute(pool)
            .await
            {
                tracing::warn!(?error, user_id = %user_id, "unable to store resent verification token");
            } else if let Err(error) =
                send_verification_email(&state, &email, &display_name, &token).await
            {
                tracing::error!(?error, user_id = %user_id, "resent verification email delivery failed");
            }
        }
    }
    (StatusCode::ACCEPTED, Json(serde_json::json!({"sent": true}))).into_response()
}

async fn request_password_reset(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(input): Json<PasswordResetRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let identifier = input.identifier.trim();
    if identifier.is_empty() || identifier.len() > 254 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_identifier");
    }
    let rate_key = format!(
        "password-reset:{}:{}",
        client_key(&headers, &state.auth, peer),
        identifier.to_ascii_lowercase()
    );
    if let Some(retry_after) = state
        .auth_rate_limiter
        .check(&rate_key, 3, Duration::from_secs(3600))
    {
        return rate_limit_response(retry_after);
    }

    let user = sqlx::query_as::<_, (String, String, String, Option<String>)>(
        "SELECT id,email,display_name,CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE (username=? OR email=?) AND is_active=1 LIMIT 1",
    )
    .bind(identifier)
    .bind(identifier)
    .fetch_optional(pool)
    .await;
    if let Ok(Some((user_id, email, display_name, Some(_)))) = user {
        let token = new_token();
        let _ = sqlx::query("DELETE FROM password_resets WHERE user_id=? AND used=0")
            .bind(&user_id)
            .execute(pool)
            .await;
        if let Err(error) = sqlx::query(
            "INSERT INTO password_resets (id,user_id,token_hash,expires_at,used)
             VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(), INTERVAL 1 HOUR),0)",
        )
        .bind(Uuid::new_v4().to_string())
        .bind(&user_id)
        .bind(hash_token(&token))
        .execute(pool)
        .await
        {
            tracing::warn!(?error, user_id = %user_id, "unable to store password reset token");
        } else if let Err(error) = send_password_reset_email(&state, &email, &display_name, &token).await {
            tracing::error!(?error, user_id = %user_id, "password reset email delivery failed");
        }
    }
    (StatusCode::ACCEPTED, Json(serde_json::json!({"sent": true}))).into_response()
}

async fn confirm_password_reset(
    State(state): State<AppState>,
    Json(input): Json<PasswordResetConfirm>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    if input.token.len() < 32 || input.token.len() > 128 || input.password.len() < 8 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_password_reset");
    }
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => {
            tracing::error!(?error, "password reset transaction failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    };
    let row = sqlx::query_as::<_, (String, String)>(
        "SELECT id,user_id FROM password_resets
         WHERE token_hash=? AND used=0 AND expires_at > UTC_TIMESTAMP()
         FOR UPDATE",
    )
    .bind(hash_token(&input.token))
    .fetch_optional(&mut *transaction)
    .await;
    let Some((reset_id, user_id)) = (match row {
        Ok(row) => row,
        Err(error) => {
            tracing::error!(?error, "password reset lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    }) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_password_reset");
    };
    let password_hash = match tokio::task::spawn_blocking(move || hash(input.password, DEFAULT_COST)).await {
        Ok(Ok(value)) => value,
        _ => return error_response(StatusCode::SERVICE_UNAVAILABLE, "password_reset_unavailable"),
    };
    if let Err(error) = sqlx::query("UPDATE users SET password_hash=?,email_verified_at=COALESCE(email_verified_at,UTC_TIMESTAMP()) WHERE id=?")
        .bind(password_hash)
        .bind(&user_id)
        .execute(&mut *transaction)
        .await
    {
        tracing::error!(?error, "password reset update failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "password_reset_unavailable");
    }
    let _ = sqlx::query("UPDATE password_resets SET used=1 WHERE id=?")
        .bind(&reset_id)
        .execute(&mut *transaction)
        .await;
    let _ = sqlx::query("DELETE FROM rust_sessions WHERE user_id=?")
        .bind(&user_id)
        .execute(&mut *transaction)
        .await;
    let _ = sqlx::query("DELETE FROM vesper_sessions WHERE user_id=?")
        .bind(&user_id)
        .execute(&mut *transaction)
        .await;
    let _ = sqlx::query("DELETE FROM sessions WHERE user_id=?")
        .bind(&user_id)
        .execute(&mut *transaction)
        .await;
    if let Err(error) = transaction.commit().await {
        tracing::error!(?error, "password reset commit failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "password_reset_unavailable");
    }
    Json(serde_json::json!({"reset": true})).into_response()
}

async fn send_verification_email(
    state: &AppState,
    email: &str,
    display_name: &str,
    token: &str,
) -> Result<(), String> {
    let url = state
        .mail
        .verification_url(token)
        .ok_or_else(|| "ASHAT_HUB_PUBLIC_URL is not configured".to_owned())?;
    let body = format!(
        "Hello {display_name},\n\nVerify your AGP Studios account by opening this link:\n{url}\n\nThis link expires in 24 hours. If you did not create this account, you can ignore this message.\n"
    );
    crate::mail::send_text(&state.mail, email, "Verify your AGP Studios account", &body).await
}

async fn send_password_reset_email(
    state: &AppState,
    email: &str,
    display_name: &str,
    token: &str,
) -> Result<(), String> {
    let url = state
        .mail
        .password_reset_url(token)
        .ok_or_else(|| "ASHAT_HUB_PUBLIC_URL is not configured".to_owned())?;
    let body = format!(
        "Hello {display_name},\n\nReset your AGP Studios password by opening this link:\n{url}\n\nThis link expires in 1 hour. If you did not request a reset, you can ignore this message.\n"
    );
    crate::mail::send_text(&state.mail, email, "Reset your AGP Studios password", &body).await
}


async fn session(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let current = match current_auth(pool, &state, &headers).await {
        Ok(current) => current,
        Err(error) => {
            tracing::error!(?error, "session lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    };

    let Some(current) = current else {
        return Json(AuthResponse {
            authenticated: false,
            user: None,
            csrf: None,
        })
        .into_response();
    };

    let csrf_cookie_name = &state.auth.csrf_cookie_name;
    let csrf = cookie_value(&headers, csrf_cookie_name).unwrap_or_else(new_token);
    let mut response = Json(AuthResponse {
        authenticated: true,
        user: Some(current.user),
        csrf: Some(csrf.clone()),
    })
    .into_response();

    if cookie_value(&headers, csrf_cookie_name).is_none() {
        if let SessionKind::Rust { .. } = current.kind {
            // The token is tied to the Rust session when a new CSRF cookie is issued.
            if let Err(error) = update_csrf(pool, &headers, &state, &csrf).await {
                tracing::warn!(?error, "unable to initialize Rust CSRF token");
            }
        }
        append_cookie(
            &mut response,
            csrf_cookie_name,
            &csrf,
            0,
            false,
            state.auth.secure_cookie,
        );
    }
    response
}

async fn me(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    match current_auth(pool, &state, &headers).await {
        Ok(Some(current)) => Json(serde_json::json!({
            "authenticated": true,
            "user": current.user,
        }))
        .into_response(),
        Ok(None) => error_response(StatusCode::UNAUTHORIZED, "unauthenticated"),
        Err(error) => {
            tracing::error!(?error, "authenticated user lookup failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable")
        }
    }
}

async fn account(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    match current_auth(pool, &state, &headers).await {
        Ok(Some(current)) => Json(serde_json::json!({"user": current.user})).into_response(),
        Ok(None) => error_response(StatusCode::UNAUTHORIZED, "unauthenticated"),
        Err(error) => {
            tracing::error!(?error, "account lookup failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable")
        }
    }
}

async fn update_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<ProfileUpdate>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(current) = current_auth(pool, &state, &headers).await.ok().flatten() else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    if !csrf_is_valid(&headers, &state, &current.kind) {
        return error_response(StatusCode::FORBIDDEN, "csrf_failed");
    }
    let username = input.username.unwrap_or(current.user.username.clone());
    let tag_name = input.tag_name.or(current.user.tag_name.clone());
    let display_name = input.display_name.unwrap_or(current.user.display_name.clone());
    let email = input.email.unwrap_or(current.user.email.clone());
    let discord_tag = input.discord_tag.or(current.user.discord_tag.clone());
    let location = input.location.or(current.user.location.clone());
    let interests = input.interests.or(current.user.interests.clone());
    if username.trim().is_empty()
        || username.len() > 80
        || display_name.trim().is_empty()
        || display_name.chars().count() > 200
        || email.len() > 255
        || !email.contains('@')
        || tag_name.as_ref().is_some_and(|v| v.chars().count() > 120)
        || discord_tag.as_ref().is_some_and(|v| v.chars().count() > 120)
        || location.as_ref().is_some_and(|v| v.chars().count() > 200)
        || interests.as_ref().is_some_and(|v| v.chars().count() > 500)
    {
        return error_response(StatusCode::BAD_REQUEST, "invalid_profile");
    }
    let duplicate = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM users WHERE (email=? OR username=?) AND id<>?")
        .bind(&email).bind(&username).bind(&current.user.id).fetch_one(pool).await.unwrap_or(1);
    if duplicate != 0 {
        return error_response(StatusCode::CONFLICT, "username_or_email_taken");
    }
    if let Err(error) = sqlx::query("UPDATE users SET username=?,tag_name=?,display_name=?,email=?,discord_tag=?,location=?,interests=?,updated_at=UTC_TIMESTAMP() WHERE id=?")
            .bind(username.trim()).bind(tag_name.as_deref()).bind(display_name.trim()).bind(email.trim())
            .bind(discord_tag.as_deref()).bind(location.as_deref()).bind(interests.as_deref()).bind(&current.user.id)
            .execute(pool)
            .await
    {
        tracing::error!(?error, "profile update failed");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "profile_unavailable");
    }
    match sqlx::query_as::<_, UserWithPassword>(
        "SELECT id,username,email,password_hash,display_name,tag_name,discord_tag,location,interests,role,is_active,
                CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users WHERE id=?",
    )
    .bind(&current.user.id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(user)) => Json(serde_json::json!({"user": public_user(&user)})).into_response(),
        Ok(None) => error_response(StatusCode::NOT_FOUND, "user_not_found"),
        Err(error) => {
            tracing::error!(?error, "updated profile lookup failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "profile_unavailable")
        }
    }
}

async fn logout(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };

    let current = match current_auth(pool, &state, &headers).await {
        Ok(current) => current,
        Err(error) => {
            tracing::error!(?error, "logout session lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
        }
    };

    if let Some(current) = current {
        if !csrf_is_valid(&headers, &state, &current.kind) {
            return error_response(StatusCode::FORBIDDEN, "csrf_failed");
        }
        tracing::info!(
            event = "auth.logout",
            request_id = %request_id(&headers),
            user_id = %current.user.id
        );
        match current.kind {
            SessionKind::Rust { .. } => {
                if let Some(token) = cookie_value(&headers, &state.auth.cookie_name) {
                    let _ = sqlx::query("DELETE FROM rust_sessions WHERE session_hash = ?")
                        .bind(hash_token(&token))
                        .execute(pool)
                        .await;
                }
            }
            SessionKind::Legacy => {
                if let Some(token) = cookie_value(&headers, &state.auth.legacy_cookie_name) {
                    let _ = sqlx::query("DELETE FROM sessions WHERE id = ?")
                        .bind(token)
                        .execute(pool)
                        .await;
                }
            }
        }
    }

    let mut response = Json(OkResponse { ok: true }).into_response();
    clear_cookie(
        &mut response,
        &state.auth.cookie_name,
        state.auth.secure_cookie,
    );
    clear_cookie(
        &mut response,
        &state.auth.csrf_cookie_name,
        state.auth.secure_cookie,
    );
    response
}

pub(crate) async fn enforce_csrf(
    state: &AppState,
    method: &Method,
    path: &str,
    headers: &HeaderMap,
) -> Result<(), Response> {
    if (!matches!(method, &Method::POST | &Method::PUT | &Method::DELETE)
        || matches!(
            path,
            "/api/auth/login"
                | "/api/auth/register"
                | "/api/auth/verify-email/resend"
                | "/api/auth/password-reset/request"
                | "/api/auth/password-reset/confirm"
                | "/api/sso/verify-session"
            | "/api/v1/auth/login" | "/api/v1/auth/status" | "/api/v1/auth/logout"
            | "/api/v1/models/announce"
            // Icarus device flow: the CLI has no session cookie when it
            // starts device auth or validates a token by body, and the
            // device login page POSTs a username/password form.
            | "/api/icarus/auth/device" | "/api/icarus/auth/validate"
            | "/api/icarus/auth/login"
            // OIDC token endpoint: called server-to-server by Paws & Parcels.
            | "/api/oauth/token" | "/api/oauth/authorize"
            // Admin endpoints still require the AdminUser extractor; this action
            // is exempt because the admin session may be legacy and lack CSRF.
            | "/api/admin/github/push" | "/api/admin/github/galileo-push"
            | "/api/admin/system/update" | "/api/admin/galileo/update"
            | "/api/admin/omega/servers" | "/api/admin/omega/status" | "/api/admin/omega/launch"
            | "/api/admin/users/role" | "/api/admin/users/status" | "/api/admin/users/ban"
            | "/api/admin/users" | "/api/admin/telemetry/refresh" | "/api/admin/telemetry/clear"
        )) && !path.starts_with("/api/admin/users/")
    {
        return Ok(());
    }
    let Some(pool) = state.db.as_ref() else {
        return Err(error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "auth_unavailable",
        ));
    };
    let current = match current_auth(pool, state, headers).await {
        Ok(Some(current)) => current,
        Ok(None) => {
            state.metrics.record_auth_failure();
            return Err(error_response(StatusCode::UNAUTHORIZED, "unauthenticated"));
        }
        Err(error) => {
            tracing::error!(?error, "CSRF session lookup failed");
            return Err(error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "auth_unavailable",
            ));
        }
    };
    if !csrf_is_valid(headers, state, &current.kind) {
        return Err(error_response(StatusCode::FORBIDDEN, "csrf_failed"));
    }
    Ok(())
}

pub(crate) async fn authenticated_user(
    pool: &MySqlPool,
    state: &AppState,
    headers: &HeaderMap,
) -> Result<Option<PublicUser>, sqlx::Error> {
    Ok(current_auth(pool, state, headers)
        .await?
        .map(|current| current.user))
}

async fn current_auth(
    pool: &MySqlPool,
    state: &AppState,
    headers: &HeaderMap,
) -> Result<Option<CurrentAuth>, sqlx::Error> {
    if let Some(token) = cookie_value(headers, &state.auth.cookie_name) {
        let row = sqlx::query_as::<_, RustSessionUser>(
            "SELECT u.id, u.username, u.email, u.display_name, u.tag_name,
                    u.discord_tag, u.location, u.interests, u.role,
                    CAST(u.email_verified_at AS CHAR) AS email_verified_at, s.csrf_hash
             FROM rust_sessions s
             INNER JOIN users u ON u.id = s.user_id
             WHERE s.session_hash = ? AND s.expires_at > UTC_TIMESTAMP() AND u.is_active = 1
             LIMIT 1",
        )
        .bind(hash_token(&token))
        .fetch_optional(pool)
        .await?;
        if let Some(row) = row {
            if state.auth.email_verification_enabled && row.email_verified_at.is_none() {
                return Ok(None);
            }
            let _ = sqlx::query(
                "UPDATE rust_sessions SET last_seen = UTC_TIMESTAMP() WHERE session_hash = ?",
            )
            .bind(hash_token(&token))
            .execute(pool)
            .await;
            return Ok(Some(CurrentAuth {
                user: PublicUser {
                    id: row.id,
                    username: row.username,
                    email: row.email,
                    display_name: row.display_name,
                    tag_name: row.tag_name,
                    discord_tag: row.discord_tag,
                    location: row.location,
                    interests: row.interests,
                    role: row.role,
                },
                kind: SessionKind::Rust {
                    csrf_hash: row.csrf_hash,
                },
            }));
        }
    }

    // Existing PHP logins remain readable during the migration. PHP stores
    // the session ID in the shared sessions table, so Rust can authenticate
    // the same browser without reading PHP's serialized session files.
    if let Some(token) = cookie_value(headers, &state.auth.legacy_cookie_name) {
        let row = sqlx::query_as::<_, LegacySessionUser>(
            "SELECT u.id, u.username, u.email, u.display_name, u.tag_name,
                    u.discord_tag, u.location, u.interests, u.role,
                    CAST(u.email_verified_at AS CHAR) AS email_verified_at
             FROM sessions s
             INNER JOIN users u ON u.id = s.user_id
             WHERE s.id = ? AND s.expires_at > UTC_TIMESTAMP() AND u.is_active = 1
             LIMIT 1",
        )
        .bind(token)
        .fetch_optional(pool)
        .await?;
        if let Some(row) = row {
            if state.auth.email_verification_enabled && row.email_verified_at.is_none() {
                return Ok(None);
            }
            return Ok(Some(CurrentAuth {
                user: PublicUser {
                    id: row.id,
                    username: row.username,
                    email: row.email,
                    display_name: row.display_name,
                    tag_name: row.tag_name,
                    discord_tag: row.discord_tag,
                    location: row.location,
                    interests: row.interests,
                    role: row.role,
                },
                kind: SessionKind::Legacy,
            }));
        }
    }

    Ok(None)
}

async fn update_csrf(
    pool: &MySqlPool,
    headers: &HeaderMap,
    state: &AppState,
    csrf: &str,
) -> Result<(), sqlx::Error> {
    let Some(session) = cookie_value(headers, &state.auth.cookie_name) else {
        return Ok(());
    };
    sqlx::query("UPDATE rust_sessions SET csrf_hash = ? WHERE session_hash = ?")
        .bind(hash_token(csrf))
        .bind(hash_token(&session))
        .execute(pool)
        .await
        .map(|_| ())
}

fn csrf_is_valid(headers: &HeaderMap, state: &AppState, kind: &SessionKind) -> bool {
    let Some(header_token) = headers
        .get("x-csrf-token")
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Some(cookie_token) = cookie_value(headers, &state.auth.csrf_cookie_name) else {
        return false;
    };
    if header_token.is_empty() || header_token != cookie_token {
        return false;
    }
    match kind {
        SessionKind::Rust { csrf_hash } => {
            constant_time_equal(&hash_token(header_token), csrf_hash)
        }
        // Legacy PHP sessions do not have a Rust CSRF hash. The double-submit
        // cookie is still required for Rust logout while the bridge is active.
        SessionKind::Legacy => true,
    }
}

fn public_user(user: &UserWithPassword) -> PublicUser {
    PublicUser {
        id: user.id.clone(),
        username: user.username.clone(),
        email: user.email.clone(),
        display_name: user.display_name.clone(),
        tag_name: user.tag_name.clone(),
        discord_tag: user.discord_tag.clone(),
        location: user.location.clone(),
        interests: user.interests.clone(),
        role: user.role.clone(),
    }
}

pub(crate) fn new_token() -> String {
    let suffix: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    format!("{}{}", Uuid::new_v4().simple(), suffix)
}

pub(crate) fn hash_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn valid_email(value: &str) -> bool {
    value.len() <= 255
        && value.contains('@')
        && !value.chars().any(|character| matches!(character, ' ' | '\r' | '\n'))
        && value.split('@').count() == 2
        && value.split('@').all(|part| !part.is_empty())
}

fn constant_time_equal(left: &str, right: &str) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.as_bytes()
        .iter()
        .zip(right.as_bytes())
        .fold(0u8, |acc, (a, b)| acc | (a ^ b))
        == 0
}

pub(crate) fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let raw = headers.get(header::COOKIE)?.to_str().ok()?;
    raw.split(';').find_map(|part| {
        let (key, value) = part.trim().split_once('=')?;
        (key == name).then(|| value.to_owned())
    })
}

pub(crate) fn with_auth_cookies(
    mut response: Response,
    state: &AppState,
    session: &str,
    csrf: &str,
    lifetime: i64,
) -> Response {
    append_cookie(
        &mut response,
        &state.auth.cookie_name,
        session,
        lifetime,
        true,
        state.auth.secure_cookie,
    );
    append_cookie(
        &mut response,
        &state.auth.csrf_cookie_name,
        csrf,
        0,
        false,
        state.auth.secure_cookie,
    );
    response
}

fn append_cookie(
    response: &mut Response,
    name: &str,
    value: &str,
    max_age: i64,
    http_only: bool,
    secure: bool,
) {
    let mut cookie = format!("{}={}; Domain=agpstudios.org; Path=/; SameSite=Lax", name, value);
    if max_age != 0 {
        cookie.push_str(&format!("; Max-Age={}", max_age));
    }
    if http_only {
        cookie.push_str("; HttpOnly");
    }
    if secure {
        cookie.push_str("; Secure");
    }
    if let Ok(header_value) = HeaderValue::from_str(&cookie) {
        response
            .headers_mut()
            .append(header::SET_COOKIE, header_value);
    }
}

fn clear_cookie(response: &mut Response, name: &str, secure: bool) {
    append_cookie(response, name, "", -1, true, secure);
}

fn request_id(headers: &HeaderMap) -> &str {
    headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("unknown")
}

fn client_key(headers: &HeaderMap, auth: &crate::AuthConfig, peer: SocketAddr) -> String {
    if auth.trust_proxy_headers {
        for name in ["x-real-ip", "x-forwarded-for"] {
            if let Some(value) = headers.get(name).and_then(|value| value.to_str().ok()) {
                let candidate = value.split(',').next().unwrap_or_default().trim();
                if !candidate.is_empty() && candidate.len() <= 64 && candidate.is_ascii() {
                    return candidate.to_owned();
                }
            }
        }
    }
    peer.ip().to_string()
}

#[cfg(test)]
mod tests {
    use super::{constant_time_equal, hash_token, new_token};

    #[test]
    fn tokens_are_random_and_hashes_are_stable() {
        let first = new_token();
        let second = new_token();
        assert_ne!(first, second);
        assert_eq!(hash_token(&first), hash_token(&first));
        assert_ne!(hash_token(&first), hash_token(&second));
    }

    #[test]
    fn constant_time_comparison_requires_equal_values() {
        assert!(constant_time_equal("abc", "abc"));
        assert!(!constant_time_equal("abc", "abd"));
        assert!(!constant_time_equal("abc", "abcd"));
    }
}
