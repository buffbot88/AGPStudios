//! Discovers the configured execution GGUF under `models/`.

use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize)]
pub struct ModelSet {
    pub discovered: Vec<DiscoveredModel>,
    pub inference_path: PathBuf,
    pub inference_label: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiscoveredModel {
    pub file_name: String,
    pub bytes: u64,
    pub model_name: String,
}

pub struct ResolvedModels;

impl ResolvedModels {
    pub fn discover(dir: &Path, inference_hint: Option<&str>) -> ModelSet {
        let entries: Vec<DiscoveredModel> = match std::fs::read_dir(dir) {
            Ok(rd) => rd
                .filter_map(Result::ok)
                .filter_map(|entry| {
                    let path = entry.path();
                    if path.extension().and_then(|s| s.to_str()) != Some("gguf") {
                        return None;
                    }
                    let metadata = entry.metadata().ok()?;
                    let file_name = path.file_name()?.to_str()?.to_owned();
                    Some(DiscoveredModel {
                        file_name: file_name.clone(),
                        bytes: metadata.len(),
                        model_name: file_name,
                    })
                })
                .collect(),
            Err(_) => Vec::new(),
        };

        let inference_path = pick(dir, &entries, inference_hint)
            .unwrap_or_else(|| dir.join("LFM2.5-1.2B-Instruct-Q4_K_M.gguf"));
        ModelSet {
            discovered: entries,
            inference_label: inference_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("inference.gguf")
                .to_owned(),
            inference_path,
        }
    }
}

fn pick(dir: &Path, entries: &[DiscoveredModel], hint: Option<&str>) -> Option<PathBuf> {
    if let Some(name) = hint {
        if let Some(found) = entries.iter().find(|entry| entry.file_name == name) {
            return Some(dir.join(&found.file_name));
        }
        tracing::warn!(hint = name, "configured model hint was not found");
    }
    entries
        .iter()
        .filter(|entry| entry.file_name.contains("1.2B") && entry.file_name.contains("Instruct"))
        .min_by_key(|entry| entry.bytes)
        .map(|entry| dir.join(&entry.file_name))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn touch(dir: &Path, name: &str) {
        std::fs::write(dir.join(name), b"gguf-stub").unwrap();
    }

    fn with_model_dir(f: impl FnOnce(&Path)) {
        let dir = std::env::temp_dir().join(format!("omega-models-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        f(&dir);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn execution_fallback_selects_12b_instruct_model() {
        with_model_dir(|dir| {
            touch(dir, "LFM2.5-350M-Q4_K_M.gguf");
            touch(dir, "LFM2.5-1.2B-Instruct-Q4_K_M.gguf");
            let set = ResolvedModels::discover(dir, None);
            assert!(set.inference_label.contains("1.2B-Instruct"));
        });
    }

    #[test]
    fn hint_overrides_execution_fallback() {
        with_model_dir(|dir| {
            touch(dir, "first-1.2B-Instruct.gguf");
            touch(dir, "chosen-1.2B-Instruct.gguf");
            let set = ResolvedModels::discover(dir, Some("chosen-1.2B-Instruct.gguf"));
            assert!(set.inference_label.contains("chosen"));
        });
    }
}
