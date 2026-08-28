//! Streaming/non-streaming proxy from the Coding Agent pool's RAII `InstanceGuard`
//! back to the client. Each call uses one slot from the 1.2B Coding Agent pool;
//! the instance is killed when the guard drops at the end of the request.

use bytes::Bytes;
use chrono::Utc;
use futures::{Stream, StreamExt};
use serde_json::{json, Value};
use std::{
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant},
};
use tracing::{debug, info, warn};

use omega_common::metrics::MetricsStore;
use omega_common::types::{
    BackendServer, ChatRequest, ChatResponse, MetricRecord, Pool, ProxyError,
};
use omega_common::workspace::AgentWorkspace;

use crate::demand::InstanceGuard;

struct RequestActivity(Arc<MetricsStore>);

impl RequestActivity {
    fn new(metrics: &Arc<MetricsStore>) -> Self {
        metrics.request_started();
        Self(Arc::clone(metrics))
    }
}

impl Drop for RequestActivity {
    fn drop(&mut self) {
        self.0.request_finished();
    }
}

pub struct CodingAgentProxy {
    pool: Arc<crate::demand::DemandPool>,
    timeout: Duration,
    workspace: AgentWorkspace,
}

impl CodingAgentProxy {
    pub fn new(
        pool: Arc<crate::demand::DemandPool>,
        timeout: Duration,
        workspace: AgentWorkspace,
    ) -> Self {
        Self {
            pool,
            timeout,
            workspace,
        }
    }

    /// Acquire a 1.2B slot, forward the request non-streamingly, return the
    /// `ChatResponse` and the guard. **Caller must drop the guard** to free
    /// the port.
    pub async fn forward(
        &self,
        request: &ChatRequest,
        intent_label: &'static str,
        metrics: &Arc<MetricsStore>,
    ) -> Result<(ChatResponse, InstanceGuard), ProxyError> {
        let _active = RequestActivity::new(metrics);
        let pool = Arc::clone(&self.pool);
        let guard = pool
            .clone()
            .acquire(metrics, self.timeout)
            .await
            .map_err(|_| ProxyError::NoneAvailable)?; // Best-effort workspace session log; never blocks the proxy path.
        let ws = self.workspace.clone();
        let req = request.clone();
        let intent = intent_label.to_owned();
        let port = guard.port();
        tokio::task::spawn_blocking(move || {
            if let Err(err) = ws.log_request(port, &req, &intent) {
                debug!(error = %err, "failed to log request to agent workspace");
            }
        });
        let started = Instant::now();

        let response = send_non_stream(&guard, request).await;

        let latency = started.elapsed().as_secs_f64() * 1000.0;
        match response {
            Ok(mut value) => {
                let (prompt_tokens, completion_tokens) = usage_tokens(&value);
                if let Some(obj) = value.as_object_mut() {
                    obj.insert("lane".into(), json!(guard.port().to_string()));
                }
                metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: intent_label,
                    target_port: guard.port(),
                    success: true,
                    latency_ms: latency,
                    queue_wait_ms: 0.0,
                    prompt_tokens,
                    completion_tokens,
                    time_to_first_token_ms: None,
                    error_category: None,
                });
                let parsed: ChatResponse = serde_json::from_value(value)
                    .map_err(|err| ProxyError::Decode(err.to_string()))?;
                Ok((parsed, guard))
            }
            Err(err) => {
                metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: intent_label,
                    target_port: guard.port(),
                    success: false,
                    latency_ms: latency,
                    queue_wait_ms: 0.0,
                    prompt_tokens: 0,
                    completion_tokens: 0,
                    time_to_first_token_ms: None,
                    error_category: Some(err.to_string()),
                });
                Err(err)
            }
        }
    }

    /// Acquire a 1.2B slot and stream the request body through. Returns
    /// a `GuardedStream` whose embedded `InstanceGuard` is reclaimed
    /// automatically when the SSE stream is fully drained (or the
    /// response connection drops). The handler must not drop the
    /// guard separately — it is owned by the returned wrapper.
    pub async fn forward_streaming(
        &self,
        request: &ChatRequest,
        intent_label: &'static str,
        metrics: &Arc<MetricsStore>,
    ) -> Result<GuardedStream, ProxyError> {
        let active = RequestActivity::new(metrics);
        let pool = Arc::clone(&self.pool);
        let guard = pool
            .clone()
            .acquire(metrics, self.timeout)
            .await
            .map_err(|_| ProxyError::NoneAvailable)?; // Best-effort workspace session log; never blocks the proxy path.
        let ws = self.workspace.clone();
        let req = request.clone();
        let intent = intent_label.to_owned();
        let port = guard.port();
        tokio::task::spawn_blocking(move || {
            if let Err(err) = ws.log_request(port, &req, &intent) {
                debug!(error = %err, "failed to log request to agent workspace");
            }
        });

        let url = format!(
            "http://{}:{}/v1/chat/completions",
            pool.spec.host,
            guard.port()
        );
        let client = reqwest::Client::builder()
            .timeout(self.timeout)
            .build()
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        let mut body =
            serde_json::to_value(request).map_err(|err| ProxyError::Decode(err.to_string()))?;
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), json!(true));
            obj.insert("stream_options".into(), json!({"include_usage": true}));
        }

        let response = client
            .post(&url)
            .header("Accept", "text/event-stream")
            .json(&body)
            .send()
            .await
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        if !response.status().is_success() {
            let code = response.status().as_u16();
            metrics.record(MetricRecord {
                timestamp: Utc::now().to_rfc3339(),
                pool: Pool::CodingAgent,
                intent: intent_label,
                target_port: guard.port(),
                success: false,
                latency_ms: 0.0,
                queue_wait_ms: 0.0,
                prompt_tokens: 0,
                completion_tokens: 0,
                time_to_first_token_ms: None,
                error_category: Some(format!("upstream HTTP {code}")),
            });
            return Err(ProxyError::Status(code));
        }

        let port_for_metrics = guard.port();
        let lane = guard.port().to_string();
        let bytes_stream = response.bytes_stream().map(move |chunk_result| {
            chunk_result.map_err(|err| ProxyError::Connection(err.to_string()))
        });
        Ok(GuardedStream {
            inner: LaneAnnotatingStream {
                byte_stream: Box::pin(bytes_stream),
                buffer: Vec::new(),
                lane,
            },
            metrics: Arc::clone(metrics),
            intent: intent_label,
            port: port_for_metrics,
            started: Instant::now(),
            first_token_ms: None,
            prompt_tokens: 0,
            completion_tokens: 0,
            _active: active,
            _guard: guard,
        })
    }
}

