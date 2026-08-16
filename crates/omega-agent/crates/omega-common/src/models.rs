//! Discovers GGUF files under the project's `models/` directory.
//!
//! If `orchestrator_hint` / `inference_hint` resolve cleanly, use those exact
//! filenames. If not, fall back to heuristics:
//! - orchestrator = smallest *instruct-capable* GGUF (name contains `Instruct` or `350M`. The 350M text model is the
//!   always-on intent router; vision models are not used for text routing.
//! - inference = first GGUF whose name contains `1.2B` and `Instruct`

use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize)]
pub struct ModelSet {
    pub discovered: Vec<DiscoveredModel>,
    pub orchestrator_path: PathBuf,
    pub inference_path: PathBuf,
    pub orchestrator_label: String,
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
    pub fn discover(
        dir: &Path,
        orchestrator_hint: Option<&str>,
        inference_hint: Option<&str>,
    ) -> ModelSet {
        let entries: Vec<DiscoveredModel> = match std::fs::read_dir(dir) {
            Ok(rd) => rd
                .filter_map(Result::ok)
                .filter_map(|entry| {
                    let path = entry.path();
                    if path.extension().and_then(|s| s.to_str()) != Some("gguf") {
                        return None;
                    }
                    let metadata = entry.metadata().ok()?;
                    Some(DiscoveredModel {
                        file_name: path.file_name()?.to_str()?.to_owned(),
                        bytes: metadata.len(),
                        model_name: path.file_name()?.to_str()?.to_owned(),
                    })
                })
                .collect(),
            Err(_) => Vec::new(),
        };

        let orchestrator_path = pick(dir, &entries, orchestrator_hint, |e| {
            let instruct_capable = e.file_name.contains("Instruct") || e.file_name.contains("350M");
            // Text-router-capable models sort first; among them, the smallest wins.
            (
                !instruct_capable,
                parse_quant_size_bytes(&e.file_name).unwrap_or(u64::MAX),
            )
        })
        .unwrap_or_else(|| dir.join("LFM2.5-350M-Q4_K_M.gguf"));

        let inference_path = pick(dir, &entries, inference_hint, |e| {
            if e.file_name.contains("1.2B") && e.file_name.contains("Instruct") {
                e.bytes
            } else {
                u64::MAX
            }
        })
        .unwrap_or_else(|| dir.join("LFM2.5-1.2B-Instruct-Q4_K_M.gguf"));

        ModelSet {
            discovered: entries,
            orchestrator_label: orchestrator_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("orchestrator.gguf")
                .to_owned(),
            inference_label: inference_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("inference.gguf")
                .to_owned(),
            orchestrator_path,
            inference_path,
        }
    }
}

fn pick<C: Ord, F: Fn(&DiscoveredModel) -> C>(
    dir: &Path,
    entries: &[DiscoveredModel],
    hint: Option<&str>,
    cost: F,
) -> Option<PathBuf> {
    if let Some(name) = hint {
        let found = entries.iter().any(|e| e.file_name == name);
        if found {
            return Some(dir.join(name));
        }
        tracing::warn!(
            hint = name,
            "config hint didn't match any GGUF in models/, falling back to heuristics"
        );
    }
    entries
        .iter()
        .min_by_key(|e| cost(e))
        .map(|e| dir.join(&e.file_name))
}

/// Pull the size token (e.g. "230M", "1.2B") out of a GGUF filename and
/// return an approximate byte size, so smaller orchestrator models sort first.
fn parse_quant_size_bytes(name: &str) -> Option<u64> {
    let stem = name.strip_suffix(".gguf")?;
    let mut chunks = stem.rsplit('-').peekable();
    let quant = chunks.next()?;
    let size_token = chunks.next()?;
    let approx_params = match size_token {
        "230M" | "250M" => 230_000_000_u64,
        "350M" | "360M" => 350_000_000_u64,
        "450M" | "500M" => 450_000_000_u64,
        "700M" | "750M" => 700_000_000_u64,
        "1B" | "1.0B" => 1_000_000_000_u64,
        "1.2B" => 1_200_000_000_u64,
        "3B" => 3_000_000_000_u64,
        "7B" | "8B" => 7_000_000_000_u64,
        _ => return None,
    };
    let approx_bytes = match quant {
        "Q2_K" | "Q2_K_M" => approx_params / 5,
        "Q3_K_M" | "Q3_K_S" => approx_params / 4,
        "Q4_0" | "Q4_K_M" | "Q4_K_S" => approx_params / 3,
        "Q5_0" | "Q5_K_M" | "Q5_K_S" => approx_params / 2,
        "Q6_K" => (approx_params as f64 * 0.65) as u64,
        "Q8_0" | "Q8_K" => approx_params,
        _ => return None,
    };
    Some(approx_bytes)
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
    fn orchestrator_fallback_prefers_instruct_capable_small_model() {
        with_model_dir(|dir| {
            touch(dir, "LFM2.5-230M-Q4_K_M.gguf");
            touch(dir, "LFM2.5-350M-Q4_K_M.gguf");
            touch(dir, "LFM2.5-1.2B-Instruct-Q4_K_M.gguf");
            let set = ResolvedModels::discover(dir, None, None);
            let orch = set.orchestrator_path.file_name().unwrap().to_str().unwrap();
            assert!(orch.contains("350M"), "expected 350M router, got {orch}");
            assert!(set.inference_label.contains("1.2B-Instruct"));
        });
    }

    #[test]
    fn hint_overrides_fallback_heuristics() {
        with_model_dir(|dir| {
            touch(dir, "LFM2.5-230M-Q4_K_M.gguf");
            touch(dir, "LFM2.5-VL-450M-Q4_K_M.gguf");
            let set = ResolvedModels::discover(dir, Some("LFM2.5-230M-Q4_K_M.gguf"), None);
            assert!(set.orchestrator_label.contains("230M"));
        });
    }
}
