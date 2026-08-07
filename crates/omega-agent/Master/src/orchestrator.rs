//! 230M Orchestrator: owns the intent-classifier call into the local `llama-server`
//! carrying the smallest GGUF in `models/`. Always-on baseline port + spawn-on-demand
//! extras — both wrapped by an `Arc<DemandPool>`.

use crate::demand::{DemandPool, InstanceGuard};
use crate::metrics::MetricsStore;
use crate::types::{ChatMessage, Intent};
use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use tracing::{info, warn};

pub struct Orchestrator {
    pool: Arc<DemandPool>,
    timeout: Duration,
}

impl Orchestrator {
    pub fn new(pool: Arc<DemandPool>, timeout: Duration) -> Self {
        Self { pool, timeout }
    }    /// Acquire a slot (baseline-claim or extra-spawn) and run classification.
    pub async fn classify(
        &self,
        messages: &[ChatMessage],
        metrics: &Arc<MetricsStore>,
    ) -> (Intent, InstanceGuard) {
        let pool = Arc::clone(&self.pool);
        let guard = match pool.clone().acquire(metrics, self.timeout).await {
            Ok(g) => g,
            Err(err) => {
                metrics.event(format!(
                    "orchestrator acquire failed; defaulting to Unknown: {err}"
                ));
                // Degraded-mode last-resort: hand back a baseline guard. The
                // baseline child is supervised alive by `supervision.rs`, so
                // the upstream `/v1/chat/completions` call will still go
                // through and return either a real classification or an
                // upstream error; either way the caller gets a guard that
                // references the right port.
                InstanceGuard::baseline(
                    Arc::clone(&pool),
                    pool.baseline_port.unwrap_or(0),
                )
            }
        };

        let last_user = messages
            .iter()
            .rev()
            .find(|m| m.role == "user")
            .map(|m| m.content.clone())
            .unwrap_or_default();

        let request = json!({
            "model": pool.spec.model.file_name().and_then(|s| s.to_str()).unwrap_or("orchestrator"),
            "messages": [
                {
                    "role": "system",
                    "content": "You are an intent classifier for a chat server. Respond with EXACTLY one lowercase word: chat, status, or unknown. No explanation, no punctuation."
                },
                {"role": "user", "content": build_prompt(&last_user)}
            ],
            "max_tokens": 8,
            "temperature": 0.0,
            "stream": false
        });

        let url = format!(
            "http://{}:{}/v1/chat/completions",
            pool.spec.host,
            guard.port()
        );
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(20))
            .build()
            .expect("reqwest client");

        let response = client.post(&url).json(&request).send().await;

        let intent = match response {
            Ok(r) if r.status().is_success() => match r.json::<Value>().await {
                Ok(v) => parse_intent(&v),
                Err(err) => {
                    warn!("orchestrator response parse failed: {err}");
                    Intent::Unknown
                }
            },
            Ok(r) => {
                warn!(
                    status = r.status().as_u16(),
                    "orchestrator returned non-success"
                );
                Intent::Unknown
            }
            Err(err) => {
                warn!("orchestrator request failed: {err}");
                Intent::Unknown
            }
        };

        info!(
            intent = intent.as_str(),
            port = guard.port(),
            baseline = guard.is_baseline(),
            "classified intent"
        );
        (intent, guard)
    }
}

fn build_prompt(last_user_message: &str) -> String {
    let truncated: String = last_user_message.chars().take(200).collect();
    format!(
        "The user said: \"{truncated}\"\n\nRespond with EXACTLY one lowercase word: chat, status, or unknown."
    )
}

fn parse_intent(json: &Value) -> Intent {
    let text = json
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    let first = text.split_whitespace().next().unwrap_or("");
    match first {
        "chat" => Intent::Chat,
        "status" => Intent::Status,
        "code" => Intent::Code,
        _ => Intent::Unknown,
    }
}