/// Wraps the llama-server response stream together with its
/// `InstanceGuard`. The guard's `Drop` impl spawns a tokio task that
/// kills the spawning llama-server — without this wrapper, the
/// streaming handler in `handlers.rs::chat` would drop the guard at
/// the end of the match arm and the backend would be killed before
/// the client reads any SSE bytes. The guard's lifetime is now
/// bound to the stream's own, so the port is reclaimed only when the
/// stream is fully drained (or the response connection drops).
pub struct GuardedStream {
    inner: LaneAnnotatingStream,
    metrics: Arc<MetricsStore>,
    intent: &'static str,
    port: u16,
    started: Instant,
    first_token_ms: Option<f64>,
    prompt_tokens: u32,
    completion_tokens: u32,
    _active: RequestActivity,
    _guard: InstanceGuard,
}

impl Stream for GuardedStream {
    type Item = Result<Bytes, ProxyError>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let me = self.get_mut();
        match Pin::new(&mut me.inner).poll_next(cx) {
            Poll::Ready(Some(Ok(bytes))) => {
                update_stream_usage(
                    &bytes,
                    &mut me.first_token_ms,
                    &mut me.prompt_tokens,
                    &mut me.completion_tokens,
                    me.started,
                );
                Poll::Ready(Some(Ok(bytes)))
            }
            Poll::Ready(None) => {
                me.metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: me.intent,
                    target_port: me.port,
                    success: true,
                    latency_ms: me.started.elapsed().as_secs_f64() * 1000.0,
                    queue_wait_ms: 0.0,
                    prompt_tokens: me.prompt_tokens,
                    completion_tokens: me.completion_tokens,
                    time_to_first_token_ms: me.first_token_ms,
                    error_category: None,
                });
                Poll::Ready(None)
            }
            Poll::Ready(Some(Err(err))) => {
                me.metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: me.intent,
                    target_port: me.port,
                    success: false,
                    latency_ms: me.started.elapsed().as_secs_f64() * 1000.0,
                    queue_wait_ms: 0.0,
                    prompt_tokens: me.prompt_tokens,
                    completion_tokens: me.completion_tokens,
                    time_to_first_token_ms: me.first_token_ms,
                    error_category: Some(err.to_string()),
                });
                Poll::Ready(Some(Err(err)))
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

