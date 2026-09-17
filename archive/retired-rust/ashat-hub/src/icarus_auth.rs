use axum::{
    extract::{Form, Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use rand::{distributions::Alphanumeric, Rng};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

use uuid::Uuid;

use crate::{response::error_response, AppState};

// ── Request / Response types ──────────────────────────────────────

#[derive(Debug, Serialize)]
struct DeviceAuthResponse {
    device_code: String,
    user_code: String,
    verification_url: String,
    expires_in: u64,
}

#[derive(Debug, Serialize)]
struct DevicePollResponse {
    status: String,
    session_token: Option<String>,
    csrf_token: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct LoginQuery {
    pub(crate) code: String,
}

#[derive(Debug, Deserialize)]
struct ValidateRequest {
    session_token: String,
}

#[derive(Debug, Deserialize)]
struct LoginForm {
    username: String,
    password: String,
    code: String,
}

#[derive(Debug, FromRow)]
struct UserWithPassword {
    id: String,
    password_hash: String,
    email_verified_at: Option<String>,
}

#[derive(Debug, Serialize)]
struct ValidateResponse {
    valid: bool,
    user_id: Option<String>,
}

// ── Routes ────────────────────────────────────────────────────────

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/icarus/auth/device", post(start_device_auth))
        .route(
            "/api/icarus/auth/device/:code",
            get(poll_device_auth),
        )
        .route("/api/icarus/auth/login", get(login_page).post(login_submit))
        .route("/api/icarus/auth/validate", post(validate_session))
}

// ── Helpers ───────────────────────────────────────────────────────

/// Generate a short user code like `ABCD-1234`.
fn generate_user_code() -> String {
    let mut rng = rand::thread_rng();
    let letters: String = (0..4)
        .map(|_| rng.sample(Alphanumeric) as char)
        .map(|c| c.to_ascii_uppercase())
        .collect();
    let digits: String = (0..4)
        .map(|_| rng.sample(Alphanumeric) as char)
        .map(|c| c.to_ascii_uppercase())
        .collect();
    format!("{}-{}", letters, digits)
}

fn html_response(html: String) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        html,
    )
        .into_response()
}

// ── POST /api/icarus/auth/device ──────────────────────────────────

async fn start_device_auth(State(state): State<AppState>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_not_configured");
    };

    let device_code = Uuid::new_v4().to_string().replace('-', "");
    let user_code = generate_user_code();
    let expires_at = chrono::Utc::now() + chrono::Duration::minutes(10);

    if let Err(error) = sqlx::query(
        "INSERT INTO icarus_devices (device_code, user_code, status, created_at, expires_at)
         VALUES (?, ?, 'pending', UTC_TIMESTAMP(), ?)",
    )
    .bind(&device_code)
    .bind(&user_code)
    .bind(expires_at.naive_utc())
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Failed to create Icarus device auth");
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "device_auth_failed");
    }

    let base_url = state
        .hub_public_url
        .as_deref()
        .unwrap_or("http://localhost:3101");
    let verification_url = format!(
        "{}/api/icarus/auth/login?code={}",
        base_url, user_code
    );

    Json(DeviceAuthResponse {
        device_code,
        user_code,
        verification_url,
        expires_in: 600,
    })
    .into_response()
}

// ── GET /api/icarus/auth/device/{code} ────────────────────────────

async fn poll_device_auth(
    State(state): State<AppState>,
    Path(code): Path<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_not_configured");
    };

    let row = sqlx::query_as::<_, (String, Option<String>, Option<String>)>(
        "SELECT status, session_token, csrf_token
         FROM icarus_devices
         WHERE device_code = ? AND expires_at > UTC_TIMESTAMP()",
    )
    .bind(&code)
    .fetch_optional(pool)
    .await;

    match row {
        Ok(Some((status, session_token, csrf_token))) => match status.as_str() {
            "authorized" => Json(DevicePollResponse {
                status: "authorized".to_owned(),
                session_token,
                csrf_token,
            })
            .into_response(),
            "expired" | "denied" => Json(DevicePollResponse {
                status,
                session_token: None,
                csrf_token: None,
            })
            .into_response(),
            _ => Json(DevicePollResponse {
                status: "pending".to_owned(),
                session_token: None,
                csrf_token: None,
            })
            .into_response(),
        },
        Ok(None) => error_response(StatusCode::NOT_FOUND, "device_code_not_found"),
        Err(error) => {
            tracing::error!(?error, "Icarus device poll failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "database_error")
        }
    }
}

// ── GET /api/icarus/auth/login?code={user_code} ───────────────────

