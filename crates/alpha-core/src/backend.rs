use std::{future::Future, pin::Pin, time::Duration};

use alpha_common::{ChatRequest, LiquidConfig};

pub type ChatStream = reqwest::Response;
pub type ChatResult<'a> = Pin<Box<dyn Future<Output = anyhow::Result<ChatStream>> + Send + 'a>>;

pub trait ChatBackend: Send + Sync {
    fn stream<'a>(&'a self, request: &'a ChatRequest) -> ChatResult<'a>;
}

pub struct LiquidBackend {
    client: reqwest::Client,
    config: LiquidConfig,
}

impl LiquidBackend {
    pub fn new(config: LiquidConfig) -> anyhow::Result<Self> {
        Ok(Self {
            client: reqwest::Client::builder().timeout(Duration::from_secs(120)).build()?,
            config,
        })
    }

    pub async fn health_check(&self) -> bool {
        let endpoint = format!("{}/health", self.config.endpoint.trim_end_matches('/'));
        self.client.get(endpoint).send().await.map(|response| response.status().is_success()).unwrap_or(false)
    }

    async fn request(&self, request: &ChatRequest) -> anyhow::Result<ChatStream> {
        let mut messages = request.messages.clone();
        if let Some(tools) = &request.tools {
            let definitions = tools.iter()
                .map(|tool| tool.get("function").cloned().unwrap_or_else(|| tool.clone()))
                .collect::<Vec<_>>();
            let definitions = serde_json::to_string(&definitions)?;
            let prompt = format!("List of tools: {definitions}\nOutput function calls as JSON.");
            if let Some(system) = messages.iter_mut().find(|message| message.role == "system") {
                if let alpha_common::MessageContent::Text(content) = &mut system.content {
                    content.push('\n');
                    content.push_str(&prompt);
                }
            } else {
                messages.insert(0, alpha_common::ChatMessage {
                    role: "system".into(),
                    content: alpha_common::MessageContent::Text(prompt),
                    tool_calls: None,
                    tool_call_id: None,
                });
            }
        }
        let response = self.client
            .post(format!("{}/v1/chat/completions", self.config.endpoint.trim_end_matches('/')))
            .json(&serde_json::json!({
                "model": self.config.model,
                "messages": messages,
                "max_tokens": request.max_tokens.max(1024),
                "temperature": request.temperature,
                "stream": true,
                "tools": serde_json::Value::Null,
                "tool_choice": "none",
            }))
            .send()
            .await?;
        if !response.status().is_success() {
            return Err(anyhow::anyhow!("Liquid backend returned {}", response.status()));
        }
        Ok(response)
    }
}

impl ChatBackend for LiquidBackend {
    fn stream<'a>(&'a self, request: &'a ChatRequest) -> ChatResult<'a> {
        Box::pin(self.request(request))
    }
}