fn update_stream_usage(
    bytes: &Bytes,
    first_token_ms: &mut Option<f64>,
    prompt: &mut u32,
    completion: &mut u32,
    started: Instant,
) {
    for line in bytes.split(|b| *b == b'\n') {
        let Some(payload) = line.strip_prefix(b"data: ") else {
            continue;
        };
        let Ok(value) = serde_json::from_slice::<Value>(payload) else {
            continue;
        };
        if first_token_ms.is_none()
            && value
                .get("choices")
                .and_then(Value::as_array)
                .is_some_and(|choices| !choices.is_empty())
        {
            *first_token_ms = Some(started.elapsed().as_secs_f64() * 1000.0);
        }
        if let Some(usage) = value.get("usage") {
            *prompt = usage
                .get("prompt_tokens")
                .and_then(Value::as_u64)
                .unwrap_or(0) as u32;
            *completion = usage
                .get("completion_tokens")
                .and_then(Value::as_u64)
                .unwrap_or(0) as u32;
        }
    }
}

/// Buffers raw SSE bytes from llama-server, splits them on the SSE
/// event terminator (`\n\n`), and re-emits each event with a `"lane"`
/// field injected into the `data: {…}` JSON payload so consumers can
/// identify which Coding Agent port produced each chunk. The
/// terminator `data: [DONE]` and any non-`data:` lines pass through
/// untouched.
pub struct LaneAnnotatingStream {
    byte_stream: Pin<Box<dyn Stream<Item = Result<Bytes, ProxyError>> + Send>>,
    buffer: Vec<u8>,
    lane: String,
}

