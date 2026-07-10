---
title: AshatOS Dual GGUF Host
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
license: mit
---

# AshatOS Dual GGUF Host

> Hugging Face Spaces installs Python dependencies from `requirements.txt` and system packages from `packages.txt` automatically on every boot. The host is **Spaces `zeroGPU`** (A10G-class, CUDA 12.x). `requirements.txt` uses `--prefer-binary` with `--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124` so pip takes a prebuilt CUDA-enabled `llama-cpp-python` wheel when one matches; if no wheel exists for the runtime, the `--config-settings="cmake.args=-DGGML_CUDA=on"` flag falls back to a CUDA-enabled source compile. `packages.txt` ships `build-essential`, `cmake`, `nvidia-cuda-toolkit` (CUDA runtime + `nvcc`), plus `cuda-cudart-dev` and `cuda-cublas-dev` (the `-dev` headers + symlinks needed for source compile of `llama-cpp-python` to link against CUDA). The runtime meta-package alone was insufficient — a real boot showed `ModuleNotFoundError: No module named 'llama_cpp'` when only `nvidia-cuda-toolkit` was present in `packages.txt`.

## Required variables

The block below lists the **built-in defaults** shipped in `app.py` (`RipBuffy/LFM2.5-Q6_K` for both lanes' repos with different GGUF files). To deploy against your own models, copy these into your Space's **Settings → Repository secrets** (or your runtime env) and replace `FAST_MODEL_REPO` / `FAST_MODEL_FILE` / `SLOW_MODEL_REPO` / `SLOW_MODEL_FILE` with your own HF repo + GGUF filename.

```text
FAST_MODEL_REPO=RipBuffy/LFM2.5-Q6_K
FAST_MODEL_FILE=LFM2.5-350M-Q6_K.gguf

SLOW_MODEL_REPO=RipBuffy/LFM2.5-Q6_K
SLOW_MODEL_FILE=LFM2.5-1.2B-Instruct-Q6_K.gguf

MODEL_REVISION=main
N_THREADS=2
N_BATCH=128
N_CTX_FAST=1024
N_CTX_SLOW=1536
MAX_TOKENS_LIMIT=256
```

Optional system prompts (override to customize each lane):

```text
FAST_SYSTEM_PROMPT=You are Ashat's fast conversational lane.
SLOW_SYSTEM_PROMPT=You are Ashat's careful reasoning lane.
```

For private model repositories, add `HF_TOKEN` as a Space secret.

## API endpoints

- `/fast_chat`
- `/slow_chat`
- `/health`
- `/unload`

Both models are loaded lazily. Only one inference runs at a time.
