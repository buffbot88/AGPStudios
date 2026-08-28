//! Advanced Coding Agent loop (Phase 6).
//!
//! When the VL-450M intent router classifies a request as `code`, this
//! module drives one 1.2B coding-agent slot through a ReAct-style loop: the model
//! emits a JSON action (`{"tool": "...", "args": {...}}`) or a final answer
//! (`{"answer": "..."}`); tools run inside the agent's workspace; observations
//! are fed back; iteration stops on an answer or when the budget is spent.
//!
//! On finish, the **Script Validation Engine** sweeps every file written during
//! the run (syntax-checking Python / JS / shell / JSON) and attaches the report
//! to the response so Ashat Hub receives debugged artifacts.

use crate::demand::{DemandPool, InstanceGuard};
use crate::skill_db::SkillDb;
use chrono::Utc;
use omega_common::config::ToolLoopSection;
use omega_common::metrics::MetricsStore;
use omega_common::types::{
    ChatChoice, ChatMessage, ChatRequest, ChatResponse, ChatUsage, MetricRecord, Pool, ProxyError,
};
use omega_common::workspace::AgentWorkspace;
use serde_json::{json, Value};
use std::{
    future::Future,
    path::{Component, Path, PathBuf},
    pin::Pin,
    sync::Arc,
    time::{Duration, Instant},
};
use tracing::{debug, info, warn};

/// Per-run limits for the tool loop (resolved from `tool_loop` config).
#[derive(Debug, Clone)]
pub struct ToolLoopConfig {
    pub max_iterations: usize,
    pub command_timeout: Duration,
    pub output_max_chars: usize,
    pub max_tokens: u32,
    pub acquire_timeout: Duration,
    /// Serper API key: config value → env `SERPER_API_KEY` → empty (DDG fallback).
    pub serper_api_key: String,
}

impl ToolLoopConfig {
    pub fn from_sections(
        tl: &ToolLoopSection,
        inference_max_tokens: u32,
        inference_timeout_seconds: u64,
    ) -> Self {
        // Priority: config value → env var → empty
        let serper_api_key = if !tl.serper_api_key.is_empty() {
            tl.serper_api_key.clone()
        } else {
            std::env::var("SERPER_API_KEY").unwrap_or_default()
        };
        Self {
            max_iterations: tl.max_iterations,
            command_timeout: Duration::from_secs(tl.command_timeout_seconds),
            output_max_chars: tl.output_max_chars,
            max_tokens: inference_max_tokens,
            acquire_timeout: Duration::from_secs(inference_timeout_seconds),
            serper_api_key,
        }
    }
}

/// Per-iteration model call. The default hits llama-server via
/// `send_non_stream`; tests inject a scripted responder.
type Sender = Arc<
    dyn Fn(
            &InstanceGuard,
            &ChatRequest,
        ) -> Pin<Box<dyn Future<Output = Result<Value, ProxyError>> + Send>>
        + Send
        + Sync,
>;

/// Drives one held 1.2B coding-agent slot through the agent loop.
pub struct ToolLoop {
    pool: Arc<DemandPool>,
    workspace: AgentWorkspace,
    cfg: ToolLoopConfig,
    skill_db: SkillDb,
    sender: Sender,
}

impl ToolLoop {
    pub fn new(
        pool: Arc<DemandPool>,
        workspace: AgentWorkspace,
        cfg: ToolLoopConfig,
        skill_db: SkillDb,
    ) -> Self {
        Self {
            pool,
            workspace,
            cfg,
            skill_db,
            sender: Arc::new(|guard, request| {
                let host = guard.pool().spec.host.clone();
                let port = guard.port();
                let req = request.clone();
                Box::pin(async move { crate::proxy::send_non_stream_to(&host, port, &req).await })
            }),
        }
    }

    #[cfg(test)]
    fn with_sender(
        pool: Arc<DemandPool>,
        workspace: AgentWorkspace,
        cfg: ToolLoopConfig,
        skill_db: SkillDb,
        sender: Sender,
    ) -> Self {
        Self {
            pool,
            workspace,
            cfg,
            skill_db,
            sender,
        }
    }

    /// Run one agentic session. The coding-agent slot is held (and killed on
    /// drop) for the whole run.
    pub async fn run(
        &self,
        request: &ChatRequest,
        metrics: &Arc<MetricsStore>,
    ) -> Result<ChatResponse, ProxyError> {
        let started = Instant::now();
        let guard = self
            .pool
            .clone()
            .acquire(metrics, self.cfg.acquire_timeout)
            .await
            .map_err(|_| ProxyError::NoneAvailable)?;
        let port = guard.port();
        metrics.mark_run_start(port);
        let _ = self.workspace.log_request(port, request, "code");

        let mut state = RunState::new(request, &self.workspace, port);
        let result: Result<ChatResponse, ProxyError> = async {
            for _ in 0..self.cfg.max_iterations {
                if state.answered.is_some() {
                    break;
                }
                let model_req = ChatRequest {
                    model: None,
                    messages: state.messages.clone(),
                    max_tokens: Some(self.cfg.max_tokens),
                    temperature: Some(0.0),
                    top_p: None,
                    stream: Some(false),
                };
                let value = (self.sender)(&guard, &model_req).await?;
                let text = extract_content(&value).unwrap_or_default();
                state.last_text = text.clone();
                match parse_action(&text) {
                    Action::Answer(answer) => {
                        info!(port, iterations = state.iterations, "tool loop answered");
                        state.answered = Some(answer);
                    }
                    Action::Tool {
                        name,
                        args,
                        final_answer,
                    } => {
                        state.iterations += 1;
                        let outcome = execute_tool(
                            &name,
                            &args,
                            &self.workspace,
                            port,
                            &self.cfg,
                            &self.skill_db,
                        )
                        .await;
                        if let Some(written) = outcome.written {
                            state.written.push(written);
                        }
                        debug!(tool = %name, "tool executed");
                        state.messages.push(ChatMessage {
                            role: "assistant".to_owned(),
                            content: text,
                        });
                        state.messages.push(ChatMessage {
                            role: "user".to_owned(),
                            content: format!("[tool {name} result]\n{}", outcome.output),
                        });
                        // Small models often emit `{"tool": ...} {"answer": ...}`
                        // concatenated: the tool already ran (real side effect);
                        // finish the loop with the co-present answer.
                        if let Some(answer) = final_answer {
                            info!(
                                port,
                                iterations = state.iterations,
                                "tool loop answered (trailing answer block)"
                            );
                            state.answered = Some(answer);
                        }
                    }
                    Action::None => {
                        state.messages.push(ChatMessage {
                            role: "assistant".to_owned(),
                            content: text,
                        });
                        state.messages.push(ChatMessage {
                            role: "user".to_owned(),
                            content: "Your last response was not a valid action. Emit exactly \
                                     one JSON object: {\"tool\":\"<name>\",\"args\":{...}} to call \
                                     a tool, or {\"answer\":\"<final response>\"} to finish. No \
                                     prose around it."
                                .to_owned(),
                        });
                    }
                }
            }

            let final_text = match state.answered {
                Some(answer) => answer,
                None => {
                    warn!(
                        port,
                        budget = self.cfg.max_iterations,
                        "tool loop budget exhausted"
                    );
                    format!(
                        "{} (iteration budget of {} exceeded — last model output)",
                        state.last_text, self.cfg.max_iterations
                    )
                }
            };
            let validation = validate_sweep(&state.written, &self.cfg).await;
            Ok(self.build_response(port, final_text, validation))
        }
        .await;

        metrics.mark_run_finish(port);
        let latency = started.elapsed().as_secs_f64() * 1000.0;
        match &result {
            Ok(_) => {
                metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: "code",
                    target_port: port,
                    success: true,
                    latency_ms: latency,
                    queue_wait_ms: 0.0,
                    error_category: None,
                });
            }
            Err(err) => {
                warn!(port, error = %err, "tool loop failed");
                metrics.record(MetricRecord {
                    timestamp: Utc::now().to_rfc3339(),
                    pool: Pool::CodingAgent,
                    intent: "code",
                    target_port: port,
                    success: false,
                    latency_ms: latency,
                    queue_wait_ms: 0.0,
                    error_category: Some(err.to_string()),
                });
            }
        }
        result
    }

    fn build_response(&self, port: u16, final_text: String, validation: Value) -> ChatResponse {
        let model_label = self
            .pool
            .spec
            .model
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("coding-agent")
            .to_owned();
        ChatResponse {
            id: format!("chatcmpl-tool-{port}"),
            object: "chat.completion".to_owned(),
            created: Utc::now().timestamp().max(0) as u64,
            model: model_label,
            choices: vec![ChatChoice {
                index: 0,
                message: ChatMessage {
                    role: "assistant".to_owned(),
                    content: final_text,
                },
                finish_reason: "stop".to_owned(),
            }],
            usage: ChatUsage {
                prompt_tokens: 0,
                completion_tokens: 0,
                total_tokens: 0,
            },
            lane: Some(port.to_string()),
            validation: Some(validation),
        }
    }
}

/// Per-run mutable state: the message trail, files written, and the answer.
struct RunState {
    messages: Vec<ChatMessage>,
    written: Vec<PathBuf>,
    answered: Option<String>,
    last_text: String,
    iterations: usize,
}

