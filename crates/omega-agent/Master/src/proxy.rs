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

use crate::demand::InstanceGuard;
use crate::metrics::MetricsStore;
use crate::types::{BackendServer, ChatRequest, ChatResponse, MetricRecord, Pool, ProxyError};

pub struct CodingAgentProxy {
    pool: Arc<crate::demand::DemandPool>,
    timeout: Duration,
}

impl CodingAgentProxy {
    pub fn new(pool: Arc<crate::demand::DemandPool>, timeout: Duration) -> Self {
        Self { pool, timeout }
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
        let pool = Arc::clone(&self.pool);
        let guard = pool
            .clone()
            .acquire(metrics, self.timeout)
            .await
            .map_err(|_| ProxyError::NoneAvailable)?;
        let started = Instant::now();

        let response = send_non_stream(&guard, request).await;

        let latency = started.elapsed().as_secs_f64() * 1000.0;
        match response {
            Ok(mut value) => {
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
        let pool = Arc::clone(&self.pool);
        let guard = pool
            .clone()
            .acquire(metrics, self.timeout)
            .await
            .map_err(|_| ProxyError::NoneAvailable)?;

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
                error_category: Some(format!("upstream HTTP {code}")),
            });
            return Err(ProxyError::Status(code));
        }

        let port_for_metrics = guard.port();
        let lane = guard.port().to_string();
        let bytes_stream = response.bytes_stream().map(move |chunk_result| {
            chunk_result.map_err(|err| ProxyError::Connection(err.to_string()))
        });
        metrics.record(MetricRecord {
            timestamp: Utc::now().to_rfc3339(),
            pool: Pool::CodingAgent,
            intent: intent_label,
            target_port: port_for_metrics,
            success: true,
            latency_ms: 0.0,
            queue_wait_ms: 0.0,
            error_category: None,
        });

        Ok(GuardedStream {
            inner: LaneAnnotatingStream {
                byte_stream: Box::pin(bytes_stream),
                buffer: Vec::new(),
                lane,
            },
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
pub(crate) struct GuardedStream {
    inner: LaneAnnotatingStream,
    _guard: InstanceGuard,
}

impl Stream for GuardedStream {
    type Item = Result<Bytes, ProxyError>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let me = self.get_mut();
        Pin::new(&mut me.inner).poll_next(cx)
    }
}

/// Buffers raw SSE bytes from llama-server, splits them on the SSE
/// event terminator (`\n\n`), and re-emits each event with a `"lane"`
/// field injected into the `data: {…}` JSON payload so consumers can
/// identify which Coding Agent port produced each chunk. The
/// terminator `data: [DONE]` and any non-`data:` lines pass through
/// untouched.
pub(crate) struct LaneAnnotatingStream {
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
    let payload_trimmed = s[after_prefix..line_end]
        .trim_end_matches(|c: char| c == '\n' || c == '\r');

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

async fn send_non_stream(
    guard: &InstanceGuard,
    request: &ChatRequest,
) -> Result<Value, ProxyError> {
    let url = format!(
        "http://{}:{}/v1/chat/completions",
        guard.pool().spec.host,
        guard.port()
    );
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
        warn!(port = guard.port(), code, "upstream returned non-success");
        return Err(ProxyError::Status(code));
    }

    let value: Value = response
        .json()
        .await
        .map_err(|err| ProxyError::Decode(err.to_string()))?;
    info!(port = guard.port(), "1.2B inference accepted");
    debug!(
        port = guard.port(),
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
                error_category: Some(format!("cross-server {}/{} HTTP {code}", backend.id, backend.port)),
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
                error_category: Some(format!("cross-server {}/{} HTTP {code}", backend.id, backend.port)),
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
pub(crate) struct CrossServerStream {
    inner: LaneAnnotatingStream,
}

impl Stream for CrossServerStream {
    type Item = Result<Bytes, ProxyError>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let me = self.get_mut();
        Pin::new(&mut me.inner).poll_next(cx)
    }
}
