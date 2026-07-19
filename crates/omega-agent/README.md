---
title: AshatOS Neural Host
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
---

# AshatOS BrainStem Inference Host

A private inference appliance running on Hugging Face Spaces (zeroGPU).

**Public surface:** Read-only telemetry dashboard  
**Private surface:** One authenticated GGUF inference lane (BrainStem)

The Space is **not** AshatOS itself. It is a focused token-generation host that
accepts authenticated requests, runs inference on-demand, collects metrics,
and displays a sanitized public dashboard.

---

## How it works

1. On boot, `app.py` installs or detects `llama-server` (prebuilt binary from
   GitHub releases, with CPU-only source build fallback).
2. The GGUF model downloads from Hugging Face Hub in a background thread.
3. The ZeroGPU runtime is used per-request: each inference call starts
   `llama-server`, runs one completion, collects metrics, and terminates.
4. The dashboard is server-rendered HTML at GET /; a JS setInterval polls /api/dashboard_html for live updates (no inference controls).
5. FastAPI routes expose OpenAI-compatible `/v1/chat/completions` and
   `/v1/models` endpoints.
6. Authentication via `X-Ashat-Key` header (constant-time HMAC comparison).

---

## Required Space Secrets

| Secret | Purpose |
|---|---|
| `HF_TOKEN` | Hugging Face access token (for private repos) |
| `ASHAT_BRAINSTEM_KEY` | BrainStem lane API key |
| `ASHAT_ADMIN_KEY` | Admin operations (benchmark) key |

---

## Environment Variables

### Model Configuration

| Variable | Default | Description |
|---|---|---|
| `BRAINSTEM_MODEL_REPO` | `buckets/stressthismess/ashatos-storage` | BrainStem model repository |
| `BRAINSTEM_MODEL_FILE` | `LFM2.5-1.2B-Instruct-Q8_0.gguf` | BrainStem GGUF filename |
| `MODEL_REVISION` | `main` | Hugging Face model revision |

### Runtime Configuration

| Variable | Default | Description |
|---|---|---|
| `INTERNAL_PORT` | `18080` | Port for llama-server |
| `N_THREADS` | `2` | CPU threads for tokenization/sampling |
| `N_BATCH` | `128` | Batch size |
| `BRAINSTEM_CTX` | `8192` | BrainStem context size |
| `BRAINSTEM_MAX_TOKENS` | `8192` | BrainStem max output tokens |
| `BRAINSTEM_GPU_DURATION` | `120` | ZeroGPU duration for BrainStem (seconds) |
| `QUEUE_LIMIT` | `16` | Max queued requests |
| `PUBLIC_REFRESH_SECONDS` | `10` | Dashboard auto-refresh interval |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LLAMA_SERVER_VERSION` | `b9945` | Specific llama.cpp release tag (pinned for reproducible installs). Set to `"latest"` to track upstream. |
| `LLAMA_SERVER_HF_REPO` | `stressthismess/llama-server-mirror` | HF mirror repo consulted if every GitHub-release strategy fails. |
| `LLAMA_SERVER_HF_FILE` | `llama-server-{tag}` | Filename in the mirror repo (auto-derived from the pinned tag if left blank). |
| `LLAMA_SERVER_PATH` | *(none)* | Manual path to llama-server binary |

---

## API Endpoints

### OpenAI-Compatible (FastAPI)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | No | Public telemetry dashboard (HTML, JS-polled) |
| `GET` | `/v1/models` | No | List available models |
| `POST` | `/v1/chat/completions` | `X-Ashat-Key` | Chat completions |
| `GET` | `/health` | No | Health check |
| `GET` | `/api/public_status` | No | Public status snapshot |
| `GET` | `/api/public_metrics` | No | Public metrics snapshot |
| `GET` | `/api/dashboard_html` | No | Status + card HTML snippets (live JS poll) |

---

## Logs

All logs written to `./logs/`:

- `brainstem.out.log` / `brainstem.err.log` — llama-server output
- `llama_install.log` — binary install diagnostics

---

## Client Usage

```python
import httpx
import json

# OpenAI-compatible endpoint (requires HF auth for zeroGPU Space)
resp = httpx.post(
    "https://stressthismess-ashatos.hf.space/v1/chat/completions",
    headers={
        "Authorization": "Bearer <HF_TOKEN>",
        "X-Ashat-Key": "<ASHAT_BRAINSTEM_KEY>",
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

## Tests

A small unittest suite covers the install-asset-selection logic without
booting the Space. The `install_strategies` module is dependency-light
(no gradio, fastapi, huggingface_hub), so tests run on a vanilla Python install:

```bash
python -m unittest discover tests -v
```

The suite pins:
- Real-asset filtering (rejects cudart-* / cross-tag / non-archive names).
- Fallback to "every archive in the release" when no linux binary matches.
- Empty-asset fallback to URL-pattern guesses.
- URL guesses that disagree with the GitHub release JSON are dropped.
