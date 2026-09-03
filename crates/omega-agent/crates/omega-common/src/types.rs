//! Shared structs and enums used across the Omega server.
//!
//! All chat-shaped types snake_case their JSON fields to match the OpenAI-compatible
//! surface that the telemetry frontend and external clients expect.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::Path;

/// Known model-file extensions that may never surface publicly. Only these
/// are stripped — never an arbitrary dot-suffix, so versioned names like
/// `LFM2.5-1.2B-Instruct-Q4_K_M` keep their internal dots intact.
const MODEL_FILE_EXTS: [&str; 4] = [".gguf", ".bin", ".safetensors", ".onnx"];

/// Strip a known model-file extension from a name, if present.
fn strip_model_ext(name: &str) -> String {
    let lower = name.to_ascii_lowercase();
    for ext in MODEL_FILE_EXTS {
        if let Some(stem) = lower.strip_suffix(ext) {
            return name[..stem.len()].to_owned();
        }
    }
    name.to_owned()
}

/// Public-facing model name for telemetry: the basename with a known
/// model-file extension stripped. The public surface must never leak the
/// on-disk location of a GGUF, so handlers render this instead of the raw
/// path.
pub fn public_model_name(path: &Path) -> String {
    path.file_name()
        .map(|s| strip_model_ext(&s.to_string_lossy()))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "model".to_owned())
}

/// Same sanitization for a free-form model string (e.g. one already received
/// from a peer): take the final path segment and strip a known model-file
/// extension so an upstream leak is never forwarded to the public surface.
pub fn sanitize_model_name(raw: &str) -> String {
    let stem = Path::new(raw)
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| raw.to_owned());
    if stem.is_empty() {
        "model".to_owned()
    } else {
        strip_model_ext(&stem)
    }
}

/// Outcome of the intent router's classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Intent {
    /// Standard chat completion: route to a 1.2B coding-agent slot.
    Chat,
    /// Status/health check: still routes through a coding-agent slot but the lane
    /// is marked with `lane_state = "status"` for telemetry.
    Status,
    /// Reserved for the deferred "Advanced Coding Agent with tools" (Phase 6).
    #[serde(skip)]
    Code,
    /// Could not classify: route to a coding-agent slot but tracking will surface
    /// the unknown rate in `/api/public_metrics`.
    Unknown,
}

impl Intent {
    /// Stable lowercase wire label for telemetry and metrics.
    pub fn as_str(self) -> &'static str {
        match self {
            Intent::Chat => "chat",
            Intent::Status => "status",
            Intent::Code => "code",
            Intent::Unknown => "unknown",
        }
    }
}

