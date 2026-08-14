//! Ashat skills database — HTTP-backed skill lookup.
//!
//! The coding agent skill tool queries the Alpha servers HTTP API:
//! GET /api/skills?name=<name>. No MySQL dependency needed.

use omega_common::config::SkillsSection;

#[derive(Debug, Clone)]
pub struct SkillDb {
    enabled: bool,
    host: String,
    port: u16,
}

impl SkillDb {
    pub fn from_config(cfg: &SkillsSection) -> Self {
        Self {
            enabled: cfg.enabled,
            host: cfg.host.clone(),
            port: cfg.port,
        }
    }

    pub fn disabled() -> Self {
        Self {
            enabled: false,
            host: String::new(),
            port: 0,
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    pub async fn lookup(&self, name: &str) -> Result<Option<String>, String> {
        if !self.enabled {
            return Err("skills database disabled in server-config.json".to_owned());
        }

        let base = if self.port == 80 || self.port == 443 {
            format!("https://{}", self.host)
        } else {
            format!("http://{}:{}", self.host, self.port)
        };
        let endpoint = format!("{}/api/skills", base.trim_end_matches('/'));
        let url = reqwest::Url::parse_with_params(&endpoint, [("name", name)])
            .map_err(|e| format!("skills API URL invalid: {e}"))?;

        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|e| format!("HTTP client init failed: {e}"))?;

        let resp = client
            .get(url)
            .send()
            .await
            .map_err(|e| format!("skills API request failed: {e}"))?;

        let status = resp.status();
        let body = resp
            .text()
            .await
            .map_err(|e| format!("skills API read failed: {e}"))?;

        if status == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        if !status.is_success() {
            return Err(format!("skills API returned status {status}: {body}"));
        }

        let v: serde_json::Value =
            serde_json::from_str(&body).map_err(|e| format!("skills API JSON parse error: {e}"))?;

        v.get("skill")
            .and_then(|s| s.get("content"))
            .and_then(|c| c.as_str())
            .map(|s| s.to_owned())
            .ok_or_else(|| format!("unexpected skills API response: {body}"))
            .map(Some)
    }
}