impl Stream for LaneAnnotatingStream {
    type Item = Result<Bytes, ProxyError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        // LaneAnnotatingStream is Unpin because its only non-Unpin field
        // (Pin<Box<dyn Stream<…>>>) is itself Unpin.
        let me = self.get_mut();
        loop {
            // Drain any complete events currently buffered.
            if let Some(idx) = find_event_boundary(&me.buffer) {
                let event: Vec<u8> = me.buffer.drain(..idx + 2).collect();
                let annotated = annotate_event(&event, &me.lane);
                return Poll::Ready(Some(Ok(Bytes::from(annotated))));
            }
            // Need more upstream bytes.
            match me.byte_stream.as_mut().poll_next(cx) {
                Poll::Ready(Some(Ok(bytes))) => {
                    me.buffer.extend_from_slice(&bytes);
                    continue;
                }
                Poll::Ready(Some(Err(err))) => return Poll::Ready(Some(Err(err))),
                Poll::Ready(None) => {
                    // Upstream closed. Flush trailing partial event verbatim.
                    if me.buffer.is_empty() {
                        return Poll::Ready(None);
                    }
                    let trailing = std::mem::take(&mut me.buffer);
                    return Poll::Ready(Some(Ok(Bytes::from(trailing))));
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

/// Locate the offset of the SSE event terminator (`\n\n`) in `buf`.
fn find_event_boundary(buf: &[u8]) -> Option<usize> {
    for i in 0..buf.len().saturating_sub(1) {
        if buf[i] == b'\n' && buf[i + 1] == b'\n' {
            return Some(i);
        }
    }
    None
}

/// Annotate one SSE event block with the serving port. Recognises a
/// `data: <payload>` line within the block, parses the payload as
/// JSON, inserts `"lane"`, and re-emits the block with the JSON line
/// rebuilt in place. Untouched cases: `data: [DONE]`, non-JSON
/// payloads, and events without a `data:` line.
fn annotate_event(event_bytes: &[u8], lane: &str) -> Vec<u8> {
    let s = match std::str::from_utf8(event_bytes) {
        Ok(s) => s,
        Err(_) => return event_bytes.to_vec(),
    };
    const PREFIX: &str = "data: ";
    let Some(idx) = s.find(PREFIX) else {
        return event_bytes.to_vec();
    };
    let after_prefix = idx + PREFIX.len();

    // Find the SSE line terminator inside this event block.
    let line_end = s[after_prefix..]
        .find('\n')
        .map(|n| after_prefix + n)
        .unwrap_or(s.len());
    let payload_trimmed =
        s[after_prefix..line_end].trim_end_matches(|c: char| c == '\n' || c == '\r');

    if payload_trimmed == "[DONE]" {
        return event_bytes.to_vec();
    }
    match serde_json::from_str::<Value>(payload_trimmed) {
        Ok(mut value) => {
            if let Some(obj) = value.as_object_mut() {
                obj.insert("lane".into(), json!(lane));
            }
            let new_payload = value.to_string();
            let mut out = String::with_capacity(s.len() + lane.len() + 16);
            out.push_str(&s[..after_prefix]);
            out.push_str(&new_payload);
            out.push_str(&s[line_end..]);
            out.into_bytes()
        }
        Err(_) => event_bytes.to_vec(),
    }
}

fn usage_tokens(value: &Value) -> (u32, u32) {
    let usage = value.get("usage");
    (
        usage
            .and_then(|u| u.get("prompt_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0) as u32,
        usage
            .and_then(|u| u.get("completion_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0) as u32,
    )
}

pub(crate) async fn send_non_stream(
    guard: &InstanceGuard,
    request: &ChatRequest,
) -> Result<Value, ProxyError> {
    send_non_stream_to(&guard.pool().spec.host, guard.port(), request).await
}

/// Non-streaming chat completion against `host:port`. Shared by the plain
/// proxy path and the tool loop's per-iteration model calls.
pub(crate) async fn send_non_stream_to(
    host: &str,
    port: u16,
    request: &ChatRequest,
) -> Result<Value, ProxyError> {
    let url = format!("http://{host}:{port}/v1/chat/completions");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(120))
        .build()
        .map_err(|err| ProxyError::Connection(err.to_string()))?;

    let mut body =
        serde_json::to_value(request).map_err(|err| ProxyError::Decode(err.to_string()))?;
    if let Some(obj) = body.as_object_mut() {
        obj.insert("stream".into(), json!(false));
    }

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|err| ProxyError::Connection(err.to_string()))?;

    if !response.status().is_success() {
        let code = response.status().as_u16();
        warn!(port, code, "upstream returned non-success");
        return Err(ProxyError::Status(code));
    }

    let value: Value = response
        .json()
        .await
        .map_err(|err| ProxyError::Decode(err.to_string()))?;
    info!(port, "1.2B inference accepted");
    debug!(
        port,
        "payload keys: {:?}",
        value.as_object().map(|o| o.keys().collect::<Vec<_>>())
    );
    Ok(value)
}

/// Cross-server proxy: routes `chat` requests to a non-local backend
/// (Beta, Delta, ...) over plain HTTP. The upstream is an external
/// llama-server with its own lifecycle, so we don't manage processes
/// here — we forward the body, decorate the response with a
/// server-id `lane` field, and return. The handler (`handlers::chat`)
/// selects this proxy when the row chain picks a backend whose id is
/// not `omega`.
pub struct CrossServerProxy {
    timeout: Duration,
}

impl CrossServerProxy {
    pub fn new(timeout: Duration) -> Self {
        Self { timeout }
    }

    /// Forward the request to `backend.host:backend.port`, inject
    /// `lane = backend.id`, and parse a `ChatResponse`. `request` is
    /// the same body a `CodingAgentProxy` would have sent; we just
    /// pass it through minus the local-only intent wiring.
    pub async fn forward(
        &self,
        request: &ChatRequest,
        intent_label: &'static str,
        backend: &BackendServer,
        metrics: &Arc<MetricsStore>,
    ) -> Result<ChatResponse, ProxyError> {
        let started = Instant::now();
        let url = format!(
            "http://{}:{}/v1/chat/completions",
            backend.host, backend.port
        );
        let client = reqwest::Client::builder()
            .timeout(self.timeout)
            .build()
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        let mut body =
            serde_json::to_value(request).map_err(|err| ProxyError::Decode(err.to_string()))?;
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), json!(false));
        }

        let mut req = client.post(&url).json(&body);
        if let Some(api_key) = backend.api_key.as_ref() {
            req = req.header("X-Ashat-Key", api_key);
        }
        let response = req
            .send()
            .await
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        if !response.status().is_success() {
            let code = response.status().as_u16();
            warn!(
                backend_id = %backend.id,
                host = %backend.host,
                port = backend.port,
                code,
                "cross-server upstream returned non-success"
            );
            metrics.record(MetricRecord {
                timestamp: Utc::now().to_rfc3339(),
                pool: Pool::CodingAgent,
                intent: intent_label,
                target_port: backend.port,
                success: false,
                latency_ms: 0.0,
                queue_wait_ms: 0.0,
                prompt_tokens: 0,
                completion_tokens: 0,
                time_to_first_token_ms: None,
                error_category: Some(format!(
                    "cross-server {}/{} HTTP {code}",
                    backend.id, backend.port
                )),
            });
            return Err(ProxyError::Status(code));
        }

        let mut value: Value = response
            .json()
            .await
            .map_err(|err| ProxyError::Decode(err.to_string()))?;
        let lane = backend.id.clone();
        if let Some(obj) = value.as_object_mut() {
            obj.insert("lane".into(), json!(lane));
        }

        let latency = started.elapsed().as_secs_f64() * 1000.0;
        info!(
            backend_id = %backend.id,
            host = %backend.host,
            port = backend.port,
            latency_ms = latency,
            "cross-server inference accepted"
        );
        metrics.record(MetricRecord {
            timestamp: Utc::now().to_rfc3339(),
            pool: Pool::CodingAgent,
            intent: intent_label,
            target_port: backend.port,
            success: true,
            latency_ms: latency,
            queue_wait_ms: 0.0,
            prompt_tokens: 0,
            completion_tokens: 0,
            time_to_first_token_ms: None,
            error_category: None,
        });

        serde_json::from_value(value).map_err(|err| ProxyError::Decode(err.to_string()))
    }

    /// Forward the request to `backend.host:backend.port` with
    /// `stream: true`, stream raw SSE bytes, and annotate each
    /// `data: {…}` JSON event with `"lane": backend.id`. The caller
    /// owns the returned `CrossServerStream` until it is fully drained.
    pub async fn forward_streaming(
        &self,
        request: &ChatRequest,
        intent_label: &'static str,
        backend: &BackendServer,
        metrics: &Arc<MetricsStore>,
    ) -> Result<CrossServerStream, ProxyError> {
        let url = format!(
            "http://{}:{}/v1/chat/completions",
            backend.host, backend.port
        );
        let client = reqwest::Client::builder()
            .timeout(self.timeout)
            .build()
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        let mut body =
            serde_json::to_value(request).map_err(|err| ProxyError::Decode(err.to_string()))?;
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), json!(true));
        }

        let mut req = client
            .post(&url)
            .header("Accept", "text/event-stream")
            .json(&body);
        if let Some(api_key) = backend.api_key.as_ref() {
            req = req.header("X-Ashat-Key", api_key);
        }
        let response = req
            .send()
            .await
            .map_err(|err| ProxyError::Connection(err.to_string()))?;

        if !response.status().is_success() {
            let code = response.status().as_u16();
            metrics.record(MetricRecord {
                timestamp: Utc::now().to_rfc3339(),
                pool: Pool::CodingAgent,
                intent: intent_label,
                target_port: backend.port,
                success: false,
                latency_ms: 0.0,
                queue_wait_ms: 0.0,
                prompt_tokens: 0,
                completion_tokens: 0,
                time_to_first_token_ms: None,
                error_category: Some(format!(
                    "cross-server {}/{} HTTP {code}",
                    backend.id, backend.port
                )),
            });
            return Err(ProxyError::Status(code));
        }

        let bytes_stream = response.bytes_stream().map(move |chunk_result| {
            chunk_result.map_err(|err| ProxyError::Connection(err.to_string()))
        });
        info!(
            backend_id = %backend.id,
            host = %backend.host,
            port = backend.port,
            "cross-server streaming accepted"
        );
        metrics.record(MetricRecord {
            timestamp: Utc::now().to_rfc3339(),
            pool: Pool::CodingAgent,
            intent: intent_label,
            target_port: backend.port,
            success: true,
            latency_ms: 0.0,
            queue_wait_ms: 0.0,
            prompt_tokens: 0,
            completion_tokens: 0,
            time_to_first_token_ms: None,
            error_category: None,
        });

        Ok(CrossServerStream {
            inner: LaneAnnotatingStream {
                byte_stream: Box::pin(bytes_stream),
                buffer: Vec::new(),
                lane: backend.id.clone(),
            },
        })
    }
}

/// Cross-server streaming response wrapper. No RAII guard because the
/// remote backend has its own lifecycle; drop semantics only affect
/// the axum response body stream.
pub struct CrossServerStream {
    inner: LaneAnnotatingStream,
}

impl Stream for CrossServerStream {
    type Item = Result<Bytes, ProxyError>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let me = self.get_mut();
        Pin::new(&mut me.inner).poll_next(cx)
    }
}
