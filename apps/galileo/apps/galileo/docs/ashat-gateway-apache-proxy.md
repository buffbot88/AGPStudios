# Alpha Gateway Apache Proxy Runbook

> **Off-host deployments only.** Galileo's primary deployment runs on the Alpha host and reaches `alpha-server` directly on loopback (`ASHAT_GATEWAY_URL` defaults to `http://127.0.0.1:3000`). This runbook is only needed if a Galileo instance runs off-host (for example on Cloudflare Pages) and must reach the gateway through the public domain.

Galileo calls the Ashat Hub Alpha gateway at `https://agpstudios.org/v1/chat/completions`. The Alpha host's Apache currently proxies `/api`, `/health`, `/ready`, and `/host` to the ashat-hub Rust gateway on `:3100`, but `/v1/*` falls through to the Vite SPA. This runbook exposes `alpha-server` (`crates/alpha-server`, port `3000`) under the existing domain.

## 1. (Recommended) Bind alpha-server to loopback

In `crates/alpha-server/config.toml`, keep the gateway off the public interface so only Apache can reach it:

```toml
[server]
host = "127.0.0.1"
port = 3000
```

Restart alpha-server after the change.

## 2. Add the Apache proxy rules

In the `agpstudios.org` vhost, next to the existing `/api` proxy block, add:

```apache
# Ashat Alpha inference gateway (Omega/Beta/Delta routing)
ProxyPreserveHost Off
ProxyPass        /v1/ http://127.0.0.1:3000/v1/ timeout=600
ProxyPassReverse /v1/ http://127.0.0.1:3000/v1/
```

Notes:

- `timeout=600` keeps long streaming generations from being cut off mid-stream.
- `ProxyPreserveHost Off` is correct here: alpha-server does not do virtual-host routing, and it avoids Host-header mismatches.
- Streaming (SSE) works through `mod_proxy_http`; ensure `SetEnv proxy-sendchunked 1` if chunks appear buffered.

Then:

```bash
sudo apachectl configtest
sudo systemctl reload httpd
```

## 3. Verify

From outside the host:

```bash
# Should return {"object":"list","data":[{"id":"ashat",...}]}
curl -s https://agpstudios.org/v1/models

# Should return {"status":"ok","version":"..."}
curl -s https://agpstudios.org/v1/../health
# or directly:
curl -s https://agpstudios.org/health   # existing ashat-hub liveness, unchanged

# Round-trip inference (local text worker, no tokens of consequence)
curl -s -X POST https://agpstudios.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"hi"}],"max_tokens":8,"stream":false}'
```

The `/v1/models` response must be JSON, not the AGP Studios SPA HTML. If you still get HTML, the vhost edit did not land on the active site config (`apachectl -S` to check which file is live).

## 4. Galileo side

No code change is required: set `gateway.url` to `https://agpstudios.org` in `config.json` and the app appends `/v1`. On Alpha itself the loopback default (`http://127.0.0.1:3000`) applies and no proxy is needed — this runbook is only for off-host deployments.

Optional hardening later: front `/v1` with the same `X-Ashat-Key` auth alpha-server already uses for its Omega/Beta/Delta agents, then set `gateway.api_key` in `config.json` to match.
