//! Single-key auth middleware: gates the `/v1/chat/completions` route.

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;
use std::sync::Arc;

use crate::handlers::AppState;

const AUTH_HEADER: &str = "X-Ashat-Key";

pub async fn require_ashat_key(
    State(state): State<Arc<AppState>>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let supplied = request
        .headers()
        .get(AUTH_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    if state.config.ash_key.is_empty() || supplied != state.config.ash_key {
        let body = Json(json!({
            "error": {
                "message": "Missing or invalid X-Ashat-Key",
                "type": "authentication_error"
            }
        }));
        return (StatusCode::UNAUTHORIZED, body).into_response();
    }

    next.run(request).await
}

/// Privileged-key middleware for admin routes (`POST /api/admin/update`).
/// Requires the dedicated `admin_key` (config `admin_key` or env
/// `ASHAT_ADMIN_KEY`) when configured; otherwise falls back to the shared
/// `ASHAT_KEY` so existing single-key deployments keep working.
pub async fn require_admin_key(
    State(state): State<Arc<AppState>>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let supplied = request
        .headers()
        .get(AUTH_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    let expected = state
        .config
        .admin_key
        .as_deref()
        .unwrap_or(&state.config.ash_key);
    if expected.is_empty() || supplied != expected {
        let body = Json(json!({
            "error": {
                "message": "Missing or invalid admin key",
                "type": "authentication_error"
            }
        }));
        return (StatusCode::UNAUTHORIZED, body).into_response();
    }
    next.run(request).await
}
