---
title: ASHAT Llama Server
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
license: mit
---

# AshatOS Dual llama-server Host

> Hugging Face Space that auto-installs or builds `llama-server`, then spawns
> **two local GGUF model servers** behind a single Gradio UI. No runtime
> dependency on `llama-cpp-python`.

## How it works

1. On boot, `app.py` detects whether `llama-server` is available.
2. If missing, it attempts (in order):
   - Download a prebuilt binary from GitHub releases
   - Clone and build `llama.cpp` from source (CPU-only)
3. Two GGUF models are downloaded from Hugging Face Hub (or used from a local path):
   - **MainBrain** (port `18080`, ctx `1024`) — fast/350M model
   - **MicroBrain** (port `18081`, ctx `1536`) — slow/1.2B model
4. Each model gets its own `llama-server` subprocess.
5. The Gradio UI waits for both servers (or degrades gracefully) and routes
   user prompts to the selected model via `POST /v1/chat/completions`.

## Required variables

These are the **built-in defaults**. Override them via Space secrets or env vars.

```text
FAST_MODEL_REPO=RipBuffy/LFM2.5-Q6_K
FAST_MODEL_FILE=LFM2.5-350M-Q6_K.gguf

SLOW_MODEL_REPO=RipBuffy/LFM2.5-Q6_K
SLOW_MODEL_FILE=LFM2.5-1.2B-Instruct-Q6_K.gguf

MODEL_REVISION=main
N_CTX_FAST=1024
N_CTX_SLOW=1536
```

**Optional overrides:**

| Variable | Default | Purpose |
|---|---|---|
| `MAINBRAIN_PORT` | `18080` | Port for MainBrain server |
| `MICROBRAIN_PORT` | `18081` | Port for MicroBrain server |
| `MAINBRAIN_CTX` | `1024` | Context size for MainBrain |
| `MICROBRAIN_CTX` | `1536` | Context size for MicroBrain |
| `MAINBRAIN_MODEL_PATH` | *(none)* | Direct local path to GGUF (bypasses HF download) |
| `MICROBRAIN_MODEL_PATH` | *(none)* | Direct local path to GGUF (bypasses HF download) |
| `LLAMA_THREADS` | `2` | CPU threads for tokenization/sampling |
| `LLAMA_BATCH_SIZE` | `128` | Batch size for prompt processing |
| `LLAMA_SERVER_PATH` | *(none)* | Explicit path to `llama-server` binary |
| `AUTO_BUILD_LLAMA_SERVER` | `1` | Set to `0` to skip auto-install |

For private model repositories, add `HF_TOKEN` as a Space secret.

## API endpoints

- `/chat` — send a prompt to a selected model
- `/status` — server status (ready/error/running)
- `/health` — (legacy health, maintained for backward compat if needed)

## Logs

All logs are written to `./logs/`:

- `mainbrain.out.log` / `mainbrain.err.log`
- `microbrain.out.log` / `microbrain.err.log`
- `llama_install.log`

## Client usage

See `dual_model_client_example.py`.

```python
from gradio_client import Client

client = Client("your-space-id")
result = client.predict(
    model_name="MainBrain",
    message="Hello!",
    max_tokens=96,
    temperature=0.7,
    top_p=0.9,
    api_name="/chat",
)
```
