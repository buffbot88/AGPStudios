use axum::{
    body::Body,
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Serialize;
use futures_util::StreamExt;
use std::{collections::HashMap, sync::Arc};
use tokio::{sync::Semaphore, time::{timeout, Duration}};

use crate::AppState;
use alpha_common::{AgentEvent, ChatRequest};
use alpha_core::backend::ChatBackend;
use alpha_core::router::Intent;

pub fn create_router(state: AppState) -> Router {
    Router::new()
        .route("/v1/chat/completions", post(chat_completions))
        .route("/v1/models", get(models))
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/workers", get(workers))
        .with_state(state)
}

async fn chat_completions(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(mut request): Json<ChatRequest>,
) -> Response {
    if request.mode.is_none() {
        request.mode = headers
            .get("x-ashat-mode")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
    }
    // Wire the wait queue: full -> 429, else enqueue and wait for a concurrency slot.
    let request_id = format!(
        "req-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    {
        let mut queue = state.queue.write().await;
        if queue.enqueue(request_id).is_err() {
            return (
                StatusCode::TOO_MANY_REQUESTS,
                Json(serde_json::json!({
                    "error": "Queue full",
                    "message": "Maximum requests in queue reached"
                })),
            ).into_response();
        }
    }
    let intent = state.router.classify(&request.messages, request.stream, request.mode.as_deref());

    tracing::info!(
        "Classified intent: {:?} ({} messages)",
        intent,
        request.messages.len()
    );

    let _account_permit = if let Some(account) = headers.get("x-ashat-account").and_then(|value| value.to_str().ok()).filter(|value| !value.is_empty()) {
        let slots = {
            let mut accounts = state.account_slots.lock().await;
            accounts.entry(account.to_owned()).or_insert_with(|| Arc::new(Semaphore::new(2))).clone()
        };
        match timeout(Duration::from_secs(30), slots.acquire_owned()).await {
            Ok(Ok(permit)) => Some(permit),
            _ => {
                let mut queue = state.queue.write().await;
                queue.dequeue();
                return completion_response(request.stream, StatusCode::TOO_MANY_REQUESTS, serde_json::json!({
                    "error": "Account inference limit reached",
                    "message": "This account has too many active requests; retry shortly"
                }));
            }
        }
    } else { None };

    let capacity = match intent {
        Intent::Vision => state.vision_slots.clone(),
        Intent::Liquid => state.liquid_slots.clone(),
    };
    let _permit = match timeout(Duration::from_secs(30), capacity.acquire_owned()).await {
        Ok(Ok(permit)) => permit,
        Ok(Err(error)) => {
            let mut queue = state.queue.write().await;
            queue.dequeue();
            tracing::error!(?error, "inference capacity unavailable");
            return completion_response(request.stream, StatusCode::SERVICE_UNAVAILABLE, serde_json::json!({
                "error": "Inference capacity unavailable"
            }));
        }
        Err(_) => {
            let mut queue = state.queue.write().await;
            queue.dequeue();
            return completion_response(request.stream, StatusCode::TOO_MANY_REQUESTS, serde_json::json!({
                "error": "Inference queue wait expired",
                "message": "Capacity is busy; retry shortly"
            }));
        }
    };
    {
        let mut queue = state.queue.write().await;
        queue.dequeue();
    }

    if request.stream {
        match intent {
            Intent::Liquid => return match state.liquid.stream(&request).await {
                Ok(response) => {
                    if headers.get("x-galileo-protocol").and_then(|value| value.to_str().ok()) == Some("events") {
                        canonical_stream_response(response)
                    } else {
                        upstream_stream_response(response)
                    }
                },
                Err(error) => completion_response(true, StatusCode::BAD_GATEWAY, serde_json::json!({"error": error.to_string()})),
            },
            Intent::Vision => return match alpha_core::proxy::vision_stream(&state.vision_pool, &request).await {
                Ok((port, response)) => {
                    if headers.get("x-galileo-protocol").and_then(|value| value.to_str().ok()) == Some("events") {
                        canonical_vision_stream_response(state.vision_pool.clone(), port, response)
                    } else {
                        vision_stream_response(state.vision_pool.clone(), port, response)
                    }
                },
                Err(error) => completion_response(true, StatusCode::BAD_GATEWAY, serde_json::json!({"error": error.to_string()})),
            },
        }
    }

    match intent {
        // ── Vision: on-demand 450M VL ──────────────────────────
        Intent::Vision => {
            match alpha_core::proxy::vision_completions(&state.vision_pool, &request).await {
                Ok(body) => completion_response(request.stream, StatusCode::OK, body),
                Err(e) => {
                    tracing::error!("Vision inference failed: {}", e);
                    completion_response(request.stream, StatusCode::BAD_GATEWAY, serde_json::json!({
                        "error": "Vision inference failed",
                        "message": e.to_string()
                    }))
                }
            }
        }

        Intent::Liquid => match state.liquid.stream(&request).await {
            Ok(response) => completion_response(request.stream, StatusCode::OK, response.json().await.unwrap_or_default()),
            Err(error) => completion_response(request.stream, StatusCode::BAD_GATEWAY, serde_json::json!({"error": error.to_string()})),
        },
    }
}

fn completion_response(stream: bool, status: StatusCode, body: serde_json::Value) -> Response {
    if !stream {
        return (status, Json(body)).into_response();
    }

    let content = body
        .pointer("/choices/0/message/content")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let id = format!(
        "chatcmpl-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default()
    );
    let chunk = serde_json::json!({
        "id": id,
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": null}]
    });
    let done = serde_json::json!({
        "id": id,
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    });
    let payload = format!("data: {chunk}\n\ndata: {done}\n\ndata: [DONE]\n\n");
    (
        status,
        [(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache")],
        payload,
    ).into_response()
}

fn canonical_stream_response(response: reqwest::Response) -> Response {
    normalized_stream_response(response.bytes_stream(), None)
}

fn canonical_vision_stream_response(pool: Arc<alpha_core::demand::VisionPool>, port: u16, response: reqwest::Response) -> Response {
    normalized_stream_response(response.bytes_stream(), Some((pool, port)))
}

/// Per-call accumulation for OpenAI tool_call deltas: the name may trail the
/// id, arguments arrive fragmented across records, and continuations carry no
/// id (they are keyed by the last-seen id).
#[derive(Default)]
struct ToolAccumulation {
    name: String,
    args: String,
    announced: bool,
}

/// Emit every accumulated tool call exactly once: tool.start (if the name
/// arrived after the initial id chunk) followed by a single tool.arguments
/// carrying the fully accumulated, parsed arguments. Fragmented deltas are
/// joined before parsing, so partial JSON prefixes never leak downstream.
fn flush_tool_events(tools: &mut HashMap<String, ToolAccumulation>, order: &mut Vec<String>) -> String {
    let mut output = String::new();
    for id in order.drain(..) {
        if let Some(entry) = tools.remove(&id) {
            if entry.name.is_empty() {
                continue;
            }
            if !entry.announced {
                output.push_str(&encode_event(&AgentEvent::ToolStart { id: id.clone(), name: entry.name.clone() }));
            }
            let arguments = serde_json::from_str::<serde_json::Value>(entry.args.trim())
                .unwrap_or_else(|_| serde_json::json!({ "raw": entry.args }));
            output.push_str(&encode_event(&AgentEvent::ToolArguments { id, arguments }));
        }
    }
    output
}

fn parse_special_tool_call(raw: &str) -> Option<(String, serde_json::Value)> {
    let call = raw.trim().trim_start_matches('[').trim_end_matches(']').trim();
    let open = call.find('(')?;
    let name = call[..open].trim().to_owned();
    if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') { return None; }
    let args = call[open + 1..].trim_end_matches(')').trim();
    if args.is_empty() { return Some((name, serde_json::json!({}))); }
    let mut object = serde_json::Map::new();
    for pair in args.split(',') {
        let (key, value) = pair.split_once('=')?;
        let key = key.trim().trim_matches('"').trim_matches('\'').to_owned();
        let value = value.trim();
        let value = serde_json::from_str(value).unwrap_or_else(|_| serde_json::Value::String(value.trim_matches('"').trim_matches('\'').to_owned()));
        object.insert(key, value);
    }
    Some((name, serde_json::Value::Object(object)))
}

fn normalized_stream_response(
    input: impl futures_util::Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Send + 'static,
    release: Option<(Arc<alpha_core::demand::VisionPool>, u16)>,
) -> Response {
    let response_id = format!("resp-{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|value| value.as_nanos()).unwrap_or_default());
    let stream = futures_util::stream::unfold((Box::pin(input), String::new(), false, false, response_id, release, HashMap::<String, ToolAccumulation>::new(), Vec::<String>::new(), String::new(), String::new(), false, 0u32), |(mut input, mut buffer, mut started, complete, response_id, release, mut tools, mut tool_order, mut last_tool_id, mut special_tool, mut special_active, mut special_index)| async move {
        loop {
            if let Some(end) = buffer.find("\n\n") {
                let record: String = buffer.drain(..end + 2).collect();
                if let Some(data) = record.lines().find_map(|line| line.strip_prefix("data: ")).filter(|data| *data != "[DONE]") {
                    if let Ok(chunk) = serde_json::from_str::<serde_json::Value>(data) {
                        let content = chunk.pointer("/choices/0/delta/content").and_then(serde_json::Value::as_str).unwrap_or_default();
                        let mut output = String::new();
                        if !content.is_empty() {
                            if !started {
                                started = true;
                                output.push_str(&encode_event(&AgentEvent::ResponseStart { response_id: response_id.clone() }));
                            }
                            let mut text = content.to_owned();
                            loop {
                                if !special_active {
                                    let Some(start) = text.find("<|tool_call_start|>") else {
                                        output.push_str(&encode_event(&AgentEvent::TextDelta { delta: text }));
                                        break;
                                    };
                                    if start > 0 { output.push_str(&encode_event(&AgentEvent::TextDelta { delta: text[..start].to_owned() })); }
                                    text = text[start + "<|tool_call_start|>".len()..].to_owned();
                                    special_active = true;
                                }
                                special_tool.push_str(&text);
                                let Some(end) = special_tool.find("<|tool_call_end|>") else { break; };
                                let raw = special_tool[..end].to_owned();
                                special_tool = special_tool[end + "<|tool_call_end|>".len()..].to_owned();
                                special_active = false;
                                if let Some((name, arguments)) = parse_special_tool_call(&raw) {
                                    special_index += 1;
                                    let id = format!("special-{response_id}-{special_index}");
                                    output.push_str(&encode_event(&AgentEvent::ToolStart { id: id.clone(), name }));
                                    output.push_str(&encode_event(&AgentEvent::ToolArguments { id, arguments }));
                                }
                                text = std::mem::take(&mut special_tool);
                            }
                        }
                        if let Some(calls) = chunk.pointer("/choices/0/delta/tool_calls").and_then(serde_json::Value::as_array) {
                            for call in calls {
                                // Continuation deltas (argument fragments) carry no id; they belong to the last-seen call.
                                if let Some(id) = call.get("id").and_then(serde_json::Value::as_str) {
                                    if !id.is_empty() {
                                        if !tools.contains_key(id) {
                                            tools.insert(id.to_owned(), ToolAccumulation::default());
                                            tool_order.push(id.to_owned());
                                        }
                                        last_tool_id = id.to_owned();
                                    }
                                }
                                if last_tool_id.is_empty() {
                                    continue;
                                }
                                let Some(entry) = tools.get_mut(&last_tool_id) else { continue };
                                if let Some(name) = call.pointer("/function/name").and_then(serde_json::Value::as_str) {
                                    if entry.name.is_empty() {
                                        entry.name = name.to_owned();
                                    }
                                }
                                if let Some(args) = call.pointer("/function/arguments").and_then(serde_json::Value::as_str) {
                                    entry.args.push_str(args);
                                }
                                if !entry.announced && !entry.name.is_empty() {
                                    entry.announced = true;
                                    output.push_str(&encode_event(&AgentEvent::ToolStart { id: last_tool_id.clone(), name: entry.name.clone() }));
                                }
                            }
                        }
                        // tool.arguments goes out exactly once per call, when the upstream declares the calls complete.
                        if chunk.pointer("/choices/0/finish_reason").and_then(serde_json::Value::as_str) == Some("tool_calls") && !tool_order.is_empty() {
                            output.push_str(&flush_tool_events(&mut tools, &mut tool_order));
                        }
                        if !output.is_empty() {
                            return Some((Ok::<bytes::Bytes, std::convert::Infallible>(bytes::Bytes::from(output)), (input, buffer, started, complete, response_id, release, tools, tool_order, last_tool_id, special_tool, special_active, special_index)));
                        }
                    }
                }
                continue;
            }
            match input.as_mut().next().await {
                Some(Ok(chunk)) => buffer.push_str(&String::from_utf8_lossy(&chunk)),
                Some(Err(error)) => {
                    let output = encode_event(&AgentEvent::Error { code: "upstream_stream".into(), message: error.to_string(), retryable: true });
                    if let Some((pool, port)) = release { pool.release(port).await; }
                    return Some((Ok::<bytes::Bytes, std::convert::Infallible>(bytes::Bytes::from(output)), (input, buffer, started, true, response_id, None, tools, tool_order, last_tool_id, special_tool, special_active, special_index)));
                }
                None => {
                    if !complete {
                        // Upstream closed without finish_reason; flush pending tool calls before completing.
                        let mut output = flush_tool_events(&mut tools, &mut tool_order);
                        if let Some((pool, port)) = release { pool.release(port).await; }
                        output.push_str(&encode_event(&AgentEvent::ResponseComplete));
                        return Some((Ok::<bytes::Bytes, std::convert::Infallible>(bytes::Bytes::from(output)), (input, buffer, started, true, response_id, None, tools, tool_order, last_tool_id, special_tool, special_active, special_index)));
                    }
                    return None;
                }
            }
        }
    });
    (StatusCode::OK, [(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache")], Body::from_stream(stream)).into_response()
}

fn encode_event(event: &AgentEvent) -> String {
    format!("event: {}\ndata: {}\n\n", event_type(event), serde_json::to_string(event).unwrap_or_else(|_| "{}".into()))
}

fn event_type(event: &AgentEvent) -> &'static str {
    match event {
        AgentEvent::ResponseStart { .. } => "response.start",
        AgentEvent::TextDelta { .. } => "text.delta",
        AgentEvent::ToolStart { .. } => "tool.start",
        AgentEvent::ToolArguments { .. } => "tool.arguments",
        AgentEvent::ToolResult { .. } => "tool.result",
        AgentEvent::Status { .. } => "status",
        AgentEvent::Error { .. } => "error",
        AgentEvent::ResponseComplete => "response.complete",
    }
}

fn upstream_stream_response(response: reqwest::Response) -> Response {
    let stream = response.bytes_stream().map(|chunk| chunk.map_err(axum::Error::new));
    (StatusCode::OK, [(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache")], Body::from_stream(stream)).into_response()
}

fn vision_stream_response(pool: Arc<alpha_core::demand::VisionPool>, port: u16, response: reqwest::Response) -> Response {
    let stream = futures_util::stream::unfold((response.bytes_stream(), Some((pool, port))), |(mut input, release)| async move {
        match input.next().await {
            Some(chunk) => Some((chunk.map_err(axum::Error::new), (input, release))),
            None => {
                if let Some((pool, port)) = release { pool.release(port).await; }
                None
            }
        }
    });
    (StatusCode::OK, [(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache")], Body::from_stream(stream)).into_response()
}

async fn models() -> impl IntoResponse {
    Json(serde_json::json!({
        "object": "list",
        "data": [{"id": "ashat", "object": "model", "owned_by": "ashat"}]
    }))
}

#[derive(Serialize)]
struct HealthResponse {
    status: String,
    version: String,
}

async fn health() -> impl IntoResponse {
    Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

#[derive(Serialize)]
struct StatusResponse {
    queued_requests: usize,
    max_queue: usize,
    active_requests: usize,
    available_liquid_slots: usize,
    available_vision_slots: usize,
    running_vision_instances: usize,
    max_vision_instances: usize,
}

async fn status(State(state): State<AppState>) -> impl IntoResponse {
    let queue = state.queue.read().await;
    let liquid_available = state.liquid_slots.available_permits();
    let vision_available = state.vision_slots.available_permits();
    Json(StatusResponse {
        queued_requests: queue.queue_size(),
        max_queue: queue.max_queue_size(),
        active_requests: liquid_available.max(1) - liquid_available
            + vision_available.max(1) - vision_available,
        available_liquid_slots: liquid_available,
        available_vision_slots: vision_available,
        running_vision_instances: state.vision_pool.running_count().await,
        max_vision_instances: state.config.models.max_instances as usize,
    })
}

#[derive(Serialize)]
struct WorkersResponse {
    liquid_backend_configured: bool,
    liquid_backend_healthy: bool,
    vision_worker_active: bool,
}

async fn workers(State(state): State<AppState>) -> impl IntoResponse {
    Json(WorkersResponse {
        liquid_backend_configured: !state.router.config().liquid.endpoint.is_empty(),
        liquid_backend_healthy: state.liquid.health_check().await,
        vision_worker_active: state.vision_pool.has_running().await,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flush_emits_start_then_arguments_once_with_joined_fragments() {
        let mut tools: HashMap<String, ToolAccumulation> = HashMap::new();
        let mut order = vec!["call_1".to_string()];
        tools.insert("call_1".to_string(), ToolAccumulation { name: "list".into(), args: format!("{}{}", "{\"pa", "th\":\".\"}"), announced: true });

        // announced=true: tool.start already went out during accumulation, flush must not repeat it.
        let output = flush_tool_events(&mut tools, &mut order);
        assert_eq!(output.matches("event: tool.start").count(), 0);
        assert_eq!(output.matches("event: tool.arguments").count(), 1);
        assert!(output.contains("\"path\":\".\""), "fragmented args must join: {output}");
    }

    #[test]
    fn flush_announces_start_for_name_arriving_after_id() {
        let mut tools: HashMap<String, ToolAccumulation> = HashMap::new();
        let mut order = vec!["call_2".to_string()];
        // Id chunk carried no name; name arrived in a continuation. No start event was emitted yet.
        tools.insert("call_2".to_string(), ToolAccumulation { name: "read".into(), args: "{}".into(), announced: false });

        let output = flush_tool_events(&mut tools, &mut order);
        assert_eq!(output.matches("event: tool.start").count(), 1);
        let start = output.find("event: tool.start").expect("start present");
        let args = output.find("event: tool.arguments").expect("args present");
        assert!(start < args, "tool.start must precede tool.arguments");
    }

    #[test]
    fn flush_skips_nameless_calls_and_repeated_flush_is_empty() {
        let mut tools: HashMap<String, ToolAccumulation> = HashMap::new();
        let mut order = vec!["call_3".to_string()];
        tools.insert("call_3".to_string(), ToolAccumulation { name: String::new(), args: "{}".into(), announced: false });

        assert!(flush_tool_events(&mut tools, &mut order).is_empty());
        assert!(flush_tool_events(&mut tools, &mut order).is_empty(), "second flush must be empty");
    }

    #[test]
    fn flush_falls_back_to_raw_object_on_unparseable_args() {
        let mut tools: HashMap<String, ToolAccumulation> = HashMap::new();
        let mut order = vec!["call_4".to_string()];
        tools.insert("call_4".to_string(), ToolAccumulation { name: "search".into(), args: "{oops".into(), announced: true });

        let output = flush_tool_events(&mut tools, &mut order);
        assert!(output.contains("\"raw\":\"{oops\""), "unparseable args must degrade to a raw payload: {output}");
    }
}