impl RunState {
    fn new(request: &ChatRequest, workspace: &AgentWorkspace, port: u16) -> Self {
        let dir = workspace.ensure_agent_dir(port).unwrap_or_default();
        let system = format!(
            "You are Ashat's advanced coding agent. You work in the workspace directory {}.\n\
             You may call tools by replying with EXACTLY one JSON object and nothing else:\n\
             \n\
             TIER 1 — File system tools:\n\
             {{\"tool\": \"read_file\", \"args\": {{\"path\": \"relative/path\"}}}}\n\
             {{\"tool\": \"write_file\", \"args\": {{\"path\": \"relative/path\", \"content\": \"...\"}}}}\n\
             {{\"tool\": \"str_replace\", \"args\": {{\"path\": \"relative/path\", \"old_string\": \"exact match\", \"new_string\": \"replacement\", \"allow_multiple\": false}}}}\n\
             {{\"tool\": \"sed_replace\", \"args\": {{\"path\": \"relative/path\", \"pattern\": \"regex\", \"replacement\": \"$1 replacement\", \"flags\": \"i\"}}}}\n\
             {{\"tool\": \"list_dir\", \"args\": {{\"path\": \"relative/dir\"}}}} (optional path, defaults to workspace root)\n\
             {{\"tool\": \"tree\", \"args\": {{\"path\": \"relative/dir\", \"max_depth\": 4}}}} (recursive directory tree)\n\
             {{\"tool\": \"glob\", \"args\": {{\"pattern\": \"**/*.rs\"}}}}\n\
             {{\"tool\": \"code_search\", \"args\": {{\"pattern\": \"search regex\", \"flags\": \"-i\", \"maxResults\": 15}}}}\n\
             \n\
             TIER 2 — Execution tools:\n\
             {{\"tool\": \"run_command\", \"args\": {{\"command\": \"shell command\"}}}}\n\
             {{\"tool\": \"apply_patch\", \"args\": {{\"diff\": \"unified diff\"}}}}\n\
             {{\"tool\": \"git_status\"}} — show working tree status\n\
             {{\"tool\": \"git_diff\", \"args\": {{\"path\": \"file\", \"staged\": false, \"base\": \"HEAD\"}}}}\n\
             \n\
             TIER 3 — Web tools:\n\
             {{\"tool\": \"read_url\", \"args\": {{\"url\": \"https://...\", \"max_chars\": 20000}}}}\n\
             {{\"tool\": \"web_search\", \"args\": {{\"query\": \"search terms\", \"depth\": \"standard\"}}}}\n\
             \n\
             Legacy tools:\n\
             {{\"tool\": \"validate\", \"args\": {{\"path\": \"relative/path\"}}}}\n\
             {{\"tool\": \"skill\", \"args\": {{\"name\": \"skill-name\"}}}}\n\
             \n\
             When the task is complete, reply with EXACTLY one JSON object:\n\
             {{\"answer\": \"your final response to the user\"}}\n\
             File paths must be relative to the workspace and must never escape it.",
            dir.display()
        );
        let mut messages = vec![ChatMessage {
            role: "system".to_owned(),
            content: system,
        }];
        messages.extend(request.messages.clone());
        Self {
            messages,
            written: Vec::new(),
            answered: None,
            last_text: String::new(),
            iterations: 0,
        }
    }
}

struct ToolOutcome {
    output: String,
    written: Option<PathBuf>,
}

#[derive(Debug)]
enum Action {
    Answer(String),
    Tool {
        name: String,
        args: Value,
        /// Set when the reply also contained an `answer`/`finish` block: the
        /// tool still executes (side effect), then the loop finishes with this.
        final_answer: Option<String>,
    },
    None,
}

fn extract_content(value: &Value) -> Option<String> {
    value
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .map(|s| s.to_owned())
}

/// Interpret the model's reply as a final answer, a tool call, or nothing.
///
/// Scans *all* balanced JSON blocks in the reply (small models often emit
/// `{"tool": ...} {"answer": ...}` concatenated or wrap actions in prose).
/// If a tool block is present it is returned even when an answer block is
/// also present — the caller executes the tool (side effect) and then uses
/// the co-present answer as the final text. Without a tool block, a lone
/// answer/finish block wins.
fn parse_action(text: &str) -> Action {
    let blocks = json_blocks(text);
    let mut answer: Option<(usize, String)> = None;
    for (i, v) in blocks.iter().enumerate() {
        if let Some(a) = v.get("answer").and_then(Value::as_str) {
            answer = Some((i, a.trim().to_owned()));
            break;
        }
        if let Some(f) = v.get("finish").and_then(Value::as_str) {
            answer = Some((i, f.trim().to_owned()));
            break;
        }
    }
    let mut tool: Option<(usize, String, Value)> = None;
    for (i, v) in blocks.iter().enumerate() {
        if let Some(t) = v.get("tool").and_then(Value::as_str) {
            tool = Some((
                i,
                t.trim().to_lowercase(),
                v.get("args").cloned().unwrap_or_else(|| json!({})),
            ));
            break;
        }
    }
    match (tool, answer) {
        // Tool block precedes the answer: run the tool (side effect), then
        // finish with the co-present answer.
        (Some((ti, name, args)), Some((ai, a))) if ti < ai => Action::Tool {
            name,
            args,
            final_answer: Some(a),
        },
        // Plain tool call with no answer.
        (Some((_, name, args)), None) => Action::Tool {
            name,
            args,
            final_answer: None,
        },
        // Answer first (or same block): the model declared itself done — a
        // trailing/stray tool block must NOT execute.
        (Some(_), Some((_, a))) => Action::Answer(a),
        (None, Some((_, a))) => Action::Answer(a),
        (None, None) => Action::None,
    }
}

/// Parse the whole text as JSON, else find every balanced `{...}` block (so
/// prose-wrapped and concatenated actions still work).
fn json_blocks(text: &str) -> Vec<Value> {
    let mut out = Vec::new();
    if let Ok(v) = serde_json::from_str::<Value>(text) {
        out.push(v);
        return out;
    }
    let mut search_from = 0;
    while let Some(start) = text[search_from..].find('{') {
        let start = search_from + start;
        let mut depth = 0i32;
        let mut in_string = false;
        let mut escaped = false;
        let mut end = None;
        for (i, ch) in text[start..].char_indices() {
            if in_string {
                if escaped {
                    escaped = false;
                } else if ch == '\\' {
                    escaped = true;
                } else if ch == '"' {
                    in_string = false;
                }
                continue;
            }
            match ch {
                '"' => in_string = true,
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = Some(start + i + 1);
                        break;
                    }
                }
                _ => {}
            }
        }
        match end {
            Some(end) => {
                if let Ok(v) = serde_json::from_str(&text[start..end]) {
                    out.push(v);
                }
                search_from = end;
            }
            None => break,
        }
    }
    out
}

async fn execute_tool(
    name: &str,
    args: &Value,
    workspace: &AgentWorkspace,
    port: u16,
    cfg: &ToolLoopConfig,
    skill_db: &SkillDb,
) -> ToolOutcome {
    let dir = match workspace.ensure_agent_dir(port) {
        Ok(d) => d,
        Err(e) => {
            return ToolOutcome {
                output: format!("workspace unavailable: {e}"),
                written: None,
            }
        }
    };
    let path_arg = args.get("path").and_then(Value::as_str).unwrap_or("");
    let output = match name {
        // ── Tier 1: file system read ──────────────────────────────
        "read_file" | "read-files" => {
            read_file(&dir, path_arg, cfg.output_max_chars)
        }
        // ── Tier 1: file system write ─────────────────────────────
        "write_file" | "write-file" => {
            let content = args.get("content").and_then(Value::as_str).unwrap_or("");
            match write_file(&dir, path_arg, content) {
                Ok(joined) => {
                    let bytes = std::fs::metadata(&joined).map(|m| m.len()).unwrap_or(0);
                    return ToolOutcome {
                        output: format!("wrote {path_arg} ({bytes} bytes)"),
                        written: Some(joined),
                    };
                }
                Err(e) => Err(e),
            }
        }
        // ── Tier 1: precise file editing ──────────────────────────
        "str_replace" | "str-replace" => {
            let old_str = args.get("old_string").and_then(Value::as_str).unwrap_or("");
            let new_str = args.get("new_string").and_then(Value::as_str).unwrap_or("");
            let allow = args
                .get("allow_multiple")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            str_replace_tool(&dir, path_arg, old_str, new_str, allow)
        }
        // ── Tier 1: regex-based file editing ──────────────────────
        "sed_replace" | "sed-replace" => {
            let pattern = args.get("pattern").and_then(Value::as_str).unwrap_or("");
            let replacement = args.get("replacement").and_then(Value::as_str).unwrap_or("");
            let flags = args.get("flags").and_then(Value::as_str).unwrap_or("");
            sed_replace(&dir, path_arg, pattern, replacement, flags)
        }
        // ── Tier 1: directory browsing ────────────────────────────
        "list_dir" | "list-directory" => {
            let sub = args.get("path").and_then(Value::as_str).unwrap_or("");
            let target = if sub.is_empty() {
                dir.clone()
            } else {
                match safe_join(&dir, sub) {
                    Ok(p) => p,
                    Err(e) => return ToolOutcome { output: e, written: None },
                }
            };
            list_dir(&target)
        }
        // ── Tier 1: directory tree ────────────────────────────────
        "tree" => {
            let sub = args.get("path").and_then(Value::as_str).unwrap_or("");
            let depth = args
                .get("max_depth")
                .and_then(Value::as_u64)
                .unwrap_or(4) as usize;
            tree(&dir, sub, depth)
        }
        // ── Tier 1: file discovery by glob ────────────────────────
        "glob" => {
            let pattern = args.get("pattern").and_then(Value::as_str).unwrap_or("*");
            glob_files(&dir, pattern)
        }
        // ── Tier 1: ripgrep-based code search ─────────────────────
        "code_search" | "code-search" => {
            let pattern = args.get("pattern").and_then(Value::as_str).unwrap_or("");
            let flags = args.get("flags").and_then(Value::as_str).unwrap_or("");
            let max_results = args
                .get("maxResults")
                .and_then(Value::as_u64)
                .unwrap_or(15) as usize;
            code_search(&dir, pattern, flags, max_results)
        }
        // ── Tier 2: shell execution ───────────────────────────────
        "run_command" | "run-terminal-command" => {
            let command = args.get("command").and_then(Value::as_str).unwrap_or("");
            run_command(&dir, command, cfg).await
        }
        // ── Tier 2: unified diff application ──────────────────────
        "apply_patch" | "apply-patch" => {
            let diff = args.get("diff").and_then(Value::as_str).unwrap_or("");
            apply_patch(&dir, diff)
        }
        // ── Tier 1: git status ───────────────────────────────────
        "git_status" | "git-status" => {
            git_status(&dir, cfg).await
        }
        // ── Tier 1: git diff ─────────────────────────────────────
        "git_diff" | "git-diff" => {
            let path = args.get("path").and_then(Value::as_str).unwrap_or("");
            let staged = args.get("staged").and_then(Value::as_bool).unwrap_or(false);
            let base = args.get("base").and_then(Value::as_str).unwrap_or("");
            git_diff(&dir, path, staged, base, cfg).await
        }
        // ── Tier 3: web content fetching ──────────────────────────
        "read_url" | "read-url" => {
            let url = args.get("url").and_then(Value::as_str).unwrap_or("");
            let max_chars = args
                .get("max_chars")
                .and_then(Value::as_u64)
                .unwrap_or(20000) as usize;
            read_url(url, max_chars).await
        }
        // ── Tier 3: web search ────────────────────────────────────
        "web_search" | "web-search" => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            let depth = args.get("depth").and_then(Value::as_str).unwrap_or("standard");
            web_search(query, depth, &cfg.serper_api_key).await
        }
        // ── legacy tools ──────────────────────────────────────────
        "validate" => match safe_join(&dir, path_arg) {
            Ok(joined) => validate_file_abs(&joined, cfg).await,
            Err(e) => Err(e),
        },
        "skill" => {
            let skill = args.get("name").and_then(Value::as_str).unwrap_or("");
            skill_lookup(skill_db, skill).await
        }
        other => Err(format!("unknown tool: {other}")),
    };
    ToolOutcome {
        output: output.unwrap_or_else(|e| e),
        written: None,
    }
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_owned()
    } else {
        let cut: String = s.chars().take(max).collect();
        format!("{cut}... [truncated]")
    }
}

