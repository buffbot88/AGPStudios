import gc
import json
import os
import threading
import time
from typing import Any, Literal

import gradio as gr
from huggingface_hub import hf_hub_download
from llama_cpp import Llama

# Fast-lane model
FAST_MODEL_REPO = os.getenv("FAST_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K").strip()
FAST_MODEL_FILE = os.getenv("FAST_MODEL_FILE", "LFM2.5-350M-Q6_K.gguf").strip()

# Slow/reasoning-lane model
SLOW_MODEL_REPO = os.getenv("SLOW_MODEL_REPO", "RipBuffy/LFM2.5-Q6_K").strip()
SLOW_MODEL_FILE = os.getenv("SLOW_MODEL_FILE", "LFM2.5-1.2B-Instruct-Q6_K.gguf").strip()

MODEL_REVISION = os.getenv("MODEL_REVISION", "main").strip()
HF_TOKEN = os.getenv("HF_TOKEN") or None

N_CTX_FAST = int(os.getenv("N_CTX_FAST", "1024"))
N_CTX_SLOW = int(os.getenv("N_CTX_SLOW", "1536"))
N_THREADS = int(os.getenv("N_THREADS", "2"))
N_BATCH = int(os.getenv("N_BATCH", "128"))
MAX_TOKENS_LIMIT = int(os.getenv("MAX_TOKENS_LIMIT", "256"))

FAST_SYSTEM_PROMPT = os.getenv(
    "FAST_SYSTEM_PROMPT",
    "You are Ashat's fast conversational lane. Be concise, natural, and helpful.",
)

SLOW_SYSTEM_PROMPT = os.getenv(
    "SLOW_SYSTEM_PROMPT",
    "You are Ashat's reasoning lane. Think carefully and return a clear final answer.",
)

_models: dict[str, Llama | None] = {"fast": None, "slow": None}
_model_paths: dict[str, str | None] = {"fast": None, "slow": None}

# One global inference lock: zeroGPU shares a single A10G between lanes; serializing avoids GPU-context contention.
_inference_lock = threading.Lock()
_load_locks = {"fast": threading.Lock(), "slow": threading.Lock()}
_started_at = time.time()


def model_config(lane: Literal["fast", "slow"]) -> tuple[str, str, int]:
    if lane == "fast":
        return FAST_MODEL_REPO, FAST_MODEL_FILE, N_CTX_FAST
    return SLOW_MODEL_REPO, SLOW_MODEL_FILE, N_CTX_SLOW


def load_model(lane: Literal["fast", "slow"]) -> Llama:
    existing = _models[lane]
    if existing is not None:
        return existing

    with _load_locks[lane]:
        existing = _models[lane]
        if existing is not None:
            return existing

        repo, filename, context_size = model_config(lane)
        if not repo:
            raise RuntimeError(f"{lane.upper()}_MODEL_REPO is not configured")
        if not filename:
            raise RuntimeError(f"{lane.upper()}_MODEL_FILE is not configured")

        path = hf_hub_download(
            repo_id=repo,
            filename=filename,
            revision=MODEL_REVISION,
            token=HF_TOKEN,
        )

        model = Llama(
            model_path=path,
            n_ctx=context_size,
            n_threads=N_THREADS,
            n_threads_batch=N_THREADS,
            n_batch=N_BATCH,
            n_gpu_layers=99,  # offload all available layers to the zeroGPU A10G; safe upper bound that works for any small/medium GGUF
            use_mmap=True,
            use_mlock=False,
            verbose=False,
        )

        _model_paths[lane] = path
        _models[lane] = model
        return model


def parse_history(history_json: str) -> list[dict[str, str]]:
    try:
        data = json.loads(history_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"history_json is invalid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError("history_json must contain a JSON list")

    messages: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()

        if role in {"system", "user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    return messages


def generate(
    lane: Literal["fast", "slow"],
    message: str,
    history_json: str,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> dict[str, Any]:
    if not message or not message.strip():
        return {"ok": False, "lane": lane, "error": "message cannot be empty"}

    try:
        history = parse_history(history_json)
        messages: list[dict[str, str]] = []

        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})

        messages.extend(history)
        messages.append({"role": "user", "content": message.strip()})

        max_tokens = max(1, min(int(max_tokens), MAX_TOKENS_LIMIT))
        model = load_model(lane)

        started = time.perf_counter()

        # Prevent simultaneous CPU inference by the two models.
        with _inference_lock:
            result = model.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=float(temperature),
                top_p=float(top_p),
                stream=False,
            )

        elapsed = time.perf_counter() - started
        text = result["choices"][0]["message"]["content"]

        return {
            "ok": True,
            "lane": lane,
            "model": model_config(lane)[1],
            "response": text,
            "elapsed_seconds": round(elapsed, 3),
            "usage": result.get("usage", {}),
        }
    except Exception as exc:
        return {"ok": False, "lane": lane, "error": str(exc)}


