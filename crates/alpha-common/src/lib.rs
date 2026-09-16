//! Shared config & request types for Ashat AI.

#[derive(serde::Deserialize, Clone)]
pub struct Config {
    pub server: ServerConfig,
    pub models: ModelsConfig,
    pub queue: QueueConfig,
    pub pool: PoolConfig,
    pub liquid: LiquidConfig,
    #[serde(default)]
    pub vision: VisionConfig,
}

#[derive(serde::Deserialize, Clone)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
}

#[derive(serde::Deserialize, Clone)]
pub struct ModelsConfig {
    pub local_model: String,
    pub mmproj: String,
    pub max_instances: u32,
    pub cpu_cap: u32,
}

#[derive(serde::Deserialize, Clone)]
pub struct LiquidConfig {
    pub endpoint: String,
    pub model: String,
}

fn default_true() -> bool {
    true
}

/// On-demand vision pool (450M VL). Only started when an image
/// needs to be read. Auto-stops after idle timeout.
#[derive(serde::Deserialize, Clone)]
pub struct VisionConfig {
    pub endpoint: String,
    pub idle_timeout_secs: u64,
    #[serde(default = "default_true")]
    pub on_demand: bool,
}

impl Default for VisionConfig {
    fn default() -> Self {
        Self {
            endpoint: "http://127.0.0.1:19000".into(),
            idle_timeout_secs: 300,
            on_demand: true,
        }
    }
}

#[derive(serde::Deserialize, Clone)]
pub struct QueueConfig {
    pub max_requests: usize,
    pub max_concurrent: usize,
}

#[derive(serde::Deserialize, Clone)]
pub struct PoolConfig {
    pub min_instances: u32,
    pub port_base: u16,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: MessageContent,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCall>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct ToolCall {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub function: ToolCallFunction,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct ToolCallFunction {
    pub name: String,
    pub arguments: String,
}

/// Message content: plain text, or OpenAI-style multimodal parts
/// (text + image_url) so the VL model can see images.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(untagged)]
pub enum MessageContent {
    Text(String),
    Parts(Vec<ContentPart>),
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ContentPart {
    Text { text: String },
    ImageUrl { image_url: ImageUrl },
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct ImageUrl {
    pub url: String,
}

impl ChatMessage {
    /// Joined plain text of a message (image parts contribute nothing).
    pub fn text(&self) -> String {
        match &self.content {
            MessageContent::Text(t) => t.clone(),
            MessageContent::Parts(parts) => parts
                .iter()
                .filter_map(|p| match p {
                    ContentPart::Text { text } => Some(text.clone()),
                    _ => None,
                })
                .collect::<Vec<_>>()
                .join(" "),
        }
    }

    /// Returns true if this message contains image parts.
    pub fn has_image(&self) -> bool {
        match &self.content {
            MessageContent::Text(_) => false,
            MessageContent::Parts(parts) => parts
                .iter()
                .any(|p| matches!(p, ContentPart::ImageUrl { .. })),
        }
    }
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct AgentMessage {
    pub role: String,
    pub content: serde_json::Value,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct AgentCapabilities {
    pub tools: bool,
    pub vision: bool,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct AgentRequest {
    pub conversation_id: String,
    #[serde(default)]
    pub project_id: Option<String>,
    pub messages: Vec<AgentMessage>,
    #[serde(default)]
    pub operation: Option<AgentOperation>,
    pub capabilities: AgentCapabilities,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentOperation {
    Chat,
    Agent,
    Vision,
}

#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "type")]
pub enum AgentEvent {
    #[serde(rename = "response.start")]
    ResponseStart { response_id: String },
    #[serde(rename = "text.delta")]
    TextDelta { delta: String },
    #[serde(rename = "reasoning.delta")]
    ReasoningDelta { delta: String },
    #[serde(rename = "tool.start")]
    ToolStart { id: String, name: String },
    #[serde(rename = "tool.arguments")]
    ToolArguments { id: String, arguments: serde_json::Value },
    #[serde(rename = "tool.result")]
    ToolResult { id: String, ok: bool, result: Option<serde_json::Value>, error: Option<ToolError> },
    #[serde(rename = "status")]
    Status { state: String, message: Option<String> },
    #[serde(rename = "error")]
    Error { code: String, message: String, retryable: bool },
    #[serde(rename = "response.complete")]
    ResponseComplete,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ToolError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, serde::Deserialize)]
pub struct ChatRequest {
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub stream: bool,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
    #[serde(default = "default_temperature")]
    pub temperature: f32,
    #[serde(default)]
    pub tools: Option<Vec<serde_json::Value>>,
}

fn default_max_tokens() -> u32 {
    1024
}

fn default_temperature() -> f32 {
    0.7
}
