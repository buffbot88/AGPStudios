# Ashat Neural Network

## BrainStem Inference Host

A private Python AI Neural Network service running on an Oracle Linux ARM64 server. The repository contains the BrainStem application; `llama-server` is the separate local inference runtime it uses.

**Public surface:** Read-only telemetry dashboard  
**Private surface:** One authenticated BrainStem inference lane

The service starts at OS boot through systemd, runs one persistent CPU-only
`llama-server` process, and stores all runtime settings in a protected JSON
file. It does not require an environment file or environment-variable setup.

## Repository boundary

This GitHub repository contains the Ashat Neural Network application, API,
request validation, telemetry, tests, and deployment documentation. The
following are deliberately external or local-only and are never committed:

- `llama-server` binaries and native libraries
- GGUF model files
- `server-config.json` and the real `BRAINSTEM_KEY`
- runtime logs, virtual environments, caches, and build directories

Production installs the runtime at `/usr/local/libexec/ashat-neural-host/` and
stores the model at `/var/lib/ashat-neural-host/`. The checked-in
`server-config.example.json` is only a local development template.

## How it works

1. On boot, the systemd-managed Python service starts one native ARM64
   `llama-server` process in CPU-only mode.
2. The GGUF model is stored in permanent runtime data at `/var/lib/ashat-neural-host`.
3. Each authenticated request reuses the persistent local backend; only one
   inference is admitted at a time on the 1-OCPU host.
4. The dashboard is server-rendered HTML at GET /; JavaScript polls the status
   endpoint for live updates.
5. FastAPI routes expose OpenAI-compatible `/v1/chat/completions` and
   `/v1/models` endpoints.
6. Authentication uses the `X-Ashat-Key` header with constant-time comparison.

## Configuration files

- `server-config.example.json` is safe to commit and provides a local testing
  template with a placeholder BrainStem key.
- `server-config.json` is the real local/production file and is ignored by
  Git. Never commit it or put a real key in the example file.
- Production path: `/home/opc/Projects/AshatNueralHost/server-config.json`.

Protect the production file as `root:opc` mode `640`. It contains the BrainStem key,
model path, llama-server path, CPU settings, queue limit, logging level, and
model limits. The loader checks the production path first, then the application
path/current directory for local development.

For a local test setup, provide the external runtime assets first:

```bash
# Place a compatible llama-server wrapper/binary in bin/.
# Place the GGUF model in models/LFM2.5-1.2B-Instruct-Q8_0.gguf.
cp server-config.example.json server-config.json
# Replace BRAINSTEM_KEY with a local-only test key.
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

The repository intentionally does not download or vendor the runtime binary or
model. Production uses the permanent paths documented in `DEPLOYMENT.md`.

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | No | Public telemetry dashboard |
| `GET` | `/v1/models` | No | List available models |
| `POST` | `/v1/chat/completions` | `X-Ashat-Key` | Chat completions |
| `GET` | `/health` | No | Health check |
| `GET` | `/api/public_status` | No | Public status snapshot |
| `GET` | `/api/public_metrics` | No | Public metrics snapshot |
| `GET` | `/api/dashboard_html` | No | Live dashboard snippets |

## Client usage

```python
import httpx

resp = httpx.post(
    "https://ashatneuralhost.agpstudios.org/v1/chat/completions",
    headers={"X-Ashat-Key": "<YOUR_BRAINSTEM_KEY>"},
    json={
        "model": "brainstem",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 64,
    },
)
print(resp.json()["choices"][0]["message"]["content"])
```

## Deployed model

```text
local runtime model
LFM2.5-1.2B-Instruct-Q8_0.gguf
```

The deployed local model is the Q8_0 GGUF build:
`LFM2.5-1.2B-Instruct-Q8_0.gguf`.

## Tests

```bash
python -m unittest discover tests -v
```

The suite covers request validation, lane resolution, response shaping,
metrics redaction, local runtime checks, backend command construction, error
classification, and stderr parsing. Network/model integration tests remain
opt-in.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for Oracle paths, JSON permissions,
systemd boot behavior, HTTPS health checks, and troubleshooting.

## License

MIT