/// Minimal percent-encoding for URL query parameters.
fn simple_url_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for byte in s.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char);
            }
            b' ' => out.push_str("%20"),
            _ => {
                out.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    out
}

/// Join a relative path onto the workspace, rejecting anything that escapes it
/// (absolute paths, `..` climbing out). Non-existent parents are allowed.
fn safe_join(dir: &Path, rel: &str) -> Result<PathBuf, String> {
    if rel.trim().is_empty() {
        return Err("empty path".to_owned());
    }
    let p = Path::new(rel);
    if p.is_absolute() {
        return Err(format!("absolute paths not allowed: {rel}"));
    }
    let base = dir
        .canonicalize()
        .map_err(|e| format!("workspace unavailable: {e}"))?;
    let mut cur = base.clone();
    for comp in p.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                if !cur.pop() {
                    return Err(format!("path escapes the workspace: {rel}"));
                }
            }
            Component::Normal(seg) => cur.push(seg),
            other => return Err(format!("unsupported path component: {other:?}")),
        }
    }
    if !cur.starts_with(&base) {
        return Err(format!("path escapes the workspace: {rel}"));
    }
    Ok(cur)
}

fn list_dir(dir: &Path) -> Result<String, String> {
    let mut out = String::new();
    for entry in std::fs::read_dir(dir).map_err(|e| e.to_string())? {
        let e = entry.map_err(|e| e.to_string())?;
        let name = e.file_name().to_string_lossy().into_owned();
        let kind = if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            "dir "
        } else {
            "file"
        };
        out.push_str(&format!("{kind} {name}\n"));
    }
    if out.is_empty() {
        out.push_str("(empty)\n");
    }
    Ok(out)
}

/// ── Tier 1: tree — directory structure with indentation ──────────────
///
/// Recursively lists a directory tree with classic tree-style formatting
/// (├──, └──, │). Accepts an optional subpath within the workspace and
/// a max depth (default 4). Output is capped at 500 lines to avoid
/// runaway results. Does not follow symlinks.
fn tree(dir: &Path, subpath: &str, max_depth: usize) -> Result<String, String> {
    let target = if subpath.is_empty() {
        dir.to_path_buf()
    } else {
        safe_join(dir, subpath)?
    };
    if !target.is_dir() {
        return Err(format!("not a directory: {}", target.display()));
    }
    let mut out = String::new();
    let mut line_count = 0usize;
    let max_lines = 500;
    // Print the root name
    let root_name = target
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| ".".to_owned());
    out.push_str(&format!("{root_name}\n"));
    line_count += 1;
    tree_recurse(&target, dir, "", max_depth, &mut out, &mut line_count, max_lines);
    if line_count >= max_lines {
        out.push_str(&format!("... (truncated at {max_lines} lines)\n"));
    }
    Ok(out)
}

fn tree_recurse(
    current: &Path,
    workspace: &Path,
    prefix: &str,
    depth: usize,
    out: &mut String,
    line_count: &mut usize,
    max_lines: usize,
) {
    if depth == 0 || *line_count >= max_lines {
        return;
    }
    let mut entries: Vec<(String, PathBuf, bool)> = Vec::new();
    if let Ok(read_dir) = std::fs::read_dir(current) {
        for entry in read_dir.flatten() {
            // Skip symlinks to avoid infinite loops
            let meta = match entry.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            if meta.file_type().is_symlink() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().into_owned();
            let is_dir = meta.is_dir();
            entries.push((name, entry.path(), is_dir));
        }
    }
    entries.sort_by(|a, b| {
        // Directories first, then files, alphabetical within each
        b.2.cmp(&a.2).then_with(|| a.0.cmp(&b.0))
    });
    let count = entries.len();
    for (i, (name, path, is_dir)) in entries.into_iter().enumerate() {
        if *line_count >= max_lines {
            break;
        }
        let is_last = i == count - 1;
        let connector = if is_last { "└── " } else { "├── " };
        let display = if is_dir {
            format!("{name}/")
        } else {
            name
        };
        out.push_str(&format!("{prefix}{connector}{display}\n"));
        *line_count += 1;
        if is_dir {
            let child_prefix = if is_last {
                format!("{prefix}    ")
            } else {
                format!("{prefix}│   ")
            };
            tree_recurse(
                &path,
                workspace,
                &child_prefix,
                depth - 1,
                out,
                line_count,
                max_lines,
            );
        }
    }
}

fn read_file(dir: &Path, rel: &str, max: usize) -> Result<String, String> {
    let joined = safe_join(dir, rel)?;
    let content = std::fs::read_to_string(&joined).map_err(|e| format!("read failed: {e}"))?;
    Ok(truncate(&content, max))
}

fn write_file(dir: &Path, rel: &str, content: &str) -> Result<PathBuf, String> {
    let joined = safe_join(dir, rel)?;
    if let Some(parent) = joined.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("mkdir failed: {e}"))?;
    }
    std::fs::write(&joined, content).map_err(|e| format!("write failed: {e}"))?;
    Ok(joined)
}

async fn run_command(dir: &Path, command: &str, cfg: &ToolLoopConfig) -> Result<String, String> {
    if command.trim().is_empty() {
        return Err("empty command".to_owned());
    }
    let child = tokio::process::Command::new("sh")
        .arg("-c")
        .arg(command)
        .current_dir(dir)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("failed to start shell: {e}"))?;
    let output = match tokio::time::timeout(cfg.command_timeout, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("command failed: {e}")),
        Err(_) => {
            // Dropping the future kills the child via kill_on_drop.
            return Err(format!(
                "command timed out after {}s",
                cfg.command_timeout.as_secs()
            ));
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let mut text = String::new();
    if !output.status.success() {
        text.push_str(&format!("exit code {:?}\n", output.status.code()));
    }
    text.push_str(&stdout);
    if !stderr.trim().is_empty() {
        text.push_str(&format!("\nstderr: {stderr}"));
    }
    Ok(truncate(&text, cfg.output_max_chars))
}

/// ── Tier 1: git_status — working tree status ────────────────────────
///
/// Runs `git status --short` in the workspace directory. Returns a
/// concise listing of modified, staged, and untracked files. Timeout
/// protected like run_command.
async fn git_status(dir: &Path, cfg: &ToolLoopConfig) -> Result<String, String> {
    let child = tokio::process::Command::new("git")
        .args(["status", "--short"])
        .current_dir(dir)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("failed to run git: {e}"))?;
    let output = match tokio::time::timeout(cfg.command_timeout, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("git status failed: {e}")),
        Err(_) => {
            return Err(format!(
                "git status timed out after {}s",
                cfg.command_timeout.as_secs()
            ));
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        // exit code 128 usually means not a git repo
        return Err(format!("git status failed (exit {:?}): {}",
            output.status.code(), stderr.trim()));
    }
    let text = stdout.trim();
    if text.is_empty() {
        Ok("(clean — no changes)\n".to_owned())
    } else {
        Ok(format!("{text}\n"))
    }
}

/// ── Tier 1: git_diff — show changes ─────────────────────────────────
///
/// Runs `git diff` in the workspace directory. Accepts optional args:
/// - `path`: restrict diff to a specific file
/// - `staged`: if true, shows staged changes (`git diff --staged`)
/// - `base`: show diff against a specific ref (e.g. HEAD, main)
async fn git_diff(
    dir: &Path,
    path: &str,
    staged: bool,
    base: &str,
    cfg: &ToolLoopConfig,
) -> Result<String, String> {
    let mut cmd = tokio::process::Command::new("git");
    cmd.current_dir(dir)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    if staged {
        cmd.args(["diff", "--staged"]);
    } else if !base.is_empty() {
        cmd.args(["diff", base]);
    } else {
        cmd.arg("diff");
    }
    if !path.is_empty() {
        cmd.arg("--").arg(path);
    }
    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to run git: {e}"))?;
    let output = match tokio::time::timeout(cfg.command_timeout, child.wait_with_output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("git diff failed: {e}")),
        Err(_) => {
            return Err(format!(
                "git diff timed out after {}s",
                cfg.command_timeout.as_secs()
            ));
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!("git diff failed (exit {:?}): {}",
            output.status.code(), stderr.trim()));
    }
    let text = stdout.trim();
    if text.is_empty() {
        Ok("(no diff — working tree clean)\n".to_owned())
    } else {
        Ok(truncate(text, cfg.output_max_chars))
    }
}

