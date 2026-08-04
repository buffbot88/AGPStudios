# Deployment Guide — Ashat Neural Network

This deployment runs the Ashat Neural Network's authenticated BrainStem lane on Oracle Linux ARM64.
Nginx terminates HTTPS and proxies to FastAPI on localhost; FastAPI keeps one
CPU-only `llama-server` process alive for reuse across requests.

## Runtime layout

| Component | Location |
|---|---|
| Application | `/home/opc/Projects/AshatNueralHost` |
| Python environment | `/home/opc/Projects/AshatNueralHost/.venv` |
| Native llama-server | `/usr/local/libexec/ashat-neural-host/llama-server` |
| GGUF model | `/var/lib/ashat-neural-host/LFM2.5-1.2B-Instruct-Q8_0.gguf` |
| Protected JSON configuration | `/home/opc/Projects/AshatNueralHost/server-config.json` |
| Checked-in template | `server-config.example.json` |
| FastAPI | `127.0.0.1:8000` |
| llama-server | `127.0.0.1:18080` |
| Public URL | `https://ashatneuralhost.agpstudios.org/` |

## Configuration

The application reads settings only from `server-config.json`; it does not
require an environment file or environment-variable configuration. The real
production file is outside Git and must be protected:

```bash
sudo chown root:opc /home/opc/Projects/AshatNueralHost/server-config.json
sudo chmod 640 /home/opc/Projects/AshatNueralHost/server-config.json
sudo chmod 750 /home/opc/Projects/AshatNueralHost
```

The repository includes `server-config.example.json` with a placeholder key
for local testing. Copy it to `server-config.json` and replace the placeholder
with a locally generated test key. Never commit the production file or a real
BrainStem key.

The production JSON contains the BrainStem key, model path, native binary
path, CPU limits, queue limit, logging level, and model limits.
The BrainStem key is never logged or displayed by the dashboard.

## Services and OS startup

The service is enabled in `multi-user.target`, waits for
`network-online.target`, and uses `Restart=always`. Therefore FastAPI and its
persistent llama-server process start again automatically after an OS reboot.

```bash
sudo systemctl status ashat-neural-host.service
sudo systemctl status nginx
sudo systemctl status certbot-renew.timer
sudo journalctl -u ashat-neural-host.service -f
```

When typing the commands, the service name is exactly
`ashat-neural-host.service` (without spaces). The spaced form above is only a
visual workaround for clients that rewrite the unit name; use the exact name
in a shell command:

```bash
sudo systemctl status ashat-neural-host.service
```

## Health checks

```bash
curl https://ashatneuralhost.agpstudios.org/health
curl http://127.0.0.1:18080/health
```

The application health response should report `brainstem_ready: true` and
`llama_server_available: true`.

## API usage

The completion endpoint requires the `X-Ashat-Key` header:

```bash
curl https://ashatneuralhost.agpstudios.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Ashat-Key: YOUR_BRAINSTEM_KEY" \
  -d '{
    "model": "brainstem",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 64,
    "temperature": 0.7
  }'
```

The response is OpenAI-compatible and contains
`choices[0].message.content`, usage counts, and sanitized performance data.
Streaming is intentionally not enabled yet.

## Model

The deployed local model is:

```text
LFM2.5-1.2B-Instruct-Q8_0.gguf
```

The deployed local model is `LFM2.5-1.2B-Instruct-Q8_0.gguf`. Verify the
model license and usage terms before commercial redistribution.

## Troubleshooting

```bash
sudo systemctl restart ashat-neural-host.service
sudo nginx -t && sudo systemctl reload nginx
sudo journalctl -u ashat-neural-host.service -n 100 --no-pager
free -h
ps -ef | grep -E '[u]vicorn|[l]lama-server'
```

If the model process is unhealthy, the Python backend invalidates its cached
process after a failed completion and starts a replacement on the next request.
The host is deliberately limited to one inference at a time because it has one
OCPU and 4 GB RAM.
