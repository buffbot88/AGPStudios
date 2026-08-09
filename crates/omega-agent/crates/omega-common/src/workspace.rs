//! Coding-agent workspace management.
//!
//! Each spawned 1.2B coding-agent instance gets a dedicated directory under a
//! configurable root (`workspaces/agent-{port}/` by default). Incoming request
//! session logs are appended there per request; future phases seed these
//! workspaces with knowledge from the Ashat MySQL skills base before inference
//! starts, so every coding agent has the context Ashat wants it to carry.

use chrono::Utc;
use serde_json::json;
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
};

use crate::types::ChatRequest;

/// Manages per-agent working directories under a single root.
#[derive(Debug, Clone)]
pub struct AgentWorkspace {
    root: PathBuf,
}

impl AgentWorkspace {
    /// Workspace root. Callers resolve it against the project root (config
    /// `workspace.dir`, default `workspaces`).
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    /// Resolved root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Ensure `{root}/agent-{port}/` exists and return its path.
    pub fn ensure_agent_dir(&self, port: u16) -> io::Result<PathBuf> {
        let dir = self.root.join(format!("agent-{port}"));
        fs::create_dir_all(&dir)?;
        Ok(dir)
    }

    /// Append one JSON line about an incoming request to the agent's session
    /// log (`session.jsonl`). Creates the agent directory on first use.
    pub fn log_request(&self, port: u16, request: &ChatRequest, intent: &str) -> io::Result<()> {
        let dir = self.ensure_agent_dir(port)?;
        let line = json!({
            "ts": Utc::now().to_rfc3339(),
            "port": port,
            "intent": intent,
            "model": request.model,
            "messages": request.messages.len(),
            "max_tokens": request.max_tokens,
        });
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("session.jsonl"))?;
        writeln!(file, "{line}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{ChatMessage, ChatRequest};
    use std::path::{Path, PathBuf};

    fn test_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "omega-workspace-test-{}-{name}",
            std::process::id()
        ))
    }

    fn sample_request() -> ChatRequest {
        ChatRequest {
            model: Some("m".to_owned()),
            messages: vec![ChatMessage {
                role: "user".to_owned(),
                content: "hi".to_owned(),
            }],
            max_tokens: Some(16),
            temperature: None,
            top_p: None,
            stream: None,
        }
    }

    #[test]
    fn root_returns_configured_path() {
        let ws = AgentWorkspace::new(PathBuf::from("/x/y"));
        assert_eq!(ws.root(), Path::new("/x/y"));
    }

    #[test]
    fn ensure_agent_dir_creates_nested_dir() {
        let root = test_root("ensure");
        let ws = AgentWorkspace::new(root.clone());
        let dir = ws.ensure_agent_dir(18280).expect("create dir");
        assert!(dir.ends_with("agent-18280"));
        assert!(dir.is_dir());
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn log_request_appends_jsonl_line() {
        let root = test_root("log");
        let ws = AgentWorkspace::new(root.clone());
        ws.log_request(18280, &sample_request(), "chat")
            .expect("log");
        ws.log_request(18280, &sample_request(), "chat")
            .expect("log again");
        let log = std::fs::read_to_string(root.join("agent-18280/session.jsonl")).expect("read");
        assert_eq!(log.lines().count(), 2);
        let first: serde_json::Value = serde_json::from_str(log.lines().next().unwrap()).unwrap();
        assert_eq!(first["port"], 18280);
        assert_eq!(first["intent"], "chat");
        assert_eq!(first["model"], "m");
        assert_eq!(first["messages"], 1);
        assert_eq!(first["max_tokens"], 16);
        std::fs::remove_dir_all(&root).ok();
    }
}