/// ── Tier 1: str_replace — precise file editing ──────────────────────
///
/// Reads the file, finds the exact `old_string`, replaces it with
/// `new_string`. Rejects if the old string is not found (or found more
/// than once — to avoid ambiguity). If `allow_multiple` is true in args,
/// all occurrences are replaced.
fn str_replace_tool(
    dir: &Path,
    rel: &str,
    old_str: &str,
    new_str: &str,
    allow_multiple: bool,
) -> Result<String, String> {
    if old_str.is_empty() {
        return Err("old_string is empty".to_owned());
    }
    let joined = safe_join(dir, rel)?;
    let content = std::fs::read_to_string(&joined)
        .map_err(|e| format!("read failed: {e}"))?;
    let count = content.matches(old_str).count();
    if count == 0 {
        return Err(format!("old_string not found in {rel}"));
    }
    // Default: reject if more than one match (ambiguous). The model can
    // add more surrounding context to disambiguate.
    if count > 1 && !allow_multiple {
        return Err(format!(
            "old_string found {count} times in {rel}; add more context to make it unique, or set allow_multiple: true"
        ));
    }
    let new_content = content.replacen(old_str, new_str, if allow_multiple { usize::MAX } else { 1 });
    std::fs::write(&joined, &new_content)
        .map_err(|e| format!("write failed: {e}"))?;
    Ok(format!("replaced {count} occurrence(s) in {rel}"))
}

/// ── Tier 1: sed_replace — regex-based file editing ────────────────────
///
/// Reads the file, applies a regex replacement. Supports case-insensitive
/// mode (`flags: "i"`) and regex capture groups (`$1`, `$2`, etc. in the
/// replacement string). Returns the number of replacements made.
fn sed_replace(
    dir: &Path,
    rel: &str,
    pattern: &str,
    replacement: &str,
    flags: &str,
) -> Result<String, String> {
    if pattern.is_empty() {
        return Err("pattern is empty".to_owned());
    }
    let mut builder = regex::RegexBuilder::new(pattern);
    if flags.contains('i') {
        builder.case_insensitive(true);
    }
    let re = builder
        .build()
        .map_err(|e| format!("invalid regex '{pattern}': {e}"))?;
    let joined = safe_join(dir, rel)?;
    let content = std::fs::read_to_string(&joined)
        .map_err(|e| format!("read failed: {e}"))?;
    let count = re.find_iter(&content).count();
    if count == 0 {
        return Err(format!("pattern '{pattern}' not found in {rel}"));
    }
    let new_content = re.replace_all(&content, replacement);
    std::fs::write(&joined, new_content.as_ref())
        .map_err(|e| format!("write failed: {e}"))?;
    Ok(format!("replaced {count} occurrence(s) in {rel}"))
}

/// ── Tier 1: glob — file discovery by pattern ─────────────────────────
///
/// Uses the `glob` crate to find files matching a pattern within the
/// workspace. Returns relative paths sorted by modification time (most
/// recent first), capped at 250 results.
fn glob_files(dir: &Path, pattern: &str) -> Result<String, String> {
    let full_pattern = dir.join(pattern);
    let pat_str = full_pattern.to_string_lossy();
    let paths = glob::glob(&pat_str)
        .map_err(|e| format!("invalid glob pattern '{pattern}': {e}"))?;
    let mut entries: Vec<(PathBuf, std::time::SystemTime)> = Vec::new();
    for entry in paths {
        let p = entry.map_err(|e| format!("glob read error: {e}"))?;
        let meta = p.metadata().map_err(|e| format!("metadata error: {e}"))?;
        let mtime = meta.modified().unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        entries.push((p, mtime));
    }
    entries.sort_by(|a, b| b.1.cmp(&a.1)); // most recent first
    let mut out = String::new();
    let count = entries.len();
    for (p, _) in entries.into_iter().take(250) {
        let rel = p.strip_prefix(dir).unwrap_or(&p);
        out.push_str(&format!("{}\n", rel.display()));
    }
    if out.is_empty() {
        out.push_str("(no matches)\n");
    }
    if count > 250 {
        out.push_str(&format!("... ({count} total, showing 250)\n"));
    }
    Ok(out)
}

/// ── Tier 1: code_search — ripgrep-based code search ──────────────────
///
/// Shells out to `rg` (ripgrep) for fast pattern matching. Returns
/// structured results: file path, line number, and matched line.
/// Respects .gitignore rules by default.
fn code_search(dir: &Path, pattern: &str, flags: &str, max_results: usize) -> Result<String, String> {
    if pattern.trim().is_empty() {
        return Err("search pattern is empty".to_owned());
    }
    // Check that ripgrep is available
    let rg_available = std::process::Command::new("rg")
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
    if rg_available.is_err() {
        return Err(
            "ripgrep (rg) is not installed. Install it with: cargo install ripgrep, or sudo apt install ripgrep"
                .to_owned(),
        );
    }
    let mut cmd = std::process::Command::new("rg");
    cmd.arg("--json")
        .arg("--max-count")
        .arg("1") // one match per file
        .arg("--max-filesize")
        .arg("1M")
        .arg("--no-binary");
    // Parse extra flags from the flags string
    for flag in flags.split_whitespace() {
        cmd.arg(flag);
    }
    cmd.arg(pattern).arg(dir);
    let output = cmd
        .output()
        .map_err(|e| format!("failed to run rg: {e}"))?;
    // ripgrep returns exit code 1 for no matches (not an error)
    if !output.status.success() && !output.stdout.is_empty() {
        // Some flags might cause failure; still try to parse stdout
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut results: Vec<String> = Vec::new();
    for line in stdout.lines().take(max_results * 2) {
        // Each JSON line from rg --json is a JSON object with type and data
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            let typ = v.get("type").and_then(Value::as_str).unwrap_or("");
            if typ == "match" {
                let data = v.get("data");
                let file = data
                    .and_then(|d| d.get("path"))
                    .and_then(|p| p.get("text"))
                    .and_then(Value::as_str)
                    .unwrap_or("?");
                let line_number = data
                    .and_then(|d| d.get("line_number"))
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                let text = data
                    .and_then(|d| d.get("lines"))
                    .and_then(|l| l.get("text"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                // Strip the dir prefix from the file path
                let rel = std::path::Path::new(file)
                    .strip_prefix(dir)
                    .unwrap_or(std::path::Path::new(file));
                results.push(format!(
                    "{}:{}:{}",
                    rel.display(),
                    line_number,
                    text.trim_end()
                ));
            }
        }
        if results.len() >= max_results {
            break;
        }
    }
    if results.is_empty() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if !stderr.trim().is_empty() {
            return Err(format!("ripgrep error: {stderr}"));
        }
        return Ok("(no matches)\n".to_owned());
    }
    let mut out = results.join("\n");
    out.push('\n');
    if stdout.lines().count() > max_results {
        out.push_str(&format!("... (showing {max_results} of more matches)\n"));
    }
    Ok(out)
}

/// ── Tier 2: apply_patch — unified diff application ───────────────────
///
/// Accepts a standard unified diff string. Supports:
/// - Creating new files (--- /dev/null, +++ b/path)
/// - Modifying existing files (--- a/path, +++ b/path)
/// - Deleting files (--- a/path, +++ /dev/null)
///
/// Uses line-level 3-context matching. Strips a/ and b/ prefixes from
/// paths. Works within the workspace directory.
fn apply_patch(dir: &Path, diff: &str) -> Result<String, String> {
    if diff.trim().is_empty() {
        return Err("empty diff".to_owned());
    }
    let mut out = String::new();
    let mut lines_iter = diff.lines().peekable();
    while lines_iter.peek().is_some() {
        // Look for diff header: --- a/path or --- /dev/null
        let header = lines_iter.find(|l| l.starts_with("--- "));
        let header = match header {
            Some(h) => h,
            None => break,
        };
        let from_line = lines_iter.next().unwrap_or("");
        if !from_line.starts_with("+++ ") {
            return Err(format!(
                "expected +++ line after --- line, got: {from_line}"
            ));
        }
        let old_path_raw = header.strip_prefix("--- ").unwrap_or("").trim();
        let new_path_raw = from_line.strip_prefix("+++ ").unwrap_or("").trim();
        // Strip a/ or b/ prefixes (standard unified diff format)
        let old_path = old_path_raw
            .strip_prefix("a/")
            .unwrap_or(old_path_raw);
        let new_path = new_path_raw
            .strip_prefix("b/")
            .unwrap_or(new_path_raw);
        // Skip to @@ hunk header
        while let Some(line) = lines_iter.peek() {
            if line.starts_with("@@ ") {
                break;
            }
            lines_iter.next();
        }
        // Delete case: +++ /dev/null
        if new_path == "/dev/null" {
            let target = safe_join(dir, old_path)?;
            if target.is_file() {
                std::fs::remove_file(&target)
                    .map_err(|e| format!("delete failed: {e}"))?;
                out.push_str(&format!("deleted {old_path}\n"));
            }
            continue;
        }
        // Create case: --- /dev/null
        let is_create = old_path == "/dev/null" || old_path == "/dev/null";
        // Collect hunks
        let mut hunks: Vec<(i64, Vec<String>)> = Vec::new();
        while let Some(line) = lines_iter.peek() {
            if line.starts_with("@@ ") {
                let hunk_header = lines_iter.next().unwrap();
                // Parse @@ -old_start,old_count +new_start,new_count @@
                let parts: Vec<&str> = hunk_header.splitn(2, "@@ ").collect();
                if parts.len() < 2 {
                    return Err(format!("bad hunk header: {hunk_header}"));
                }
                let nums = parts[1]
                    .splitn(2, " @@")
                    .next()
                    .unwrap_or("");
                let new_start = nums
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("+1")
                    .trim_start_matches('+')
                    .parse::<i64>()
                    .unwrap_or(1);
                let mut hunk_lines: Vec<String> = Vec::new();
                for hline in lines_iter.by_ref() {
                    if hline.starts_with("@@ ") || hline.starts_with("diff ") {
                        // Push it back by re-peeking
                        break;
                    }
                    hunk_lines.push(hline.to_owned());
                }
                hunks.push((new_start, hunk_lines));
            } else if line.starts_with("diff ") || line.starts_with("--- ") {
                break;
            } else {
                lines_iter.next();
            }
        }
        // Apply hunks
        if is_create {
            // Create new file: collect + lines from all hunks
            let target = safe_join(dir, new_path)?;
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| format!("mkdir failed: {e}"))?;
            }
            let mut content = String::new();
            for (_, hunk_lines) in &hunks {
                for hl in hunk_lines {
                    if let Some(rest) = hl.strip_prefix('+') {
                        content.push_str(rest);
                        content.push('\n');
                    }
                }
            }
            std::fs::write(&target, &content)
                .map_err(|e| format!("write failed: {e}"))?;
            out.push_str(&format!("created {new_path}\n"));
            continue;
        }
        // Modify existing file
        let target = safe_join(dir, new_path)?;
        let original = std::fs::read_to_string(&target)
            .map_err(|e| format!("read failed for {new_path}: {e}"))?;
        let mut file_lines: Vec<String> = original.lines().map(String::from).collect();
        // Sort hunks by line number descending so we can apply from bottom up
        hunks.sort_by(|a, b| b.0.cmp(&a.0));
        for (new_start, hunk_lines) in hunks {
            // Parse hunk: split into before-context, removals/additions,
            // and after-context. Unified diffs have context lines both
            // before AND after the changed lines.
            let mut ctx_before: Vec<String> = Vec::new();
            let mut removals: Vec<String> = Vec::new();
            let mut additions: Vec<String> = Vec::new();
            let mut ctx_after: Vec<String> = Vec::new();
            // First pass: find where changes start
            let mut seen_minus_plus = false;
            for hl in &hunk_lines {
                if hl.starts_with('-') || hl.starts_with('+') {
                    seen_minus_plus = true;
                }
                if let Some(rest) = hl.strip_prefix('-') {
                    removals.push(rest.to_owned());
                } else if let Some(rest) = hl.strip_prefix('+') {
                    additions.push(rest.to_owned());
                } else if hl.starts_with(' ') {
                    if seen_minus_plus {
                        ctx_after.push(hl[1..].to_owned());
                    } else {
                        ctx_before.push(hl[1..].to_owned());
                    }
                } else if hl.starts_with("\\") {
                    // backslash line: no newline at end of file, skip
                }
            }
            // Find the context match position. Search for the before-context
            // near the expected line, then verify removals and after-context.
            let context_anchor = if ctx_before.is_empty() {
                (new_start - 1) as usize
            } else {
                let search_from = ((new_start - 1) as usize).min(file_lines.len());
                let mut found = None;
                'search: for offset in 0..=search_from.min(50) {
                    let candidate = search_from.saturating_sub(offset);
                    // Check before-context + removals + after-context fit
                    let needed = ctx_before.len() + removals.len() + ctx_after.len();
                    if candidate + needed > file_lines.len() {
                        continue;
                    }
                    // Verify before-context
                    for (i, ctx) in ctx_before.iter().enumerate() {
                        if file_lines[candidate + i] != *ctx {
                            continue 'search;
                        }
                    }
                    // Verify removals
                    let remove_start = candidate + ctx_before.len();
                    for (i, rem) in removals.iter().enumerate() {
                        if file_lines[remove_start + i] != *rem {
                            continue 'search;
                        }
                    }
                    // Verify after-context
                    let after_start = remove_start + removals.len();
                    for (i, ctx) in ctx_after.iter().enumerate() {
                        if file_lines[after_start + i] != *ctx {
                            continue 'search;
                        }
                    }
                    found = Some(candidate);
                    break;
                }
                found.ok_or_else(|| {
                    format!(
                        "could not find context for hunk at line {new_start} in {new_path}"
                    )
                })?
            };
            // start_idx is AFTER the context lines — that's where removals begin
            let start_idx = context_anchor + ctx_before.len();
            // Remove old lines and insert new lines at start_idx
            let end_idx = start_idx + removals.len();
            if end_idx > file_lines.len() {
                return Err(format!(
                    "hunk at line {new_start} extends past end of {new_path}"
                ));
            }
            // Verify removed lines match
            for (i, removal) in removals.iter().enumerate() {
                if file_lines[start_idx + i] != *removal {
                    return Err(format!(
                        "context mismatch at line {} in {new_path}: expected '{}', got '{}'",
                        start_idx + i + 1,
                        removal,
                        file_lines[start_idx + i]
                    ));
                }
            }
            // Replace
            file_lines.splice(start_idx..end_idx, additions.iter().cloned());
        }
        // Write back
        let mut new_content = file_lines.join("\n");
        // Preserve trailing newline if original had one
        if original.ends_with('\n') && !new_content.ends_with('\n') {
            new_content.push('\n');
        }
        std::fs::write(&target, &new_content)
            .map_err(|e| format!("write failed: {e}"))?;
        out.push_str(&format!("patched {new_path}\n"));
    }
    if out.is_empty() {
        return Err("no hunks found in diff".to_owned());
    }
    Ok(out)
}

