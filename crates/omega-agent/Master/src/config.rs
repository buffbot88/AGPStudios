//! Loads `server-config.json` and overlays environment variables.
//!
//! Resolves the `llama-server` binary location in priority order:
//! 1. `server.orchestrator_binary_default` in `server-config.json`
//! 2. `ASHAT_LLAMA_BIN` environment variable
//! 3. Plain `PATH` lookup

use crate::models::ResolvedModels;
use crate::types::{BackendServer, RowChainEntry};
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

#[derive(Debug, Clone, Deserialize)]
pub struct FileConfig {
    #[serde(rename = "ASHAT_KEY")]
    pub ash_key: String,
    pub server: ServerSection,
    pub models: ModelsSection,
    pub inference: InferenceSection,
    pub orchestrator_pool: OrchestratorPoolSection,
    pub coding_agent_pool: CodingAgentPoolSection,
    pub row_chain: Vec<BackendServer>,
    pub metrics: MetricsSection,
}

/// Resolved, post-env-overlay configuration. This is what the rest of the server sees.
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// Bound address for the public axum server (`0.0.0.0:8080` by default).
    pub bind: String,
    /// Single shared auth key. Read from `server-config.json` only — no env override.
    pub ash_key: String,
    /// Resolved path to the `llama-server` binary.
    pub llama_binary: PathBuf,
    /// Resolved orchestrator GGUF file path.
    pub orchestrator_model: PathBuf,
    /// Resolved 1.2B GGUF file path.
    pub inference_model: PathBuf,
    pub inference: InferenceSection,
    pub orchestrator_pool: OrchestratorPoolSection,
    pub coding_agent_pool: CodingAgentPoolSection,
    pub row_chain_entries: Vec<RowChainEntry>,
    pub backends: Vec<BackendServer>,
    pub metrics_path: PathBuf,
}

impl AppConfig {
    pub fn load() -> Self {
        let file: FileConfig = read_json("server-config.json");
        let project_root = env::var("ASHAT_PROJECT_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."));
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

        let row_chain_entries = file
            .row_chain
            .iter()
            .enumerate()
            .map(|(i, b)| RowChainEntry {
                position: (i + 1) as u8,
                server_id: b.id.clone(),
            })
            .collect();

        Self {
            bind,
            ash_key: file.ash_key,
            llama_binary,
            orchestrator_model: resolved.orchestrator_path,
            inference_model: resolved.inference_path,
            inference: file.inference,
            orchestrator_pool: file.orchestrator_pool,
            coding_agent_pool: file.coding_agent_pool,
            row_chain_entries,
            backends: file.row_chain,
            metrics_path,
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
