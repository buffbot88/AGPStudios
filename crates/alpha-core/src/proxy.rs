use std::time::Duration;

use alpha_common::ChatRequest;

async fn stream_request(url: &str, payload: serde_json::Value) -> anyhow::Result<reqwest::Response> {
    let response = reqwest::Client::builder()
        .timeout(Duration::from_secs(120))
        .build()?
        .post(url)
        .json(&payload)
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow::anyhow!("worker returned {}", response.status()));
    }
    Ok(response)
}

pub async fn vision_stream(endpoint: &str, request: &ChatRequest) -> anyhow::Result<reqwest::Response> {
    match stream_request(&format!("{}/v1/chat/completions", endpoint.trim_end_matches('/')), serde_json::json!({
        "model": "local", "messages": request.messages, "max_tokens": request.max_tokens.min(4096),
        "temperature": request.temperature, "stream": true,
    })).await {
        Ok(response) => Ok(response),
        Err(error) => Err(error)
    }
}

pub async fn vision_completions(endpoint: &str, request: &ChatRequest) -> anyhow::Result<serde_json::Value> {
    let response = reqwest::Client::builder().timeout(Duration::from_secs(120)).build()?
        .post(format!("{}/v1/chat/completions", endpoint.trim_end_matches('/')))
        .json(&serde_json::json!({
            "model": "local", "messages": request.messages, "max_tokens": request.max_tokens.min(4096),
            "temperature": request.temperature, "stream": false,
        })).send().await;
    let result = match response {
        Ok(response) => { let status = response.status(); let body = response.json().await?; if status.is_success() { Ok(body) } else { Err(anyhow::anyhow!("VL returned {status}: {body}")) } }
        Err(error) => Err(error.into()),
    };
    result
}
