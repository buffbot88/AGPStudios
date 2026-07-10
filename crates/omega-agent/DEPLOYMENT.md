# Deployment Guide — AshatOS Dual-Lane Inference Host

## Hugging Face Spaces

### Creating the Space

1. Go to https://huggingface.co/new-space
2. Set Space name: `ashatos` (or your preferred name)
3. SDK: **Gradio**
4. Space hardware: **ZeroGPU** (or CPU basic for testing)
5. License: MIT

### Pushing the Code

```bash
git remote add space https://huggingface.co/spaces/YOUR_USERNAME/ashatos
git push space main
```

### Environment Setup

Add these **Space Secrets** in the Settings tab:

| Key | Value |
|---|---|
| `HF_TOKEN` | Your Hugging Face access token (for model downloads) |
| `ASHAT_MICROBRAIN_KEY` | Random 256-bit key (generate with `secrets.token_urlsafe(48)`) |
| `ASHAT_MAINBRAIN_KEY` | Different random 256-bit key |
| `ASHAT_ADMIN_KEY` | Separate key for admin operations |

### Optional Space Variables

These can be set as normal environment variables to override defaults:

| Key | Default | Description |
|---|---|---|
| `MAIN_MODEL_REPO` | `stressthismess/LFM2.5-Q6_K` | Override model repository |
| `MAIN_MODEL_FILE` | `LFM2.5-1.2B-Instruct-Q6_K.gguf` | Override model file |
| `MICRO_MODEL_REPO` | `stressthismess/LFM2.5-Q6_K` | Override model repository |
| `MICRO_MODEL_FILE` | `LFM2.5-350M-Q6_K.gguf` | Override model file |
| `LLAMA_SERVER_VERSION` | `b9945` | Pin a specific llama.cpp release (pinned default; set Space Secret to override) |
| `LLAMA_SERVER_HF_REPO` | `stressthismess/llama-server-mirror` | HF mirror repo used as fallback when GitHub releases are unreachable |
| `LLAMA_SERVER_HF_FILE` | `llama-server-{tag}` | Filename inside the mirror repo (auto-derived from tag if blank) |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose logging |

## Key Generation

Generate secure random keys on your local machine:

```bash
python -c "import secrets; print('MICRO:', secrets.token_urlsafe(48))"
python -c "import secrets; print('MAIN: ', secrets.token_urlsafe(48))"
python -c "import secrets; print('ADMIN:', secrets.token_urlsafe(48))"
```

## Verifying Deployment

1. Wait for the Space to finish building (no more "Building..." status)
2. Visit `https://YOUR_USERNAME-ashatos.hf.space/`
3. You should see the **AshatOS Neural Host** dashboard
4. Check the logs for model download status
5. Send a test inference request:

```bash
curl -X POST https://YOUR_USERNAME-ashatos.hf.space/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Ashat-Key: YOUR_MICROBRAIN_KEY" \
  -d '{"messages":[{"role":"user","content":"Hello!"}],"max_tokens":64}'
```

## Troubleshooting

### "@spaces.GPU function not detected"

This error means the Space hardware is set to zeroGPU but the app doesn't have
a `@spaces.GPU` decorated function. The app includes these — ensure you have
the latest code pushed.

### "llama-server did not become healthy"

Check the logs at `/api/public_status` or the Gradio dashboard. Common causes:
- GGUF file still downloading (first request always waits)
- Port conflict (change `INTERNAL_PORT`)
- Insufficient GPU memory

### Model download fails

Ensure `HF_TOKEN` is set as a Space secret if accessing private repos.
Public repos from the stressthismess organization should work without it.

### Authentication errors

Verify that:
- `ASHAT_MICROBRAIN_KEY` and `ASHAT_MAINBRAIN_KEY` are set as Space **Secrets**
- The keys match between the host and your client configuration
- The `X-Ashat-Key` header is sent (not `Authorization`)