async fn login_page(
    State(state): State<AppState>,
    Query(params): Query<LoginQuery>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return html_response(error_page("Service unavailable"));
    };

    // Validate the user_code exists and is still pending.
    let row = sqlx::query_as::<_, (String, String)>(
        "SELECT device_code, status
         FROM icarus_devices
         WHERE user_code = ? AND expires_at > UTC_TIMESTAMP()",
    )
    .bind(&params.code)
    .fetch_optional(pool)
    .await;

    let (_device_code, status) = match row {
        Ok(Some(row)) => row,
        Ok(None) => return html_response(error_page("Invalid or expired code")),
        Err(_) => return html_response(error_page("Service unavailable")),
    };

    if status != "pending" {
        return html_response(error_page("This code has already been used"));
    }

    html_response(login_html(&params.code))
}

// ── POST /api/icarus/auth/login ────────────────────────────────────

async fn login_submit(State(state): State<AppState>, Form(form): Form<LoginForm>) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return html_response(error_page("Service unavailable"));
    };

    // Validate the user_code exists and is still pending.
    let row = sqlx::query_as::<_, (String, String)>(
        "SELECT device_code, status
         FROM icarus_devices
         WHERE user_code = ? AND expires_at > UTC_TIMESTAMP()",
    )
    .bind(&form.code)
    .fetch_optional(pool)
    .await;

    let (device_code, status) = match row {
        Ok(Some(row)) => row,
        Ok(None) => return html_response(error_page("Invalid or expired code")),
        Err(_) => return html_response(error_page("Service unavailable")),
    };

    if status != "pending" {
        return html_response(error_page("This code has already been used"));
    }

    let identifier = form.username.trim();
    if identifier.is_empty() || form.password.is_empty() {
        return html_response(login_error_page(&form.code, "Enter your username and password."));
    }

    let user = match sqlx::query_as::<_, UserWithPassword>(
        "SELECT id, password_hash, CAST(email_verified_at AS CHAR) AS email_verified_at
         FROM users
         WHERE (username = ? OR email = ?) AND is_active = 1
         LIMIT 1",
    )
    .bind(identifier)
    .bind(identifier)
    .fetch_optional(pool)
    .await
    {
        Ok(user) => user,
        Err(error) => {
            tracing::error!(?error, "Icarus: user lookup failed");
            return html_response(error_page("Service unavailable"));
        }
    };

    let Some(user) = user else {
        return html_response(login_error_page(
            &form.code,
            "Invalid username or password.",
        ));
    };

    // PHP password_hash(PASSWORD_BCRYPT) emits $2y$; Rust bcrypt uses $2b$.
    let password_hash = user.password_hash.replace("$2y$", "$2b$");
    let password = form.password.clone();
    let valid = tokio::task::spawn_blocking(move || {
        bcrypt::verify(password, &password_hash).unwrap_or(false)
    })
    .await
    .unwrap_or(false);
    if !valid {
        return html_response(login_error_page(
            &form.code,
            "Invalid username or password.",
        ));
    }
    if state.auth.email_verification_enabled && user.email_verified_at.is_none() {
        return html_response(login_error_page(
            &form.code,
            "Verify your email address before signing in.",
        ));
    }

    // Create a Rust session the CLI can reuse for authenticated API calls.
    let session_token = crate::auth::new_token();
    let csrf_token = crate::auth::new_token();
    let session_hash = crate::auth::hash_token(&session_token);
    let csrf_hash = crate::auth::hash_token(&csrf_token);
    let lifetime = state.auth.session_lifetime_seconds;

    if let Err(error) = sqlx::query(
        "INSERT INTO rust_sessions
            (session_hash, user_id, csrf_hash, ip, user_agent, created_at, last_seen, expires_at)
         VALUES (?, ?, ?, 'icarus-cli', 'icarus-cli',
                 UTC_TIMESTAMP(), UTC_TIMESTAMP(),
                 DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND))",
    )
    .bind(&session_hash)
    .bind(&user.id)
    .bind(&csrf_hash)
    .bind(lifetime)
    .execute(pool)
    .await
    {
        tracing::error!(?error, "Icarus: session creation failed");
        return html_response(error_page("Failed to create session"));
    }

    // Mark the device code as authorized so the CLI poll returns the tokens.
    let _ = sqlx::query(
        "UPDATE icarus_devices
         SET status = 'authorized', session_token = ?, csrf_token = ?, user_id = ?
         WHERE device_code = ?",
    )
    .bind(&session_token)
    .bind(&csrf_token)
    .bind(&user.id)
    .bind(&device_code)
    .execute(pool)
    .await;

    html_response(success_page())
}