/// One message in a chat request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentMessage {
    pub role: String,
    pub content: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentCapabilities {
    pub tools: bool,
    pub vision: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentRequest {
    pub conversation_id: String,
    #[serde(default)]
    pub project_id: Option<String>,
    pub messages: Vec<AgentMessage>,
    #[serde(default)]
    pub operation: Option<AgentOperation>,
    pub capabilities: AgentCapabilities,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentOperation {
    Chat,
    Agent,
    Vision,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type")]
pub enum AgentEvent {
    #[serde(rename = "response.start")]
    ResponseStart { response_id: String },
    #[serde(rename = "text.delta")]
    TextDelta { delta: String },
    #[serde(rename = "tool.start")]
    ToolStart { id: String, name: String },
    #[serde(rename = "tool.arguments")]
    ToolArguments { id: String, arguments: Value },
    #[serde(rename = "tool.result")]
    ToolResult { id: String, ok: bool, result: Option<Value>, error: Option<String> },
    #[serde(rename = "status")]
    Status { state: String, message: Option<String> },
    #[serde(rename = "error")]
    Error { code: String, message: String, retryable: bool },
    #[serde(rename = "response.complete")]
    ResponseComplete,
}

/// Body of POST /v1/chat/completions. OpenAI-compatible shape.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    #[serde(default)]
    pub model: Option<String>,
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub max_tokens: Option<u32>,
    #[serde(default)]
    pub temperature: Option<f32>,
    #[serde(default)]
    pub top_p: Option<f32>,
    #[serde(default)]
    pub stream: Option<bool>,
    /// Declares the operation so routing never infers intent from user text.
    #[serde(default)]
    pub operation: Option<AgentOperation>,
}

/// Omega v6 model variant: uses ollama.cpp as the inference backend.
///
/// The Omega v6 model is a special variant optimized for ollama.cpp
/// inference. It provides better performance and lower memory usage
/// than the standard llama-server backend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OmegaModelConfig {
    pub name: String,
    pub version: String,
    pub engine: String,
    pub model_path: String,
    pub description: String,
}

/// Body returned by POST /v1/chat/completions. OpenAI-compatible shape.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatResponse {
    pub id: String,
    pub object: String,
    pub created: u64,
    pub model: String,
    pub choices: Vec<ChatChoice>,
    pub usage: ChatUsage,
    /// Omega extension: which lane/port produced the response.
    #[serde(default)]
    pub lane: Option<String>,
    /// Omega extension: Script Validation Engine report for tool-loop runs.
    /// Present on advanced coding-agent (Phase 6) responses only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub validation: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatChoice {
    pub index: u32,
    pub message: ChatMessage,
    pub finish_reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatUsage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
}

/// Health probe body.
#[derive(Debug, Clone, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub uptime_seconds: f64,
    pub orchestrator_ready: bool,
    pub coding_agent_capacity: CodingCapacitySnapshot,
}

#[derive(Debug, Clone, Serialize)]
pub struct CodingCapacitySnapshot {
    pub ports_total: usize,
    pub ports_active: usize,
    pub queue_depth: usize,
    pub queue_limit: usize,
    pub memory_available_mb: Option<u64>,
    pub memory_pressure: Option<f64>,
    pub worker_startup_latency_ms: Option<f64>,
    pub recent_failure_rate: Option<f64>,
    pub estimated_request_cost: Option<f64>,
}

/// A backend row in `server-config.json:row_chain`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendServer {
    pub id: String,
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub api_key: Option<String>,
    pub enabled: bool,
    /// Relative traffic share when multiple healthy backends are enabled
    /// (weighted round-robin). Defaults to 1 = equal shares.
    #[serde(default = "default_backend_weight")]
    pub weight: u32,
}

fn default_backend_weight() -> u32 {
    1
}

/// Identifier for which request-handling pool a record pertains to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Pool {
    Orchestrator,
    CodingAgent,
}

impl Pool {
    pub fn as_str(self) -> &'static str {
        match self {
            Pool::Orchestrator => "orchestrator",
            Pool::CodingAgent => "coding_agent",
        }
    }
}

/// Persistent per-request record used to file metrics to JSONL.
#[derive(Debug, Clone, Serialize)]
pub struct MetricRecord {
    pub timestamp: String,
    pub pool: Pool,
    pub intent: &'static str,
    pub target_port: u16,
    pub success: bool,
    pub latency_ms: f64,
    pub queue_wait_ms: f64,
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub time_to_first_token_ms: Option<f64>,
    pub error_category: Option<String>,
}

/// Telemetry frame: returned by /api/dashboard_timeseries for the omega lane.
/// Generation / prompt token rates are `None` for the omega lane until the
/// upstream `llama-server` response is parsed end-to-end; today's pipeline
/// only has latency + success, so we surface that honestly instead of zero.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryFrame {
    pub timestamp: String,
    pub generation_tokens_per_second: Option<f64>,
    pub prompt_tokens_per_second: Option<f64>,
    pub total_latency_ms: f64,
    pub time_to_first_token_ms: Option<f64>,
    pub success: bool,
}

