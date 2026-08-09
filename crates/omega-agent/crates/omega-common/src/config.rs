//! Loads `server-config.json` and overlays environment variables.
//!
//! Resolves the `llama-server` binary location in priority order:
//! 1. `server.orchestrator_binary_default` in `server-config.json`
//! 2. `ASHAT_LLAMA_BIN` environment variable
//! 3. Plain `PATH` lookup

use crate::models::ResolvedModels;
use crate::types::BackendServer;
use serde::Deserialize;
use std::{env, path::PathBuf};

#[derive(Debug, Clone, Deserialize)]
pub struct ServerSection {
    pub bind: String,
    #[serde(default = "default_binary")]
    pub orchestrator_binary_default: String,
}

fn default_binary() -> String {
    "llama-server".to_owned()
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelsSection {
    #[serde(default = "default_models_dir")]
    pub dir: String,
    #[serde(default)]
    pub orchestrator_hint: Option<String>,
    #[serde(default)]
    pub inference_hint: Option<String>,
}

fn default_models_dir() -> String {
    "models".to_owned()
}

#[derive(Debug, Clone, Deserialize)]
pub struct InferenceSection {
    pub context: u32,
    pub timeout_seconds: u64,
    #[serde(default = "default_threads")]
    pub llama_threads: u32,
    #[serde(default)]
    pub llama_gpu_layers: u32,
    /// Pin the model in RAM (`--mlock`). More memory resident, fewer page-fault
    /// stalls: the "more RAM, less CPU per instance" tuning lever.
    #[serde(default)]
    pub llama_mlock: bool,
    /// Default per-request completion token budget.
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
}

fn default_max_tokens() -> u32 {
    1024
}

fn default_threads() -> u32 {
    2
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrchestratorPoolSection {
    pub ports_baseline: Vec<u16>,
    #[serde(default)]
    pub ports_extra: Vec<u16>,
    #[serde(default = "default_queue_max")]
    pub queue_max: usize,
    #[serde(default = "default_spawn_attempts")]
    pub spawn_attempts_before_503: u32,
}

fn default_queue_max() -> usize {
    32
}

fn default_spawn_attempts() -> u32 {
    3
}

#[derive(Debug, Clone, Deserialize)]
pub struct CodingAgentPoolSection {
    pub ports: Vec<u16>,
    #[serde(default = "default_queue_max")]
    pub queue_max: usize,
    #[serde(default = "default_spawn_attempts")]
    pub spawn_attempts_before_503: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MetricsSection {
    #[serde(default = "default_metrics_path")]
    pub persist_path: String,
}

fn default_metrics_path() -> String {
    "logs/metrics.jsonl".to_owned()
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct HubSection {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub url: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WorkspaceSection {
    #[serde(default = "default_workspace_dir")]
    pub dir: String,
}

impl Default for WorkspaceSection {
    fn default() -> Self {
        Self {
            dir: default_workspace_dir(),
        }
    }
}

fn default_workspace_dir() -> String {
    "workspaces".to_owned()
}

#[derive(Debug, Clone, Deserialize)]
pub struct ToolLoopSection {
    #[serde(default = "default_tool_max_iterations")]
    pub max_iterations: usize,
    #[serde(default = "default_tool_command_timeout")]
    pub command_timeout_seconds: u64,
    #[serde(default = "default_tool_output_chars")]
    pub output_max_chars: usize,
}

impl Default for ToolLoopSection {
    fn default() -> Self {
        Self {
            max_iterations: default_tool_max_iterations(),
            command_timeout_seconds: default_tool_command_timeout(),
            output_max_chars: default_tool_output_chars(),
        }
    }
}

fn default_tool_max_iterations() -> usize {
    5
}

fn default_tool_command_timeout() -> u64 {
    10
}

fn default_tool_output_chars() -> usize {
    4000
}

/// Admin update propagation: `POST /api/admin/update` runs the seed/update
/// script against every enabled peer so master builds propagate automatically.
#[derive(Debug, Clone, Deserialize)]
pub struct UpdateSection {
    #[serde(default)]
    pub peers: Vec<UpdatePeer>,
    /// Per-peer run budget before the endpoint reports the peer as timed out.
    #[serde(default = "default_update_timeout")]
    pub timeout_seconds: u64,
}

impl Default for UpdateSection {
    fn default() -> Self {
        Self {
            peers: Vec::new(),
            timeout_seconds: default_update_timeout(),
        }
    }
}

fn default_update_timeout() -> u64 {
    600
}

#[derive(Debug, Clone, Deserialize)]
pub struct UpdatePeer {
    /// SSH target for `scripts/seed_slave.sh` (e.g. `opc@150.136.208.93`).
    pub host: String,
    #[serde(default = "default_update_install")]
    pub install: String,
    /// Slave bind port (also the row_chain port the master routes to).
    #[serde(default = "default_update_port")]
    pub port: u16,
    #[serde(default)]
    pub enabled: bool,
}

fn default_update_install() -> String {
    "/home/opc/Projects/ashatneuralhost-slave".to_owned()
}

fn default_update_port() -> u16 {
    8082
}

/// Ashat's MySQL skills database. Implemented but inert until connection
/// details are provided (`skills_db.enabled` is `false` by default).
#[derive(Debug, Clone, Deserialize)]
pub struct SkillsSection {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_skills_host")]
    pub host: String,
    #[serde(default = "default_skills_port")]
    pub port: u16,
    #[serde(default)]
    pub database: String,
    #[serde(default)]
    pub user: String,
    #[serde(default)]
    pub password: String,
}

impl Default for SkillsSection {
    fn default() -> Self {
        Self {
            enabled: false,
            host: default_skills_host(),
            port: default_skills_port(),
            database: String::new(),
            user: String::new(),
            password: String::new(),
        }
    }
}

fn default_skills_host() -> String {
    "127.0.0.1".to_owned()
}

fn default_skills_port() -> u16 {
    3306
}

#[derive(Debug, Clone, Deserialize)]
pub struct FileConfig {
    #[serde(rename = "ASHAT_KEY")]
    pub ash_key: String,
    /// Optional privileged key for admin routes (`POST /api/admin/update`).
    /// Overridden by `ASHAT_ADMIN_KEY`; when neither is set, admin routes
    /// fall back to the shared `ASHAT_KEY`.
    #[serde(default)]
    pub admin_key: Option<String>,
    pub server: ServerSection,
    pub models: ModelsSection,
    pub inference: InferenceSection,
    pub orchestrator_pool: OrchestratorPoolSection,
    pub coding_agent_pool: CodingAgentPoolSection,
    pub row_chain: Vec<BackendServer>,
    pub metrics: MetricsSection,
    #[serde(default)]
    pub hub: HubSection,
    #[serde(default)]
    pub workspace: WorkspaceSection,
    #[serde(default)]
    pub tool_loop: ToolLoopSection,
    #[serde(default)]
    pub skills_db: SkillsSection,
    #[serde(default)]
    pub update: UpdateSection,
}

/// Resolved, post-env-overlay configuration. This is what the rest of the server sees.
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// Bound address for the public axum server (`0.0.0.0:8080` by default).
    pub bind: String,
    /// Single shared auth key. Read from `server-config.json` only — no env override.
    pub ash_key: String,
    /// Privileged admin key for `POST /api/admin/update`. `ASHAT_ADMIN_KEY`
    /// env wins; falls back to `ash_key` when unset.
    pub admin_key: Option<String>,
    /// Resolved path to the `llama-server` binary.
    pub llama_binary: PathBuf,
    /// Resolved orchestrator GGUF file path.
    pub orchestrator_model: PathBuf,
    /// Resolved 1.2B GGUF file path.
    pub inference_model: PathBuf,
    pub inference: InferenceSection,
    pub orchestrator_pool: OrchestratorPoolSection,
    pub coding_agent_pool: CodingAgentPoolSection,
    pub backends: Vec<BackendServer>,
    pub metrics_path: PathBuf,
    /// Ashat Hub integration (alpha_status.rs). `enabled: false` by default.
    pub hub: HubSection,
    /// Coding-agent workspace root, resolved against the project root.
    pub workspace_dir: PathBuf,
    /// Advanced coding-agent loop limits (Phase 6).
    pub tool_loop: ToolLoopSection,
    /// Ashat's MySQL skills database connection (disabled until populated).
    pub skills_db: SkillsSection,
    /// Admin update propagation peers (`POST /api/admin/update`).
    pub update: UpdateSection,
    /// Absolute project root (script paths for admin actions resolve against it).
    pub project_root: PathBuf,
}

impl AppConfig {
    pub fn load() -> Self {
        let file: FileConfig = read_json("server-config.json");
        let project_root = env::var("ASHAT_PROJECT_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
        let models_dir = project_root.join(&file.models.dir);

        let resolved = ResolvedModels::discover(
            &models_dir,
            file.models.orchestrator_hint.as_deref(),
            file.models.inference_hint.as_deref(),
        );

        let llama_name = env::var("ASHAT_LLAMA_BIN")
            .ok()
            .unwrap_or_else(|| file.server.orchestrator_binary_default.clone());
        let llama_binary = resolve_llama_binary(&llama_name);

        let bind = env::var("OMEGA_BIND").unwrap_or_else(|_| file.server.bind.clone());
        let metrics_path = env::var("OMEGA_METRICS_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| project_root.join(&file.metrics.persist_path));

        let admin_key = env::var("ASHAT_ADMIN_KEY")
            .ok()
            .filter(|k| !k.is_empty())
            .or(file.admin_key)
            .filter(|k| !k.is_empty());

        Self {
            bind,
            ash_key: file.ash_key,
            admin_key,
            llama_binary,
            orchestrator_model: resolved.orchestrator_path,
            inference_model: resolved.inference_path,
            inference: file.inference,
            orchestrator_pool: file.orchestrator_pool,
            coding_agent_pool: file.coding_agent_pool,
            backends: file.row_chain,
            metrics_path,
            hub: file.hub,
            workspace_dir: project_root.join(&file.workspace.dir),
            tool_loop: file.tool_loop,
            skills_db: file.skills_db,
            update: file.update,
            project_root,
        }
    }
}

fn read_json<T: for<'de> Deserialize<'de>>(rel_path: &str) -> T {
    let raw = std::fs::read_to_string(rel_path)
        .unwrap_or_else(|err| panic!("failed to read {rel_path}: {err}"));
    serde_json::from_str(&raw)
        .unwrap_or_else(|err| panic!("server-config.json failed to parse: {err}"))
}

fn resolve_llama_binary(name: &str) -> PathBuf {
    let candidate = PathBuf::from(name);
    if candidate.is_absolute() || candidate.components().count() > 1 {
        return candidate;
    }
    if let Ok(paths) = env::var("PATH") {
        for dir in paths.split(':') {
            let full = PathBuf::from(dir).join(name);
            if full.is_file() {
                return full;
            }
        }
    }
    candidate
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base_json(extra: &str) -> String {
        format!(
            r#"{{
              "ASHAT_KEY": "k",
              "server": {{"bind": "0.0.0.0:8080"}},
              "models": {{"dir": "models"}},
              "inference": {{"context": 4096, "timeout_seconds": 120}},
              "orchestrator_pool": {{"ports_baseline": [18079]}},
              "coding_agent_pool": {{"ports": [18080]}},
              "row_chain": [{{"id": "omega", "host": "127.0.0.1", "port": 8080, "enabled": true}}],
              "metrics": {{"persist_path": "logs/metrics.jsonl"}}"#
        ) + extra
            + "}"
    }

    #[test]
    fn missing_hub_and_workspace_default() {
        let cfg: FileConfig = serde_json::from_str(&base_json("")).expect("parse");
        assert!(!cfg.hub.enabled);
        assert_eq!(cfg.hub.url, "");
        assert_eq!(cfg.workspace.dir, "workspaces");
        assert_eq!(cfg.server.bind, "0.0.0.0:8080");
        assert_eq!(cfg.orchestrator_pool.ports_baseline, vec![18079]);
    }

    #[test]
    fn tool_loop_and_skills_db_defaults() {
        let cfg: FileConfig = serde_json::from_str(&base_json("")).expect("parse");
        assert_eq!(cfg.tool_loop.max_iterations, 5);
        assert_eq!(cfg.tool_loop.command_timeout_seconds, 10);
        assert_eq!(cfg.tool_loop.output_max_chars, 4000);
        assert!(!cfg.skills_db.enabled);
        assert_eq!(cfg.skills_db.host, "127.0.0.1");
        assert_eq!(cfg.skills_db.port, 3306);
        assert!(!cfg.inference.llama_mlock);
    }

    #[test]
    fn explicit_tool_loop_and_skills_values() {
        let json = r#"{
          "ASHAT_KEY": "k",
          "server": {"bind": "0.0.0.0:8080"},
          "models": {"dir": "models"},
          "inference": {"context": 4096, "timeout_seconds": 120, "llama_mlock": true},
          "orchestrator_pool": {"ports_baseline": [18079]},
          "coding_agent_pool": {"ports": [18080]},
          "row_chain": [],
          "metrics": {"persist_path": "logs/metrics.jsonl"},
          "tool_loop": {"max_iterations": 8, "command_timeout_seconds": 20, "output_max_chars": 6000},
          "skills_db": {"enabled": true, "host": "db.ashat.example", "database": "skills"}
        }"#;
        let cfg: FileConfig = serde_json::from_str(json).expect("parse");
        assert_eq!(cfg.tool_loop.max_iterations, 8);
        assert_eq!(cfg.tool_loop.command_timeout_seconds, 20);
        assert!(cfg.skills_db.enabled);
        assert_eq!(cfg.skills_db.host, "db.ashat.example");
        assert!(cfg.inference.llama_mlock);
    }

    #[test]
    fn explicit_hub_and_workspace_values() {
        let json = base_json(
            ",\n              \"hub\": {\"enabled\": true, \"url\": \"https://hub.ashat.example/status\"},\n              \"workspace\": {\"dir\": \"work\"}",
        );
        let cfg: FileConfig = serde_json::from_str(&json).expect("parse");
        assert!(cfg.hub.enabled);
        assert_eq!(cfg.hub.url, "https://hub.ashat.example/status");
        assert_eq!(cfg.workspace.dir, "work");
    }

    #[test]
    fn admin_key_defaults_to_none() {
        let cfg: FileConfig = serde_json::from_str(&base_json("")).expect("parse");
        assert!(cfg.admin_key.is_none());
    }

    #[test]
    fn admin_key_parses_when_present() {
        let json = base_json(",\n              \"admin_key\": \"admin-secret\"");
        let cfg: FileConfig = serde_json::from_str(&json).expect("parse");
        assert_eq!(cfg.admin_key.as_deref(), Some("admin-secret"));
    }

    #[test]
    fn backend_weight_defaults_to_one() {
        let cfg: FileConfig = serde_json::from_str(&base_json("")).expect("parse");
        assert_eq!(cfg.row_chain.len(), 1);
        assert_eq!(cfg.row_chain[0].weight, 1);
    }

    #[test]
    fn backend_weight_parses_when_present() {
        let json = r#"{
          "ASHAT_KEY": "k",
          "server": {"bind": "0.0.0.0:8080"},
          "models": {"dir": "models"},
          "inference": {"context": 4096, "timeout_seconds": 120},
          "orchestrator_pool": {"ports_baseline": [18079]},
          "coding_agent_pool": {"ports": [18080]},
          "row_chain": [
            {"id": "omega", "host": "127.0.0.1", "port": 8080, "enabled": true, "weight": 2},
            {"id": "beta", "host": "10.0.0.2", "port": 8082, "enabled": true}
          ],
          "metrics": {"persist_path": "logs/metrics.jsonl"}
        }"#;
        let cfg: FileConfig = serde_json::from_str(json).expect("parse");
        assert_eq!(cfg.row_chain.len(), 2);
        assert_eq!(cfg.row_chain[0].weight, 2);
        // Backend without a weight field keeps the serde default of 1.
        assert_eq!(cfg.row_chain[1].weight, 1);
    }

    #[test]
    fn update_section_defaults_to_empty_peers() {
        let cfg: FileConfig = serde_json::from_str(&base_json("")).expect("parse");
        assert!(cfg.update.peers.is_empty());
        assert_eq!(cfg.update.timeout_seconds, 600);
    }

    #[test]
    fn update_section_parses_peers() {
        let json = base_json(
            ",\n              \"update\": {\"timeout_seconds\": 300,\n                \"peers\": [\n                  {\"host\": \"opc@1.2.3.4\", \"install\": \"/opt/slave\", \"port\": 8088, \"enabled\": true},\n                  {\"host\": \"opc@5.6.7.8\", \"enabled\": false}\n                ]}",
        );
        let cfg: FileConfig = serde_json::from_str(&json).expect("parse");
        assert_eq!(cfg.update.timeout_seconds, 300);
        assert_eq!(cfg.update.peers.len(), 2);
        assert!(cfg.update.peers[0].enabled);
        assert_eq!(cfg.update.peers[0].port, 8088);
        assert_eq!(cfg.update.peers[0].install, "/opt/slave");
        // Second peer relies on install/port defaults.
        assert!(!cfg.update.peers[1].enabled);
        assert_eq!(
            cfg.update.peers[1].install,
            "/home/opc/Projects/ashatneuralhost-slave"
        );
        assert_eq!(cfg.update.peers[1].port, 8082);
    }
}