// ── POST /api/icarus/auth/validate ────────────────────────────────

async fn validate_session(
    State(state): State<AppState>,
    Json(input): Json<ValidateRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_not_configured");
    };

    let session_hash = crate::auth::hash_token(&input.session_token);

    let row = sqlx::query_as::<_, (String,)>(
        "SELECT u.id
         FROM rust_sessions s
         INNER JOIN users u ON u.id = s.user_id
         WHERE s.session_hash = ? AND s.expires_at > UTC_TIMESTAMP() AND u.is_active = 1
         LIMIT 1",
    )
    .bind(&session_hash)
    .fetch_optional(pool)
    .await;

    match row {
        Ok(Some((user_id,))) => Json(ValidateResponse {
            valid: true,
            user_id: Some(user_id),
        })
        .into_response(),
        Ok(None) => Json(ValidateResponse {
            valid: false,
            user_id: None,
        })
        .into_response(),
        Err(error) => {
            tracing::error!(?error, "Icarus: session validation failed");
            error_response(StatusCode::SERVICE_UNAVAILABLE, "validation_failed")
        }
    }
}

// ── HTML pages ────────────────────────────────────────────────────

fn login_html(code: &str) -> String {
    format!(
        r##"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Icarus — Sign in to AGP Studios</title>
  <style>
    :root {{ --bg: #0a0a0a; --surface: #121212; --text: #f3f0e8; --muted: #beb7aa;
             --accent: #c9a24a; --line: rgba(201,162,74,.22); }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ min-height: 100vh; display: grid; place-items: center; background: var(--bg);
            color: var(--text); font-family: 'Inter', system-ui, sans-serif; padding: 24px; }}
    .card {{ width: min(100%, 420px); padding: 32px; background: var(--surface);
             border: 1px solid var(--line); border-radius: 6px; text-align: center; }}
    .eyebrow {{ display: block; margin-bottom: 10px; color: var(--accent);
                font: 600 11px 'JetBrains Mono', monospace; letter-spacing: .12em;
                text-transform: uppercase; }}
    h1 {{ font-size: 32px; margin-bottom: 8px; }}
    .code {{ display: inline-block; margin: 16px 0; padding: 10px 20px;
             background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
             font: 600 24px 'JetBrains Mono', monospace; letter-spacing: .08em;
             color: var(--accent); }}
    p {{ color: var(--muted); font-size: 14px; line-height: 1.6; margin-bottom: 20px; }}
    form {{ text-align: left; margin-top: 8px; }}
    label {{ display: block; color: var(--muted); font-size: 13px; margin: 14px 0 6px; }}
    input {{ width: 100%; padding: 10px 12px; background: var(--bg); color: var(--text);
             border: 1px solid var(--line); border-radius: 4px; font-size: 14px;
             font-family: inherit; outline: none; }}
    input:focus {{ border-color: var(--accent); }}
    .submit {{ margin-top: 20px; width: 100%; padding: 11px 12px; background: var(--accent);
               color: #0a0a0a; border: none; border-radius: 4px; font-size: 15px;
               font-weight: 700; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="eyebrow">Icarus CLI</span>
    <h1>Sign in to AGP Studios</h1>
    <p>Enter this code in your terminal to authenticate Icarus:</p>
    <div class="code">{}</div>
    <p>Then sign in with your AGP Studios account.</p>
    <form method="post" action="/api/icarus/auth/login">
      <input type="hidden" name="code" value="{}" />
      <label for="icarus-username">Username or email</label>
      <input id="icarus-username" name="username" type="text" autocomplete="username" required />
      <label for="icarus-password">Password</label>
      <input id="icarus-password" name="password" type="password" autocomplete="current-password" required />
      <button class="submit" type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"##,
        code, code
    )
}

fn login_error_page(code: &str, message: &str) -> String {
    format!(
        r##"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Icarus — Sign in to AGP Studios</title>
  <style>
    :root {{ --bg: #0a0a0a; --surface: #121212; --text: #f3f0e8; --muted: #beb7aa;
             --accent: #c9a24a; --line: rgba(201,162,74,.22); }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ min-height: 100vh; display: grid; place-items: center; background: var(--bg);
            color: var(--text); font-family: 'Inter', system-ui, sans-serif; padding: 24px; }}
    .card {{ width: min(100%, 420px); padding: 32px; background: var(--surface);
             border: 1px solid var(--line); border-radius: 6px; text-align: center; }}
    .eyebrow {{ display: block; margin-bottom: 10px; color: var(--accent);
                font: 600 11px 'JetBrains Mono', monospace; letter-spacing: .12em;
                text-transform: uppercase; }}
    h1 {{ font-size: 32px; margin-bottom: 8px; }}
    .code {{ display: inline-block; margin: 16px 0; padding: 10px 20px;
             background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
             font: 600 24px 'JetBrains Mono', monospace; letter-spacing: .08em;
             color: var(--accent); }}
    .error {{ margin: 10px 0 0; padding: 10px 12px; background: rgba(248,113,113,.12);
              border: 1px solid rgba(248,113,113,.35); border-radius: 4px;
              color: #fca5a5; font-size: 13px; text-align: left; }}
    form {{ text-align: left; margin-top: 8px; }}
    label {{ display: block; color: var(--muted); font-size: 13px; margin: 14px 0 6px; }}
    input {{ width: 100%; padding: 10px 12px; background: var(--bg); color: var(--text);
             border: 1px solid var(--line); border-radius: 4px; font-size: 14px;
             font-family: inherit; outline: none; }}
    input:focus {{ border-color: var(--accent); }}
    .submit {{ margin-top: 20px; width: 100%; padding: 11px 12px; background: var(--accent);
               color: #0a0a0a; border: none; border-radius: 4px; font-size: 15px;
               font-weight: 700; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="eyebrow">Icarus CLI</span>
    <h1>Sign in to AGP Studios</h1>
    <p>Enter this code in your terminal to authenticate Icarus:</p>
    <div class="code">{}</div>
    <p>Then sign in with your AGP Studios account.</p>
    <div class="error">{}</div>
    <form method="post" action="/api/icarus/auth/login">
      <input type="hidden" name="code" value="{}" />
      <label for="icarus-username">Username or email</label>
      <input id="icarus-username" name="username" type="text" autocomplete="username" required />
      <label for="icarus-password">Password</label>
      <input id="icarus-password" name="password" type="password" autocomplete="current-password" required />
      <button class="submit" type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"##,
        code, message, code
    )
}

fn success_page() -> String {
    r##"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Icarus — Authenticated</title>
  <style>
    :root { --bg: #0a0a0a; --surface: #121212; --text: #f3f0e8; --accent: #c9a24a;
            --line: rgba(201,162,74,.22); }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { min-height: 100vh; display: grid; place-items: center; background: var(--bg);
           color: var(--text); font-family: 'Inter', system-ui, sans-serif; padding: 24px; }
    .card { width: min(100%, 420px); padding: 32px; background: var(--surface);
             border: 1px solid var(--line); border-radius: 6px; text-align: center; }
    .eyebrow { display: block; margin-bottom: 10px; color: var(--accent);
               font: 600 11px 'JetBrains Mono', monospace; letter-spacing: .12em;
               text-transform: uppercase; }
    h1 { font-size: 28px; margin-bottom: 12px; }
    .check { font-size: 48px; margin-bottom: 16px; color: var(--accent); }
    p { color: #beb7aa; font-size: 14px; line-height: 1.6; }
  </style>
</head>
<body>
  <div class="card">
    <span class="eyebrow">Icarus CLI</span>
    <div class="check">&#x2713;</div>
    <h1>Authenticated</h1>
    <p>You can now return to your terminal. Icarus is ready to use.</p>
  </div>
</body>
</html>"##
    .to_owned()
}

fn error_page(message: &str) -> String {
    format!(
        r##"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Icarus — Error</title>
  <style>
    :root {{ --bg: #0a0a0a; --surface: #121212; --text: #f3f0e8; --accent: #c9a24a;
             --line: rgba(201,162,74,.22); }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ min-height: 100vh; display: grid; place-items: center; background: var(--bg);
            color: var(--text); font-family: 'Inter', system-ui, sans-serif; padding: 24px; }}
    .card {{ width: min(100%, 420px); padding: 32px; background: var(--surface);
             border: 1px solid var(--line); border-radius: 6px; text-align: center; }}
    .eyebrow {{ display: block; margin-bottom: 10px; color: var(--accent);
                font: 600 11px 'JetBrains Mono', monospace; letter-spacing: .12em;
                text-transform: uppercase; }}
    h1 {{ font-size: 28px; margin-bottom: 12px; color: #f87171; }}
    p {{ color: #beb7aa; font-size: 14px; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="eyebrow">Icarus CLI</span>
    <h1>Error</h1>
    <p>{}</p>
  </div>
</body>
</html>"##,
        message
    )
}
