use axum::{
    body::{to_bytes, Body},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};

pub(crate) fn error_response(status: StatusCode, error: &'static str) -> Response {
    (status, Json(json!({"error": error}))).into_response()
}

pub(crate) fn rate_limit_response(retry_after: u64) -> Response {
    let mut response = error_response(StatusCode::TOO_MANY_REQUESTS, "rate_limited");
    if let Ok(value) = HeaderValue::from_str(&retry_after.to_string()) {
        response.headers_mut().insert("retry-after", value);
    }
    response
}

pub(crate) async fn normalize_error_response(response: Response, request_id: &str) -> Response {
    if !response.status().is_client_error() && !response.status().is_server_error() {
        return response;
    }

    let status = response.status();
    let headers = response.headers().clone();
    let bytes = match to_bytes(response.into_body(), 256 * 1024).await {
        Ok(bytes) => bytes,
        Err(_) => return error_response(status, "response_error"),
    };
    let code = serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|value| value.get("error").cloned())
        .and_then(|error| {
            error
                .as_str()
                .map(str::to_owned)
                .or_else(|| error.get("code").and_then(Value::as_str).map(str::to_owned))
        })
        .unwrap_or_else(|| status_code(status).to_owned());
    let body = json!({
        "error": {
            "code": code,
            "message": public_message(&code),
            "request_id": request_id,
        }
    });

    let mut response = Response::new(Body::from(body.to_string()));
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    response.headers_mut().remove(header::CONTENT_LENGTH);
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    response
}

fn status_code(status: StatusCode) -> &'static str {
    match status {
        StatusCode::BAD_REQUEST => "bad_request",
        StatusCode::UNAUTHORIZED => "unauthenticated",
        StatusCode::FORBIDDEN => "forbidden",
        StatusCode::NOT_FOUND => "not_found",
        StatusCode::REQUEST_TIMEOUT | StatusCode::GATEWAY_TIMEOUT => "request_timeout",
        StatusCode::TOO_MANY_REQUESTS => "rate_limited",
        StatusCode::PAYLOAD_TOO_LARGE => "payload_too_large",
        StatusCode::BAD_GATEWAY => "upstream_error",
        StatusCode::SERVICE_UNAVAILABLE => "service_unavailable",
        _ if status.is_client_error() => "client_error",
        _ => "server_error",
    }
}

fn public_message(code: &str) -> &'static str {
    match code {
        "unauthenticated" => "Authentication is required.",
        "forbidden" | "csrf_failed" => "This action is not permitted.",
        "rate_limited" => "Too many requests. Please try again later.",
        "request_timeout" => "The request took too long to complete.",
        "database_unavailable" | "auth_unavailable" => {
            "The AshatHub service is temporarily unavailable."
        }
        "upstream_error" | "chat_engine_unavailable" | "planner_unavailable" => {
            "The upstream service is currently unavailable."
        }
        "project_not_found" => "The project was not found. It may have been deleted.",
        "conversation_not_found" | "not_found" => "The requested resource was not found.",
        "job_not_found" => "The build job was not found.",
        "plan_not_found" => "The build plan was not found.",
        "file_not_found" => "The file was not found.",
        "folder_not_found" => "The folder was not found.",
        "deployment_not_found" => "The deployment was not found.",
        "deployment_backup_not_found" => "The deployment backup was not found.",
        "preview_file_not_found" => "The preview file was not found.",
        "skill_not_found" => "The requested skill was not found.",
        "device_code_not_found" => "The authentication code expired or was not found.",
        "model_not_found" => "The requested model was not found.",
        "release_not_found" => "The release was not found.",
        _ if code.ends_with("_not_found") => "The requested resource was not found.",
        "not_found_or_expired" => "The resource was not found or has expired.",
        _ => "The request could not be completed.",
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_error_response;
    use axum::{body::to_bytes, http::StatusCode, response::IntoResponse, Json};
    use serde_json::Value;

    #[tokio::test]
    async fn error_envelope_contains_code_message_and_request_id() {
        let response = normalize_error_response(
            (
                StatusCode::UNAUTHORIZED,
                Json(serde_json::json!({"error": "unauthenticated"})),
            )
                .into_response(),
            "req-test",
        )
        .await;
        let body = to_bytes(response.into_body(), 16 * 1024)
            .await
            .expect("error body should be readable");
        let value: Value = serde_json::from_slice(&body).expect("error body should be JSON");
        assert_eq!(value["error"]["code"], "unauthenticated");
        assert_eq!(value["error"]["request_id"], "req-test");
        assert!(value["error"]["message"].as_str().is_some());
    }
}
