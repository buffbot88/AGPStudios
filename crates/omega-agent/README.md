# AshatOS Dual-Lane ZeroGPU Inference Host

A private inference appliance running on Hugging Face Spaces (zeroGPU).

**Public surface:** Read-only telemetry dashboard  
**Private surface:** Two authenticated GGUF inference lanes (MicroBrain / MainBrain)

The Space is **not** AshatOS itself. It is a focused token-generation host that
accepts authenticated requests, runs inference on-demand, collects metrics,
and displays a sanitized public dashboard.

---

## How it works

1. On boot, `app.py` installs or detects `llama-server` (prebuilt binary from
   GitHub releases, with CPU-only source build fallback).
2. Both GGUF models download from Hugging Face Hub in background threads.
3. The ZeroGPU runtime is used per-request: each inference call starts
   `llama-server`, runs one completion, collects metrics, and terminates.
4. The Gradio dashboard displays live telemetry (no inference controls).
5. FastAPI routes expose OpenAI-compatible `/v1/chat/completions` and
   `/v1/models` endpoints.
6. Authentication via `X-Ashat-Key` header (constant-time HMAC comparison).

---

## Required Space Secrets

| Secret | Purpose |
|---|---|
| `HF_TOKEN` | Hugging Face access token (for private repos) |
| `ASHAT_MICROBRAIN_KEY` | MicroBrain lane API key |
| `ASHAT_MAINBRAIN_KEY` | MainBrain lane API key |
| `ASHAT_ADMIN_KEY` | Admin operations (benchmark) key |

---

## Environment Variables

### Model Configuration

| Variable | Default | Description |
|---|---|---|
| `MAIN_MODEL_REPO` | `RipBuffy/LFM2.5-Q6_K` | MainBrain model repository |
| `MAIN_MODEL_FILE` | `LFM2.5-1.2B-Instruct-Q6_K.gguf` | MainBrain GGUF filename |
| `MICRO_MODEL_REPO` | `RipBuffy/LFM2.5-Q6_K` | MicroBrain model repository |
| `MICRO_MODEL_FILE` | `LFM2.5-350M-Q6_K.gguf` | MicroBrain GGUF filename |
| `MODEL_REVISION` | `main` | Hugging Face model revision |

### Runtime Configuration

| Variable | Default | Description |
|---|---|---|
| `INTERNAL_PORT` | `18080` | Port for llama-server |
| `N_THREADS` | `2` | CPU threads for tokenization/sampling |
| `N_BATCH` | `128` | Batch size |
| `MAIN_CTX` | `1536` | MainBrain context size |
| `MICRO_CTX` | `1024` | MicroBrain context size |
| `MAIN_MAX_TOKENS` | `256` | MainBrain max output tokens |
| `MICRO_MAX_TOKENS` | `128` | MicroBrain max output tokens |
| `MAIN_GPU_DURATION` | `120` | ZeroGPU duration for MainBrain (seconds) |
| `MICRO_GPU_DURATION` | `60` | ZeroGPU duration for MicroBrain (seconds) |
| `QUEUE_LIMIT` | `16` | Max queued requests |
| `PUBLIC_REFRESH_SECONDS` | `10` | Dashboard auto-refresh interval |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LLAMA_SERVER_VERSION` | *(auto)* | Specific llama.cpp release tag |
| `LLAMA_SERVER_PATH` | *(none)* | Manual path to llama-server binary |

---

## API Endpoints

### OpenAI-Compatible (FastAPI)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | No | Gradio dashboard |
| `GET` | `/v1/models` | No | List available models |
| `POST` | `/v1/chat/completions` | `X-Ashat-Key` | Chat completions |
| `GET` | `/health` | No | Health check |
| `GET` | `/api/public_status` | No | Public status snapshot |
| `GET` | `/api/public_metrics` | No | Public metrics snapshot |

### Gradio Queue API

| API Name | Auth | Description |
|---|---|---|
| `microbrain` | `X-Ashat-Key` | MicroBrain inference |
| `mainbrain` | `X-Ashat-Key` | MainBrain inference |
| `public_status` | No | Status (must be called via gradio_client) |
| `public_metrics` | No | Metrics (must be called via gradio_client) |
| `admin_benchmark` | `X-Ashat-Key` (admin) | Run benchmark |

---

## Logs

All logs written to `./logs/`:

- `microbrain.out.log` / `microbrain.err.log` — llama-server output
- `mainbrain.out.log` / `mainbrain.err.log` — llama-server output
- `llama_install.log` — binary install diagnostics

---

## Client Usage

```python
import httpx
import json

# OpenAI-compatible endpoint (requires HF auth for zeroGPU Space)
resp = httpx.post(
    "https://RipBuffy-ashatos.hf.space/v1/chat/completions",
    headers={
        "Authorization": "Bearer <HF_TOKEN>",
        "X-Ashat-Key": "<ASHAT_MICROBRAIN_KEY>",
    },
    json={
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 64,
    },
)
print(resp.json()["choices"][0]["message"]["content"])
```

## License

MIT
