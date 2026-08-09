//! Intent-router Orchestrator (LFM2.5-VL-450M): owns the intent-classifier
//! call into the local `llama-server`
//! carrying the smallest GGUF in `models/`. Always-on baseline port + spawn-on-demand
//! extras — both wrapped by an `Arc<DemandPool>`.

use crate::demand::{DemandPool, InstanceGuard};
use omega_common::metrics::MetricsStore;
use omega_common::types::{ChatMessage, Intent};
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
    }
    /// Acquire a slot (baseline-claim or extra-spawn) and run classification.
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
                InstanceGuard::baseline(Arc::clone(&pool), pool.baseline_port.unwrap_or(0))
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
                    "content": "You are an intent classifier for a chat server. Respond with EXACTLY one lowercase word: chat, code, status, or unknown. No explanation, no punctuation."
                },
                {"role": "user", "content": build_prompt(&last_user)}
            ],
            "max_tokens": 16,
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

/// Few-shot intent prompt. Probing showed tiny instruct models (350M–1.7B)
/// classify reliably with a compact example set + an `Intent:` completion
/// prefix; a bare "respond with one word" instruction makes them echo the
/// user text or pick a biased label instead.
fn build_prompt(last_user_message: &str) -> String {
    let truncated: String = last_user_message.chars().take(200).collect();
    format!(
        "Classify the user request into EXACTLY one word: chat, code, status, or unknown.\n\
         Examples:\n\
         \"hello\" -> chat\n\
         \"write a python script\" -> code\n\
         \"are you alive\" -> status\n\
         \"xylophone purple\" -> unknown\n\n\
         Now classify: \"{truncated}\"\n\
         Intent:"
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_prompt_has_few_shot_and_completion_prefix() {
        let p = build_prompt("write hello.py");
        assert!(
            p.contains("write a python script\" -> code"),
            "few-shot code example"
        );
        assert!(
            p.contains("Now classify: \"write hello.py\""),
            "query embedded"
        );
        assert!(p.ends_with("Intent:"), "completion prefix");
        // Truncation guard: the embedded user text is capped at 200 chars.
        let long = "x".repeat(500);
        let plong = build_prompt(&long);
        assert!(plong.contains(&"x".repeat(200)), "query truncated to 200");
        assert!(!plong.contains(&"x".repeat(201)), "no untruncated tail");
    }

    #[test]
    fn parse_intent_maps_labels() {
        let resp = |content: &str| json!({ "choices": [{ "message": { "content": content } }] });
        assert_eq!(parse_intent(&resp("code")), Intent::Code);
        assert_eq!(parse_intent(&resp("chat")), Intent::Chat);
        assert_eq!(parse_intent(&resp("status")), Intent::Status);
        assert_eq!(parse_intent(&resp("unknown")), Intent::Unknown);
        // Stray prose or blanks fall back to Unknown (safe routing).
        assert_eq!(parse_intent(&resp("tell")), Intent::Unknown);
        assert_eq!(parse_intent(&resp("")), Intent::Unknown);
        assert_eq!(parse_intent(&json!({})), Intent::Unknown);
    }
}
