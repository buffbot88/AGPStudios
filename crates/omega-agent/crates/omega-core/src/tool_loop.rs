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
}

impl ToolLoopConfig {
    pub fn from_sections(
        tl: &ToolLoopSection,
        inference_max_tokens: u32,
        inference_timeout_seconds: u64,
    ) -> Self {
        Self {
            max_iterations: tl.max_iterations,
            command_timeout: Duration::from_secs(tl.command_timeout_seconds),
            output_max_chars: tl.output_max_chars,
            max_tokens: inference_max_tokens,
            acquire_timeout: Duration::from_secs(inference_timeout_seconds),
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
             {{\"tool\": \"list_dir\"}}\n\
             {{\"tool\": \"read_file\", \"args\": {{\"path\": \"relative/path\"}}}}\n\
             {{\"tool\": \"write_file\", \"args\": {{\"path\": \"relative/path\", \"content\": \"...\"}}}}\n\
             {{\"tool\": \"run_command\", \"args\": {{\"command\": \"shell command\"}}}}\n\
             {{\"tool\": \"validate\", \"args\": {{\"path\": \"relative/path\"}}}}\n\
             {{\"tool\": \"skill\", \"args\": {{\"name\": \"skill-name\"}}}}\n\
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
        "list_dir" => list_dir(&dir),
        "read_file" => read_file(&dir, path_arg, cfg.output_max_chars),
        "write_file" => {
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
        "run_command" => {
            let command = args.get("command").and_then(Value::as_str).unwrap_or("");
            run_command(&dir, command, cfg).await
        }
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