/// ── Tier 3: read_url — web content fetching ──────────────────────────
///
/// Fetches a URL via reqwest (already a dependency), strips HTML tags
/// and scripts/styles, and returns the readable text content. Caps at
/// `max_chars` characters.
async fn read_url(url: &str, max_chars: usize) -> Result<String, String> {
    if url.trim().is_empty() {
        return Err("URL is empty".to_owned());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .user_agent("Mozilla/5.0 (compatible; AshatBot/1.0)")
        .build()
        .map_err(|e| format!("HTTP client init failed: {e}"))?;
    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("HTTP request failed: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(format!("HTTP {status}"));
    }
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();
    let body = resp
        .text()
        .await
        .map_err(|e| format!("read body failed: {e}"))?;
    let text = if content_type.contains("text/html") {
        strip_html(&body)
    } else {
        body
    };
    Ok(truncate(&text, max_chars))
}

/// Minimal HTML tag stripper: removes <script>, <style>, and all tags,
/// decoding a handful of common HTML entities. Good enough for the
/// coding agent to extract readable page content.
fn strip_html(html: &str) -> String {
    let mut out = String::with_capacity(html.len());
    let mut in_tag = false;
    let mut in_script = false;
    let mut in_style = false;
    let mut chars = html.char_indices().peekable();
    while let Some((_, ch)) = chars.next() {
        match ch {
            '<' => {
                // Peek at the tag name to detect script/style
                let rest: String = chars.clone().map(|(_, c)| c).take(50).collect();
                let lower = rest.to_ascii_lowercase();
                if lower.starts_with("script") {
                    in_script = true;
                } else if lower.starts_with("style") {
                    in_style = true;
                }
                in_tag = true;
            }
            '>' if in_tag => {
                in_tag = false;
                // Check for closing script/style
                // We look backwards for the tag content
                // Simple heuristic: the tag text before this '>'
                // This is a simplified approach
                if in_script && out.ends_with("</script") {
                    in_script = false;
                } else if in_style && out.ends_with("</style") {
                    in_style = false;
                }
                // Add a space to separate text from next word
                out.push(' ');
            }
            _ if in_tag => { /* skip tag content */ }
            _ if in_script || in_style => { /* skip script/style content */ }
            '&' => {
                // Simple HTML entity decoding
                let rest: String = chars.clone().map(|(_, c)| c).take(10).collect();
                if rest.starts_with("amp;") {
                    out.push('&');
                    chars.by_ref().take(3).last();
                } else if rest.starts_with("lt;") {
                    out.push('<');
                    chars.by_ref().take(2).last();
                } else if rest.starts_with("gt;") {
                    out.push('>');
                    chars.by_ref().take(2).last();
                } else if rest.starts_with("quot;") {
                    out.push('"');
                    chars.by_ref().take(4).last();
                } else if rest.starts_with("#39;") || rest.starts_with("apos;") {
                    out.push('\'');
                    chars.by_ref().take(4).last();
                } else if rest.starts_with("nbsp;") {
                    out.push(' ');
                    chars.by_ref().take(4).last();
                } else {
                    out.push(ch);
                }
            }
            '\n' | '\r' => { /* normalize newlines to space */ }
            _ => out.push(ch),
        }
    }
    // Collapse multiple spaces
    let mut result = String::with_capacity(out.len());
    let mut prev_space = false;
    for ch in out.chars() {
        if ch == ' ' {
            if !prev_space {
                result.push(' ');
            }
            prev_space = true;
        } else {
            result.push(ch);
            prev_space = false;
        }
    }
    result.trim().to_owned()
}

/// ── Tier 3: web_search — web search via Serper API ───────────────────
///
/// Uses the Google Search Serper API (https://serper.dev). The API key
/// is resolved by `ToolLoopConfig::from_sections` (config → env → empty).
/// Returns structured search results with titles, URLs, and snippets.
async fn web_search(query: &str, depth: &str, api_key: &str) -> Result<String, String> {
    if query.trim().is_empty() {
        return Err("search query is empty".to_owned());
    }
    if api_key.is_empty() {
        // Fallback: use DuckDuckGo Lite (no API key needed)
        return web_search_ddg(query, depth).await;
    }
    let num_results = match depth {
        "deep" => 20,
        _ => 10,
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| format!("HTTP client init failed: {e}"))?;
    let body = json!({
        "q": query,
        "num": num_results,
    });
    let resp = client
        .post("https://google.serper.dev/search")
        .header("X-API-KEY", api_key)
        .header("Content-Type", "application/json")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Serper API request failed: {e}"))?;
    let status = resp.status();
    let text = resp
        .text()
        .await
        .map_err(|e| format!("read body failed: {e}"))?;
    if !status.is_success() {
        return Err(format!("Serper API returned HTTP {status}: {text}"));
    }
    let v: Value = serde_json::from_str(&text)
        .map_err(|e| format!("JSON parse error: {e}"))?;
    let mut out = String::new();
    if let Some(answer_box) = v.get("answerBox") {
        if let Some(answer) = answer_box.get("answer").and_then(Value::as_str) {
            out.push_str(&format!("Answer: {answer}\n\n"));
        }
    }
    if let Some(organic) = v.get("organic").and_then(Value::as_array) {
        for (i, result) in organic.iter().enumerate() {
            let title = result.get("title").and_then(Value::as_str).unwrap_or("");
            let link = result.get("link").and_then(Value::as_str).unwrap_or("");
            let snippet = result
                .get("snippet")
                .and_then(Value::as_str)
                .unwrap_or("");
            out.push_str(&format!("{i}. {title}\n   {link}\n   {snippet}\n\n"));
        }
    }
    if out.is_empty() {
        out.push_str("(no results found)\n");
    }
    Ok(out)
}

/// Fallback web search using DuckDuckGo Lite HTML (no API key needed).
async fn web_search_ddg(query: &str, depth: &str) -> Result<String, String> {
    let num_results = match depth {
        "deep" => 20,
        _ => 10,
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .user_agent("Mozilla/5.0 (compatible; AshatBot/1.0)")
        .build()
        .map_err(|e| format!("HTTP client init failed: {e}"))?;
    let url = format!(
        "https://lite.duckduckgo.com/lite/?q={}&kl=wt-wt",
        simple_url_encode(query)
    );
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("DDG request failed: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(format!("DDG returned HTTP {status}"));
    }
    let body = resp
        .text()
        .await
        .map_err(|e| format!("read body failed: {e}"))?;
    // Parse DDG Lite results: each result has a link with class="result-link"
    // and a snippet in class="result-snippet"
    let mut results: Vec<String> = Vec::new();
    let mut i = 1;
    // Simple HTML parsing for DDG Lite structure
    for block in body.split("<tr>") {
        if i > num_results {
            break;
        }
        // Find the link
        if let Some(start) = block.find("class=\"result-link\"") {
            let rest = &block[start..];
            if let Some(href_start) = rest.find("href=") {
                let href_rest = &rest[href_start + 6..];
                if let Some(q_end) = href_rest.find('"') {
                    let link = &href_rest[..q_end];
                    // Find title text
                    let title = if let Some(t_start) = rest.find('>') {
                        let title_rest = &rest[t_start + 1..];
                        if let Some(t_end) = title_rest.find('<') {
                            title_rest[..t_end].trim()
                        } else {
                            ""
                        }
                    } else {
                        ""
                    };
                    // Find snippet
                    let snippet = if let Some(s_start) = block.find("class=\"result-snippet\"") {
                        let s_rest = &block[s_start..];
                        if let Some(s_content_start) = s_rest.find('>') {
                            let s_content = &s_rest[s_content_start + 1..];
                            if let Some(s_end) = s_content.find("</td>") {
                                let raw = &s_content[..s_end];
                                strip_html(raw)
                            } else {
                                "".to_owned()
                            }
                        } else {
                            "".to_owned()
                        }
                    } else {
                        "".to_owned()
                    };
                    if !link.is_empty() && !link.starts_with("//duckduckgo") {
                        results.push(format!(
                            "{i}. {title}\n   {link}\n   {snippet}\n"
                        ));
                        i += 1;
                    }
                }
            }
        }
    }
    // Also try to extract from <a> tags directly as a fallback
    if results.is_empty() {
        for cap in body.match_indices("<a rel=\"nofollow\" class=\"result-link\"") {
            if i > num_results {
                break;
            }
            let rest = &body[cap.0..];
            if let Some(href_start) = rest.find("href=") {
                let href_rest = &rest[href_start + 6..];
                if let Some(q_end) = href_rest.find('"') {
                    let link = &href_rest[..q_end];
                    let title = if let Some(t_start) = rest.find('>') {
                        let title_rest = &rest[t_start + 1..];
                        if let Some(t_end) = title_rest.find('<') {
                            title_rest[..t_end].trim()
                        } else {
                            ""
                        }
                    } else {
                        ""
                    };
                    if !link.is_empty() {
                        results.push(format!("{i}. {title}\n   {link}\n"));
                        i += 1;
                    }
                }
            }
        }
    }
    if results.is_empty() {
        return Ok("(no results found — DDG parsing may need updating)\n".to_owned());
    }
    Ok(results.join("\n"))
}

/// Script Validation Engine: syntax-check one file by extension.
/// Python → `ast.parse`, JS → `node --check`, shell → `bash -n`, JSON → parse.
async fn validate_file_abs(path: &Path, cfg: &ToolLoopConfig) -> Result<String, String> {
    if !path.is_file() {
        return Err(format!("not a file: {}", path.display()));
    }
    let rel = path.display().to_string();
    let ext = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_lowercase();
    let (prog, args): (&str, Vec<String>) = match ext.as_str() {
        "py" => (
            "python3",
            vec![
                "-c".to_owned(),
                "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())".to_owned(),
                rel.clone(),
            ],
        ),
        "js" | "mjs" | "cjs" => ("node", vec!["--check".to_owned(), rel.clone()]),
        "sh" | "bash" => ("bash", vec!["-n".to_owned(), rel.clone()]),
        "json" => return Ok(validate_json_abs(path, &rel)),
        _ => return Ok(format!("no syntax checker registered for .{ext}; skipped")),
    };
    let child = tokio::process::Command::new(prog)
        .args(&args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("validator {prog} unavailable: {e}"))?;
    let output = match tokio::time::timeout(Duration::from_secs(10), child.wait_with_output()).await
    {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("validator failed: {e}")),
        Err(_) => {
            // Dropping the future kills the child via kill_on_drop.
            return Err("validation timed out".to_owned());
        }
    };
    let stderr = String::from_utf8_lossy(&output.stderr);
    if output.status.success() {
        Ok(format!("{rel}: syntax OK"))
    } else {
        Err(format!(
            "{rel}: syntax error\n{}",
            truncate(stderr.trim(), cfg.output_max_chars)
        ))
    }
}

fn validate_json_abs(path: &Path, rel: &str) -> String {
    match std::fs::read_to_string(path).map(|s| serde_json::from_str::<Value>(&s)) {
        Ok(Ok(_)) => format!("{rel}: JSON OK"),
        Ok(Err(e)) => format!("{rel}: JSON error: {e}"),
        Err(e) => format!("{rel}: read error: {e}"),
    }
}

async fn skill_lookup(db: &SkillDb, name: &str) -> Result<String, String> {
    if name.trim().is_empty() {
        return Err("skill name is empty".to_owned());
    }
    match db.lookup(name).await {
        Ok(Some(content)) => Ok(format!("skill '{name}':\n{content}")),
        Ok(None) => Err(format!("skill '{name}' not found in the skills database")),
        Err(reason) => Err(format!("skills database unavailable: {reason}")),
    }
}

/// Sweep every file written during the run through the validation engine.
async fn validate_sweep(written: &[PathBuf], cfg: &ToolLoopConfig) -> Value {
    let mut files = Vec::new();
    for path in written {
        let rel = path
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| path.display().to_string());
        match validate_file_abs(path, cfg).await {
            Ok(detail) => files.push(json!({"path": rel, "ok": true, "detail": detail})),
            Err(detail) => files.push(json!({"path": rel, "ok": false, "detail": detail})),
        }
    }
    json!({
        "engine": "omega-script-validation",
        "generated_at": Utc::now().to_rfc3339(),
        "files": files,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::demand::DemandSpec;
    use omega_common::metrics::MetricsStore;
    use omega_common::types::{ChatMessage as CM, Pool};
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn cfg() -> ToolLoopConfig {
        ToolLoopConfig {
            max_iterations: 5,
            command_timeout: Duration::from_secs(5),
            output_max_chars: 4000,
            max_tokens: 128,
            acquire_timeout: Duration::from_secs(2),
            serper_api_key: String::new(),
        }
    }

    fn spec(always_alive: bool) -> DemandSpec {
        DemandSpec {
            binary: PathBuf::from("llama-server"),
            model: PathBuf::from("/nonexistent/model.gguf"),
            ctx: 4096,
            threads: 2,
            gpu_layers: 0,
            host: "127.0.0.1".to_owned(),
            mlock: false,
            always_alive,
        }
    }

    fn temp_workspace(name: &str) -> (AgentWorkspace, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "omega-tool-loop-test-{}-{name}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        (AgentWorkspace::new(root.clone()), root)
    }

    #[test]
    fn parse_action_detects_answer_and_tool() {
        assert!(matches!(parse_action("{\"answer\":\"done\"}"), Action::Answer(a) if a == "done"));
        assert!(matches!(parse_action("{\"finish\":\"ok\"}"), Action::Answer(a) if a == "ok"));
        assert!(matches!(
            parse_action("{\"tool\":\"list_dir\",\"args\":{}}"),
            Action::Tool { name, .. } if name == "list_dir"
        ));
        assert!(matches!(
            parse_action("Here is my plan:\n{\"tool\": \"read_file\", \"args\": {\"path\": \"a.py\"}}\nthanks"),
            Action::Tool { name, .. } if name == "read_file"
        ));
        assert!(matches!(parse_action("nothing useful"), Action::None));
    }

    #[test]
    fn parse_action_carries_answer_into_concatenated_tool() {
        // Small models often emit `{"tool": ...} {"answer": ...}` together:
        // the tool must still execute, with the answer carried as final.
        let combined = "{\"tool\": \"run_command\", \"args\": {\"command\": \"echo hi\"}}  \
                        {\"answer\": \"Hello, Ashat\"}";
        match parse_action(combined) {
            Action::Tool {
                name, final_answer, ..
            } => {
                assert_eq!(name, "run_command");
                assert_eq!(final_answer.as_deref(), Some("Hello, Ashat"));
            }
            other => panic!("expected Tool with final_answer, got {other:?}"),
        }
        // Prose-wrapped concatenated actions still carry the answer block.
        let wrapped =
            "Sure!\n{\"tool\":\"list_dir\"}\n{\"answer\":\"done\"}\nlet me know if you need more";
        match parse_action(wrapped) {
            Action::Tool {
                name, final_answer, ..
            } => {
                assert_eq!(name, "list_dir");
                assert_eq!(final_answer.as_deref(), Some("done"));
            }
            other => panic!("expected Tool with final_answer, got {other:?}"),
        }
        // Tool block with no answer stays a plain tool call.
        assert!(matches!(
            parse_action("{\"tool\":\"list_dir\"} whatever"),
            Action::Tool {
                final_answer: None,
                ..
            }
        )); // A lone answer (no tool) still finishes directly.
        assert!(
            matches!(parse_action("done\n{\"answer\":\"ok\"}"), Action::Answer(a) if a == "ok")
        );
        // Reversed order — answer first, then a stray tool block: the model
        // declared itself done, so the trailing tool must NOT execute.
        assert!(matches!(
            parse_action("{\"answer\":\"done\"} {\"tool\":\"run_command\",\"args\":{\"command\":\"rm -rf /\"}}"),
            Action::Answer(a) if a == "done"
        ));
    }

    #[test]
    fn safe_join_rejects_escapes() {
        let (ws, root) = temp_workspace("join");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        assert!(safe_join(&dir, "hello.py").is_ok());
        assert!(safe_join(&dir, "sub/dir/hello.py").is_ok());
        assert!(safe_join(&dir, "../escape.txt").is_err());
        assert!(safe_join(&dir, "/etc/passwd").is_err());
        assert!(safe_join(&dir, "a/../../escape").is_err());
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn write_read_roundtrip_and_escape() {
        let (ws, root) = temp_workspace("wr");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let out = execute_tool(
            "write_file",
            &json!({"path": "a/b.txt", "content": "hi"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.written.is_some());
        let out2 = execute_tool(
            "read_file",
            &json!({"path": "a/b.txt"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert_eq!(out2.output, "hi");
        let escaped = execute_tool(
            "write_file",
            &json!({"path": "../../evil.txt", "content": "x"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(escaped.output.contains("escapes"));
        let _ = dir;
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn run_command_executes_in_workspace() {
        let (ws, root) = temp_workspace("cmd");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("marker.txt"), "x").unwrap();
        let out = execute_tool(
            "run_command",
            &json!({"command": "ls marker.txt && echo hi"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("marker.txt"));
        assert!(out.output.contains("hi"));
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn validate_syntax_checks() {
        let (ws, root) = temp_workspace("val");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        execute_tool(
            "write_file",
            &json!({"path": "good.py", "content": "print(1)"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        execute_tool(
            "write_file",
            &json!({"path": "bad.py", "content": "def f(:"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        execute_tool(
            "write_file",
            &json!({"path": "good.json", "content": "{\"a\":1}"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        let good = execute_tool(
            "validate",
            &json!({"path": "good.py"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        let bad = execute_tool(
            "validate",
            &json!({"path": "bad.py"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        let json_ok = execute_tool(
            "validate",
            &json!({"path": "good.json"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(
            good.output.contains("syntax OK"),
            "good.py: {}",
            good.output
        );
        assert!(
            bad.output.contains("syntax error"),
            "bad.py: {}",
            bad.output
        );
        assert!(
            json_ok.output.contains("JSON OK"),
            "good.json: {}",
            json_ok.output
        );
        let _ = dir;
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn skill_tool_reports_disabled() {
        let (ws, _root) = temp_workspace("skill");
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(execute_tool(
                "skill",
                &json!({"name": "rust-borrowing"}),
                &ws,
                18080,
                &cfg(),
                &SkillDb::disabled(),
            ));
        assert!(out.output.contains("disabled"), "{}", out.output);
    }

    #[tokio::test]
    async fn str_replace_tool_basic() {
        let (ws, root) = temp_workspace("str-replace");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "hello world").unwrap();
        let out = execute_tool(
            "str_replace",
            &json!({"path": "test.txt", "old_string": "world", "new_string": "rust"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("replaced 1"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "hello rust");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn str_replace_rejects_ambiguous_without_allow_multiple() {
        let (ws, root) = temp_workspace("str-replace-ambig");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "aaa").unwrap();
        let out = execute_tool(
            "str_replace",
            &json!({"path": "test.txt", "old_string": "a", "new_string": "b"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("found 3 times"), "{}", out.output);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn str_replace_allow_multiple() {
        let (ws, root) = temp_workspace("str-replace-multi");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "aaa").unwrap();
        let out = execute_tool(
            "str_replace",
            &json!({"path": "test.txt", "old_string": "a", "new_string": "b", "allow_multiple": true}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("replaced 3"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "bbb");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn sed_replace_basic() {
        let (ws, root) = temp_workspace("sed-basic");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "hello world").unwrap();
        let out = execute_tool(
            "sed_replace",
            &json!({"path": "test.txt", "pattern": "world", "replacement": "rust"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("replaced 1"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "hello rust");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn sed_replace_regex_with_capture_groups() {
        let (ws, root) = temp_workspace("sed-capture");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "foo123bar456baz").unwrap();
        let out = execute_tool(
            "sed_replace",
            &json!({"path": "test.txt", "pattern": "(\\d+)", "replacement": "[$1]"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("replaced 2"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "foo[123]bar[456]baz");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn sed_replace_case_insensitive() {
        let (ws, root) = temp_workspace("sed-case");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "Hello HELLO hello").unwrap();
        let out = execute_tool(
            "sed_replace",
            &json!({"path": "test.txt", "pattern": "hello", "replacement": "hi", "flags": "i"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("replaced 3"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "hi hi hi");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn sed_replace_no_match_is_error() {
        let (ws, root) = temp_workspace("sed-nomatch");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "hello world").unwrap();
        let out = execute_tool(
            "sed_replace",
            &json!({"path": "test.txt", "pattern": "zzz", "replacement": "aaa"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("not found"), "{}", out.output);
        // File should be unchanged
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert_eq!(content, "hello world");
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn sed_replace_invalid_regex_is_error() {
        let (ws, root) = temp_workspace("sed-badregex");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "hello").unwrap();
        let out = execute_tool(
            "sed_replace",
            &json!({"path": "test.txt", "pattern": "[invalid", "replacement": "x"}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("invalid regex"), "{}", out.output);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn glob_files_finds_matching() {
        let (ws, root) = temp_workspace("glob");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("a.rs"), "fn main() {}" ).unwrap();
        std::fs::write(dir.join("b.rs"), "fn foo() {}" ).unwrap();
        std::fs::write(dir.join("c.txt"), "hello").unwrap();
        let out = glob_files(&dir, "*.rs").expect("glob");
        assert!(out.contains("a.rs"), "{}", out);
        assert!(out.contains("b.rs"), "{}", out);
        assert!(!out.contains("c.txt"), "should not contain c.txt: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn glob_files_nested_pattern() {
        let (ws, root) = temp_workspace("glob-nested");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::create_dir_all(dir.join("src")).unwrap();
        std::fs::write(dir.join("src/main.rs"), "fn main() {}" ).unwrap();
        std::fs::write(dir.join("lib.rs"), "pub fn f() {}" ).unwrap();
        let out = glob_files(&dir, "**/*.rs").expect("glob");
        // Platform-agnostic: glob returns OS separators
        let has_main = out.contains("main.rs");
        assert!(has_main, "expected main.rs in: {}", out);
        assert!(out.contains("lib.rs"), "{}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn glob_files_no_matches() {
        let (ws, root) = temp_workspace("glob-empty");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let out = glob_files(&dir, "*.xyz").expect("glob");
        assert!(out.contains("no matches"), "{}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn tree_shows_nested_structure() {
        let (ws, root) = temp_workspace("tree");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::create_dir_all(dir.join("src")).unwrap();
        std::fs::create_dir_all(dir.join("src/utils")).unwrap();
        std::fs::write(dir.join("README.md"), "hi").unwrap();
        std::fs::write(dir.join("src/main.rs"), "fn main() {}" ).unwrap();
        std::fs::write(dir.join("src/utils/helpers.rs"), "pub fn f() {}" ).unwrap();
        std::fs::write(dir.join("lib.rs"), "pub fn g() {}" ).unwrap();
        let out = tree(&dir, "", 4).expect("tree");
        // Root name should appear
        assert!(out.contains("agent-18080"), "{}", out);
        // Tree connectors should be present
        assert!(out.contains("├──"), "expected tree connectors: {}", out);
        assert!(out.contains("└──"), "expected tree connectors: {}", out);
        // Directories should have trailing slash
        assert!(out.contains("src/"), "expected src/: {}", out);
        // Files should appear
        assert!(out.contains("README.md"), "{}", out);
        assert!(out.contains("main.rs"), "{}", out);
        assert!(out.contains("helpers.rs"), "{}", out);
        assert!(out.contains("lib.rs"), "{}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn tree_respects_max_depth() {
        let (ws, root) = temp_workspace("tree-depth");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::create_dir_all(dir.join("a/b/c")).unwrap();
        std::fs::write(dir.join("a/b/c/deep.txt"), "x").unwrap();
        std::fs::write(dir.join("a/shallow.txt"), "y").unwrap();
        // depth=1 should show a/ but not a/b/
        let out1 = tree(&dir, "", 1).expect("tree depth 1");
        assert!(out1.contains("a/"), "expected a/: {}", out1);
        assert!(!out1.contains("b/"), "should NOT contain b/ at depth 1: {}", out1);
        // depth=2 should show a/b/ but not a/b/c/
        let out2 = tree(&dir, "", 2).expect("tree depth 2");
        assert!(out2.contains("b/"), "expected b/: {}", out2);
        assert!(!out2.contains("deep.txt"), "should NOT contain deep.txt at depth 2: {}", out2);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn tree_respects_subpath() {
        let (ws, root) = temp_workspace("tree-subpath");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::create_dir_all(dir.join("src/utils")).unwrap();
        std::fs::write(dir.join("src/main.rs"), "fn main() {}" ).unwrap();
        std::fs::write(dir.join("src/utils/helpers.rs"), "pub fn f() {}" ).unwrap();
        std::fs::write(dir.join("lib.rs"), "pub fn g() {}" ).unwrap();
        let out = tree(&dir, "src", 4).expect("tree subpath");
        // Should show contents of src/ but not lib.rs
        assert!(out.contains("main.rs"), "{}", out);
        assert!(out.contains("utils/"), "{}", out);
        assert!(out.contains("helpers.rs"), "{}", out);
        assert!(!out.contains("lib.rs"), "should not contain lib.rs: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn tree_empty_directory() {
        let (ws, root) = temp_workspace("tree-empty");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let out = tree(&dir, "", 4).expect("tree");
        // Should show root name but no tree lines
        assert!(out.contains("agent-18080"), "{}", out);
        assert!(!out.contains("├──"), "should have no branches: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn tree_via_execute_tool() {
        let (ws, root) = temp_workspace("tree-execute");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("a.txt"), "x").unwrap();
        std::fs::write(dir.join("sub/b.txt"), "y").unwrap();
        let out = execute_tool(
            "tree",
            &json!({"max_depth": 2}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("a.txt"), "{}", out.output);
        assert!(out.output.contains("sub/"), "{}", out.output);
        assert!(out.output.contains("b.txt"), "{}", out.output);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn apply_patch_creates_new_file() {
        let (ws, root) = temp_workspace("patch-create");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let diff = "--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
";
        let out = execute_tool(
            "apply_patch",
            &json!({"diff": diff}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("created new.txt"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("new.txt")).unwrap();
        assert!(content.contains("hello"));
        assert!(content.contains("world"));
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn apply_patch_modifies_existing_file() {
        let (ws, root) = temp_workspace("patch-modify");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("test.txt"), "line1\nline2\nline3\n").unwrap();
        let diff = "--- a/test.txt
+++ b/test.txt
@@ -1,3 +1,3 @@
 line1
-line2
+line2_modified
 line3
";
        let out = execute_tool(
            "apply_patch",
            &json!({"diff": diff}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("patched test.txt"), "{}", out.output);
        let content = std::fs::read_to_string(dir.join("test.txt")).unwrap();
        assert!(content.contains("line2_modified"));
        assert!(!content.contains("\nline2\n"));
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn apply_patch_deletes_file() {
        let (ws, root) = temp_workspace("patch-delete");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        std::fs::write(dir.join("obsolete.txt"), "delete me").unwrap();
        let diff = "--- a/obsolete.txt
+++ /dev/null
";
        let out = execute_tool(
            "apply_patch",
            &json!({"diff": diff}),
            &ws,
            18080,
            &cfg(),
            &SkillDb::disabled(),
        )
        .await;
        assert!(out.output.contains("deleted obsolete.txt"), "{}", out.output);
        assert!(!dir.join("obsolete.txt").exists());
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn simple_url_encode_handles_special_chars() {
        assert_eq!(simple_url_encode("hello world"), "hello%20world");
        assert_eq!(simple_url_encode("a&b=c"), "a%26b%3Dc");
        assert_eq!(simple_url_encode("abc123"), "abc123");
    }

    #[test]
    fn strip_html_removes_tags() {
        let html = "<html><body><h1>Title</h1><p>Content</p></body></html>";
        let text = strip_html(html);
        assert!(text.contains("Title"));
        assert!(text.contains("Content"));
        assert!(!text.contains("<h1>"));
        assert!(!text.contains("</p>"));
    }

    #[test]
    fn strip_html_removes_script_and_style() {
        let html = "<p>visible</p><script>alert('x')</script><style>.x{color:red}</style><p>also visible</p>";
        let text = strip_html(html);
        assert!(text.contains("visible"));
        assert!(!text.contains("alert"));
        assert!(!text.contains("color:red"));
    }

    #[tokio::test]
    async fn git_status_shows_modified_files() {
        let (ws, root) = temp_workspace("git-status");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        // Init a git repo in the agent dir
        let init = std::process::Command::new("git")
            .args(["init"])
            .current_dir(&dir)
            .output();
        if init.is_err() || !init.unwrap().status.success() {
            std::fs::remove_dir_all(&root).ok();
            return; // git not available, skip
        }
        // Configure git user for commits
        let _ = std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&dir)
            .output();
        // Create and commit a file
        std::fs::write(dir.join("a.txt"), "original").unwrap();
        let _ = std::process::Command::new("git")
            .args(["add", "a.txt"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&dir)
            .output();
        // Modify the file
        std::fs::write(dir.join("a.txt"), "modified").unwrap();
        // git_status should show the modification
        let out = git_status(&dir, &cfg()).await.expect("git_status");
        assert!(out.contains("a.txt"), "expected a.txt in: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn git_status_clean_tree() {
        let (ws, root) = temp_workspace("git-status-clean");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let init = std::process::Command::new("git")
            .args(["init"])
            .current_dir(&dir)
            .output();
        if init.is_err() || !init.unwrap().status.success() {
            std::fs::remove_dir_all(&root).ok();
            return;
        }
        let _ = std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&dir)
            .output();
        std::fs::write(dir.join("a.txt"), "content").unwrap();
        let _ = std::process::Command::new("git")
            .args(["add", "a.txt"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&dir)
            .output();
        let out = git_status(&dir, &cfg()).await.expect("git_status");
        assert!(out.contains("clean"), "expected clean in: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn git_diff_shows_changes() {
        let (ws, root) = temp_workspace("git-diff");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let init = std::process::Command::new("git")
            .args(["init"])
            .current_dir(&dir)
            .output();
        if init.is_err() || !init.unwrap().status.success() {
            std::fs::remove_dir_all(&root).ok();
            return;
        }
        let _ = std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&dir)
            .output();
        std::fs::write(dir.join("a.txt"), "original").unwrap();
        let _ = std::process::Command::new("git")
            .args(["add", "a.txt"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&dir)
            .output();
        // Modify
        std::fs::write(dir.join("a.txt"), "modified").unwrap();
        let out = git_diff(&dir, "", false, "", &cfg()).await.expect("git_diff");
        assert!(out.contains("-original"), "expected -original in: {}", out);
        assert!(out.contains("+modified"), "expected +modified in: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn git_diff_staged() {
        let (ws, root) = temp_workspace("git-diff-staged");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        let init = std::process::Command::new("git")
            .args(["init"])
            .current_dir(&dir)
            .output();
        if init.is_err() || !init.unwrap().status.success() {
            std::fs::remove_dir_all(&root).ok();
            return;
        }
        let _ = std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&dir)
            .output();
        std::fs::write(dir.join("a.txt"), "v1").unwrap();
        let _ = std::process::Command::new("git")
            .args(["add", "a.txt"])
            .current_dir(&dir)
            .output();
        let _ = std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&dir)
            .output();
        // Modify and stage
        std::fs::write(dir.join("a.txt"), "v2").unwrap();
        let _ = std::process::Command::new("git")
            .args(["add", "a.txt"])
            .current_dir(&dir)
            .output();
        let out = git_diff(&dir, "", true, "", &cfg()).await.expect("git_diff staged");
        assert!(out.contains("-v1"), "expected -v1 in: {}", out);
        assert!(out.contains("+v2"), "expected +v2 in: {}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn git_diff_not_a_repo() {
        let (ws, root) = temp_workspace("git-diff-norepo");
        let dir = ws.ensure_agent_dir(18080).expect("agent dir");
        // No git init — should fail gracefully
        let out = git_diff(&dir, "", false, "", &cfg()).await;
        assert!(out.is_err(), "expected error for non-repo: {:?}", out);
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn full_loop_writes_runs_and_answers() {
        let (ws, root) = temp_workspace("loop");
        let pool = Arc::new(DemandPool::builder(Pool::CodingAgent, spec(true)).build(&[18079]));
        let calls = Arc::new(AtomicUsize::new(0));
        let calls2 = Arc::clone(&calls);
        let sender: Sender = Arc::new(move |_guard, _req| {
            let n = calls2.fetch_add(1, Ordering::SeqCst);
            let v = if n == 0 {
                json!({"choices": [{"message": {"content": "{\"tool\":\"write_file\",\"args\":{\"path\":\"hello.py\",\"content\":\"print(1)\"}}"}}]})
            } else {
                json!({"choices": [{"message": {"content": "{\"answer\":\"done\"}"}}]})
            };
            Box::pin(async move { Ok::<Value, ProxyError>(v) })
        });
        let tl = ToolLoop::with_sender(pool, ws, cfg(), SkillDb::disabled(), sender);
        let metrics = Arc::new(MetricsStore::open(&std::env::temp_dir().join(format!(
            "omega-tool-loop-metrics-{}.jsonl",
            std::process::id()
        ))));
        let req = ChatRequest {
            model: None,
            messages: vec![CM {
                role: "user".to_owned(),
                content: "write hello.py and run it".to_owned(),
            }],
            max_tokens: Some(64),
            temperature: None,
            top_p: None,
            stream: None,
        };
        let resp = tl.run(&req, &metrics).await.expect("loop ok");
        assert_eq!(resp.choices[0].message.content, "done");
        assert_eq!(resp.lane.as_deref(), Some("18079"));
        let validation = resp.validation.expect("validation report");
        assert_eq!(validation["files"].as_array().map(|a| a.len()), Some(1));
        assert!(root.join("agent-18079/hello.py").exists());
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(metrics.active_run_etas().is_empty());
        std::fs::remove_dir_all(&root).ok();
    }

    #[tokio::test]
    async fn concatenated_tool_and_answer_executes_then_finishes() {
        // Real-world shape from the live smoke: the 1.2B emits
        // `{"tool": write_file} {"answer": ...}` in ONE reply. The file must
        // actually be written (side effect) and the answer must be final.
        let (ws, root) = temp_workspace("concat");
        let pool = Arc::new(DemandPool::builder(Pool::CodingAgent, spec(true)).build(&[18079]));
        let sender: Sender = Arc::new(|_guard, _req| {
            let v = json!({"choices": [{"message": {"content":
                "{\"tool\":\"write_file\",\"args\":{\"path\":\"hello.py\",\"content\":\"print(1)\"}} {\"answer\":\"file created and executed: Hello, Ashat\"}"}}]});
            Box::pin(async move { Ok::<Value, ProxyError>(v) })
        });
        let tl = ToolLoop::with_sender(pool, ws, cfg(), SkillDb::disabled(), sender);
        let metrics = Arc::new(MetricsStore::open(&std::env::temp_dir().join(format!(
            "omega-tool-loop-metrics-concat-{}.jsonl",
            std::process::id()
        ))));
        let req = ChatRequest {
            model: None,
            messages: vec![CM {
                role: "user".to_owned(),
                content: "write hello.py and run it".to_owned(),
            }],
            max_tokens: Some(64),
            temperature: None,
            top_p: None,
            stream: None,
        };
        let resp = tl.run(&req, &metrics).await.expect("loop ok");
        assert!(resp.choices[0].message.content.contains("Hello, Ashat"));
        // The tool really ran: the file exists and validation saw it.
        assert!(root.join("agent-18079/hello.py").exists());
        let validation = resp.validation.expect("validation report");
        assert_eq!(validation["files"].as_array().map(|a| a.len()), Some(1));
        std::fs::remove_dir_all(&root).ok();
    }
}
