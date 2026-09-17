use axum::{response::{IntoResponse, Response}, routing::{get, post}, Json, Router};
use serde_json::Value;
use std::{env, time::Duration};
use tokio::{process::Command, time::timeout};
use crate::{auth::AdminUser, response::error_response, AppState};

pub(crate) fn routes() -> Router<AppState> {
    Router::new().route("/api/admin/omega/servers", get(servers)).route("/api/admin/omega/status", get(status)).route("/api/admin/omega/launch", post(launch))
}

fn cfg(name: &str, default: &str) -> String { env::var(name).unwrap_or_else(|_| default.to_owned()) }

async fn ssh(args: &[&str], seconds: u64) -> Result<String, ()> {
    let key = cfg("ASHAT_OMEGA_SSH_KEY", "/var/oled/data/oraclehost_id_rsa");
    let host = cfg("ASHAT_OMEGA_SSH_HOST", "opc@129.213.94.124");
    let mut command = Command::new("ssh");
    command.args(["-i", &key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", &host]);
    command.args(args);
    let output = timeout(Duration::from_secs(seconds), command.output()).await.map_err(|_| ())?.map_err(|_| ())?;
    if output.status.success() { String::from_utf8(output.stdout).map_err(|_| ()) } else { Err(()) }
}

async fn status_inner() -> Result<Value, ()> {
    let body = ssh(&["curl", "-fsS", "http://127.0.0.1:8080/status"], 20).await?;
    serde_json::from_str(&body).map_err(|_| ())
}

async fn status(AdminUser(_admin): AdminUser) -> Response {
    match status_inner().await { Ok(value) => Json(value).into_response(), Err(()) => error_response(axum::http::StatusCode::BAD_GATEWAY, "omega_unavailable") }
}

async fn servers(AdminUser(_admin): AdminUser) -> Response {
    let status = status_inner().await.ok();
    Json(serde_json::json!({"servers":[{"id":"omega","label":"Omega","online":status.is_some(),"ready":status.is_some(),"workers":status.as_ref().and_then(|v| v.get("workers")),"max_workers":3}]})).into_response()
}

async fn launch(AdminUser(_admin): AdminUser) -> Response {
    let script = cfg("ASHAT_OMEGA_PROVISION_SCRIPT", "/var/oled/data/AshatHub/scripts/provision-omega.sh");
    let output = timeout(Duration::from_secs(900), Command::new("bash").arg(script).output()).await;
    match output {
        Ok(Ok(output)) if output.status.success() => match status_inner().await { Ok(status) => Json(serde_json::json!({"ok":true,"server":"omega","status":status})).into_response(), _ => error_response(axum::http::StatusCode::BAD_GATEWAY, "omega_not_ready") },
        _ => error_response(axum::http::StatusCode::BAD_GATEWAY, "omega_provision_failed"),
    }
}