/// Per-lane status block embedded in `PublicStatus.lanes`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LaneStatus {
    pub label: String,
    pub model: String,
    pub ctx: u32,
    pub available: bool,
    pub ready: bool,
    pub lane_state: String,
    pub total_requests: u64,
    pub success_rate: f64,
    pub total_prompt_tokens: u64,
    pub total_completion_tokens: u64,
    pub quickest_generation_tokens_per_second: f64,
    pub slowest_generation_tokens_per_second: f64,
    pub latest_generation_tokens_per_second: f64,
    pub avg_generation_tokens_per_second: f64,
    pub avg_prompt_tokens_per_second: f64,
    pub avg_total_latency_ms: f64,
    pub last_time_to_first_token_ms: Option<f64>,
    pub avg_time_to_first_token_ms: Option<f64>,
    pub last_request_time: Option<String>,
    pub last_failure_code: Option<String>,
    pub reason_message: Option<String>,
}

impl LaneStatus {
    /// Construct an offline placeholder. Used when a lane is disabled or unknown.
    pub fn placeholder(label: &str) -> Self {
        Self {
            label: label.to_owned(),
            model: "—".to_owned(),
            ctx: 0,
            available: false,
            ready: false,
            lane_state: "offline".to_owned(),
            total_requests: 0,
            success_rate: 0.0,
            total_prompt_tokens: 0,
            total_completion_tokens: 0,
            quickest_generation_tokens_per_second: 0.0,
            slowest_generation_tokens_per_second: 0.0,
            latest_generation_tokens_per_second: 0.0,
            avg_generation_tokens_per_second: 0.0,
            avg_prompt_tokens_per_second: 0.0,
            avg_total_latency_ms: 0.0,
            last_time_to_first_token_ms: None,
            avg_time_to_first_token_ms: None,
            last_request_time: None,
            last_failure_code: None,
            reason_message: None,
        }
    }
}

/// Top-level /api/public_status body. Shape matches the Vite/Phaser frontend contract.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublicStatus {
    pub uptime_seconds: f64,
    pub llama_server_available: bool,
    pub degraded: bool,
    pub queue: QueueStatus,
    pub lanes: Lanes,
    pub all_ready: bool,
    pub orchestrator_pool: PoolSnapshot,
    pub coding_agent_pool: PoolSnapshot,
    /// How many 1.2B Coding Agent lanes are currently alive right now.
    #[serde(default)]
    pub lanes_in_use: u32,
    /// Maximum concurrent 1.2B Coding Agent lanes (capacity).
    #[serde(default)]
    pub lanes_capacity: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueueStatus {
    pub depth: usize,
    pub limit: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PoolSnapshot {
    pub ports_total: usize,
    pub ports_active: usize,
    pub baseline_alive: bool,
    pub extras_active: Vec<u16>,
    /// Allocated ports with no live instance right now.
    #[serde(default)]
    pub free_ports: Vec<u16>,
    pub queue_depth: usize,
    pub queue_limit: usize,
    #[serde(default)]
    pub last_failure_reason: Option<String>,
}

/// 3-lane object keyed by omega/beta/delta for the frontend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lanes {
    pub omega: LaneStatus,
    pub beta: LaneStatus,
    pub delta: LaneStatus,
}