def fast_chat(
    message: str,
    history_json: str = "[]",
    system_prompt: str = FAST_SYSTEM_PROMPT,
    max_tokens: int = 128,
    temperature: float = 0.7,
    top_p: float = 0.9,
) -> dict[str, Any]:
    return generate(
        "fast",
        message,
        history_json,
        system_prompt,
        max_tokens,
        temperature,
        top_p,
    )


def slow_chat(
    message: str,
    history_json: str = "[]",
    system_prompt: str = SLOW_SYSTEM_PROMPT,
    max_tokens: int = 192,
    temperature: float = 0.4,
    top_p: float = 0.9,
) -> dict[str, Any]:
    return generate(
        "slow",
        message,
        history_json,
        system_prompt,
        max_tokens,
        temperature,
        top_p,
    )


def health(load_models: bool = False) -> dict[str, Any]:
    errors: dict[str, str] = {}

    if load_models:
        for lane in ("fast", "slow"):
            try:
                load_model(lane)
            except Exception as exc:
                errors[lane] = str(exc)

    return {
        "status": "error" if errors else "ready",
        "uptime_seconds": round(time.time() - _started_at, 1),
        "fast": {
            "repo": FAST_MODEL_REPO or None,
            "file": FAST_MODEL_FILE or None,
            "loaded": _models["fast"] is not None,
        },
        "slow": {
            "repo": SLOW_MODEL_REPO or None,
            "file": SLOW_MODEL_FILE or None,
            "loaded": _models["slow"] is not None,
        },
        "errors": errors,
    }


def unload(lane: Literal["fast", "slow", "both"]) -> dict[str, Any]:
    targets = ("fast", "slow") if lane == "both" else (lane,)

    with _inference_lock:
        for target in targets:
            _models[target] = None
            _model_paths[target] = None
        gc.collect()

    return {
        "ok": True,
        "unloaded": list(targets),
        "fast_loaded": _models["fast"] is not None,
        "slow_loaded": _models["slow"] is not None,
    }


with gr.Blocks(title="AshatOS Dual GGUF Host (zeroGPU)") as demo:
    gr.Markdown(
        """
        # AshatOS Dual GGUF Host (zeroGPU)

        Two GGUF lanes running on ZeroGPU:

        - `/fast_chat` — short conversational responses
        - `/slow_chat` — deeper reasoning responses
        - `/health` — wake/readiness check
        """
    )

    with gr.Tab("Fast Lane"):
        fast_message = gr.Textbox(label="Message")
        fast_history = gr.Textbox(label="History JSON", value="[]", lines=4)
        fast_system = gr.Textbox(
            label="System Prompt",
            value=FAST_SYSTEM_PROMPT,
            lines=3,
        )
        fast_max = gr.Slider(1, MAX_TOKENS_LIMIT, value=128, step=1)
        fast_temp = gr.Slider(0.0, 2.0, value=0.7, step=0.05)
        fast_top_p = gr.Slider(0.0, 1.0, value=0.9, step=0.05)
        fast_button = gr.Button("Run Fast Lane", variant="primary")
        fast_output = gr.JSON()

        fast_button.click(
            fn=fast_chat,
            inputs=[
                fast_message,
                fast_history,
                fast_system,
                fast_max,
                fast_temp,
                fast_top_p,
            ],
            outputs=fast_output,
            api_name="fast_chat",
            concurrency_limit=1,
            concurrency_id="gguf_inference",
        )

    with gr.Tab("Slow Lane"):
        slow_message = gr.Textbox(label="Message")
        slow_history = gr.Textbox(label="History JSON", value="[]", lines=4)
        slow_system = gr.Textbox(
            label="System Prompt",
            value=SLOW_SYSTEM_PROMPT,
            lines=3,
        )
        slow_max = gr.Slider(1, MAX_TOKENS_LIMIT, value=192, step=1)
        slow_temp = gr.Slider(0.0, 2.0, value=0.4, step=0.05)
        slow_top_p = gr.Slider(0.0, 1.0, value=0.9, step=0.05)
        slow_button = gr.Button("Run Slow Lane", variant="primary")
        slow_output = gr.JSON()

        slow_button.click(
            fn=slow_chat,
            inputs=[
                slow_message,
                slow_history,
                slow_system,
                slow_max,
                slow_temp,
                slow_top_p,
            ],
            outputs=slow_output,
            api_name="slow_chat",
            concurrency_limit=1,
            concurrency_id="gguf_inference",
        )

    with gr.Tab("Status"):
        preload = gr.Checkbox(
            label="Load both models during health check",
            value=False,
        )
        health_button = gr.Button("Check Health")
        health_output = gr.JSON()
        health_button.click(
            fn=health,
            inputs=preload,
            outputs=health_output,
            api_name="health",
            concurrency_limit=1,
        )

        lane = gr.Dropdown(
            choices=["fast", "slow", "both"],
            value="both",
            label="Unload",
        )
        unload_button = gr.Button("Unload Selected")
        unload_output = gr.JSON()
        unload_button.click(
            fn=unload,
            inputs=lane,
            outputs=unload_output,
            api_name="unload",
            concurrency_limit=1,
        )

demo.queue(default_concurrency_limit=1, max_size=12)

if __name__ == "__main__":
    demo.launch()