/// Body emitted by /api/public_metrics and /api/dashboard_timeseries.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublicMetrics {
    pub uptime_seconds: f64,
    pub summaries: Lanes,
    pub active_requests: usize,
    pub requests_last_5m: usize,
    pub total_events: usize,
    pub recent_events: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimeseriesResponse {
    pub omega: Vec<TelemetryFrame>,
    /// Peer-lane frames (populated on the master by `peer_telemetry.rs`;
    /// empty on slaves and in older responses).
    #[serde(default)]
    pub beta: Vec<TelemetryFrame>,
    #[serde(default)]
    pub delta: Vec<TelemetryFrame>,
    pub events: Vec<TimeseriesEvent>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimeseriesEvent {
    pub event: String,
}

/// Errors that may surface from the demand pool's spawn sequence.
#[derive(Debug)]
pub enum DemandSpawnError {
    /// No GGUF model path was supplied.
    MissingModel,
    /// No free port in `ports_extra` (full but not at retry threshold).
    NoPortsAvailable,
    /// Could not find the `llama-server` binary (config/env/PATH all miss).
    BinaryNotFound,
    /// `tokio::process::Command::spawn` itself failed.
    CommanderSpawn(std::io::Error),
    /// `/health` did not pass after the configured attempts.
    HealthGaveUp(String),
}

impl std::fmt::Display for DemandSpawnError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingModel => write!(f, "no GGUF model path supplied to demand pool"),
            Self::NoPortsAvailable => write!(f, "no free port in the demand-pool list"),
            Self::BinaryNotFound => write!(f, "llama-server binary could not be located"),
            Self::CommanderSpawn(err) => write!(f, "failed to spawn child process: {err}"),
            Self::HealthGaveUp(s) => write!(f, "child /health did not pass: {s}"),
        }
    }
}

impl std::error::Error for DemandSpawnError {}

/// Errors that may surface when acquiring a pool slot through the queue.
#[derive(Debug)]
pub enum DemandAcquireError {
    /// Queue is full AND spawn attempts are exhausted: caller should give up.
    PoolExhausted,
    /// All slots timeout-aged out before this caller made it to the head.
    QueueAgedOut,
}

impl std::fmt::Display for DemandAcquireError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::PoolExhausted => write!(f, "pool exhausted; spawn retries exceeded"),
            Self::QueueAgedOut => write!(f, "queue head timed out before the slot was reached"),
        }
    }
}

impl std::error::Error for DemandAcquireError {}

/// Error returned by `proxy::stream_chat` if the upstream request couldn't be
/// fulfilled at all (e.g. refused connection on every port in the row chain).
#[derive(Debug)]
pub enum ProxyError {
    Connection(String),
    Status(u16),
    Decode(String),
    NoneAvailable,
}

impl std::fmt::Display for ProxyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Connection(s) => write!(f, "upstream connection refused: {s}"),
            Self::Status(code) => write!(f, "upstream returned HTTP {code}"),
            Self::Decode(s) => write!(f, "upstream returned malformed JSON: {s}"),
            Self::NoneAvailable => write!(f, "no upstream lane was available"),
        }
    }
}

impl std::error::Error for ProxyError {}

/// Errors that may come back from the row-chain walker.
#[derive(Debug)]
pub enum RouterError {
    /// Every backend in the chain either failed or was disabled.
    ChainExhausted,
}

impl std::fmt::Display for RouterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ChainExhausted => {
                write!(f, "every backend in the row chain failed or was disabled")
            }
        }
    }
}

impl std::error::Error for RouterError {}

#[cfg(test)]
mod sanitizer_tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn public_name_strips_directory_and_gguf_only() {
        let path = PathBuf::from(
            "/home/opc/Projects/ashat-master-coding-agent/models/LFM2.5-1.2B-Instruct-Q4_K_M.gguf",
        );
        assert_eq!(public_model_name(&path), "LFM2.5-1.2B-Instruct-Q4_K_M");
    }

    #[test]
    fn versioned_names_keep_internal_dots() {
        // Regression: `file_stem()` would chop at the LAST dot, mangling
        // `LFM2.5-1.2B-Instruct-Q4_K_M` down to `LFM2.5-1`. Only known
        // model-file extensions may be stripped.
        assert_eq!(
            sanitize_model_name("LFM2.5-1.2B-Instruct-Q4_K_M"),
            "LFM2.5-1.2B-Instruct-Q4_K_M"
        );
        assert_eq!(
            sanitize_model_name("/models/LFM2.5-1.2B-Instruct-Q4_K_M.gguf"),
            "LFM2.5-1.2B-Instruct-Q4_K_M"
        );
        assert_eq!(sanitize_model_name("model.bin"), "model");
        assert_eq!(sanitize_model_name("—"), "—");
    }
}
