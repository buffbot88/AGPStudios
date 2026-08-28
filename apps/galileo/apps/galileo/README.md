# Galileo: AI-Powered Full-Stack Web Development in the Browser

Galileo is an AI-powered web development agent that allows you to prompt, run, edit, and deploy full-stack applications directly from your browser—no local setup required. It is part of the AGP Studios / Ashat Hub ecosystem and is built on the open-source Bolt codebase.

## Ashat Hub Inference Routing

Galileo does not call any model provider directly. All chat and prompt-enhancement traffic is sent to the **Ashat Hub Alpha gateway** (`crates/alpha-server` in the AshatHub repository), an OpenAI-compatible `/v1/chat/completions` endpoint. The gateway classifies each request and routes it to the **Omega/Beta/Delta** coding agent pool for script generation and iterations, or to the local text/vision workers.

## Configuration

All settings live in **`config.json`** in the project root — no environment variables:

```json
{
  "gateway": {
    "url": "http://127.0.0.1:3000",
    "api_key": ""
  },
  "agents": [
    { "id": "omega", "url": "https://129.213.94.124" },
    { "id": "beta", "url": "https://150.136.208.93:8082" },
    { "id": "delta", "url": "https://129.213.147.225:8088" }
  ]
}
```

- `gateway.url` — base URL of the Alpha gateway; the app appends `/v1` automatically.
- `gateway.api_key` — bearer key if the gateway is fronted by auth (leave empty for loopback on Alpha).
- `agents` — the Omega/Beta/Delta Neural Host endpoints probed by `GET /api/status`; defaults mirror alpha-server's deployed pool.

Missing keys — or no file at all — fall back to the same built-in defaults. Restart Galileo after editing. If you set a real `api_key`, stop tracking the file in git.

Galileo is designed to run on the Alpha host itself: the app server talks to `alpha-server` on loopback, the Ashat orchestrator classifies each request and routes coding generations to the **Omega/Beta/Delta** Neural Host pool. Only off-host deployments need the public proxy — see [`docs/ashat-gateway-apache-proxy.md`](./docs/ashat-gateway-apache-proxy.md).

`GET /api/status` reports gateway reachability, queue capacity, local worker health, and per-agent Omega/Beta/Delta health as JSON (always HTTP 200; degradation is in the body).

## What Makes Galileo Different

- **Full-Stack in the Browser**: Galileo integrates AI models routed through Ashat Hub with an in-browser development environment powered by **StackBlitz's WebContainers**. This allows you to:
  - Install and run npm tools and libraries (like Vite, Next.js, and more)
  - Run Node.js servers
  - Interact with third-party APIs
  - Deploy to production from chat
  - Share your work via a URL

- **AI with Environment Control**: Galileo gives the AI complete control over the environment including the filesystem, node server, package manager, terminal, and browser console—empowering the agent to handle the entire app lifecycle from creation to deployment.

## Tips and Tricks

- **Be specific about your stack**: Mention frameworks or libraries (like Astro, Tailwind, ShadCN) in your initial prompt so Galileo scaffolds the project accordingly.

- **Use the enhance prompt icon**: Click the 'enhance' icon to have the model refine your prompt, then edit the results before submitting.

- **Scaffold the basics first**: Get the project structure in place before adding advanced functionality.

- **Batch simple instructions**: Combine simple changes into one message to save time and tokens.

## Development

```bash
pnpm install
pnpm run dev
```

| Script | Purpose |
| --- | --- |
| `pnpm run dev` | Start the Remix Vite dev server |
| `pnpm run build` | Production build |
| `pnpm run start` | Serve the production build with `remix-serve` |
| `pnpm run test` | Run Vitest suites |
| `pnpm run typecheck` | TypeScript project check |

Local runs need no extra setup — `config.json` targets alpha-server on loopback by default; edit it to point elsewhere and restart.

## Hosting on Alpha

Galileo runs as a Node Remix server behind Apache, following the same pattern as the ashat-hub gateway (`:3100`). The default port is **3200** so it never collides with alpha-server (`:3000`) or the hub (`:3100`):

```bash
pnpm run build
PORT=3200 pnpm run start
```

For a persistent service, a minimal systemd unit:

```ini
[Unit]
Description=Galileo (Ashat Hub dev studio)
After=network.target

[Service]
WorkingDirectory=/var/oled/data/AshatHub/galileo
Environment=PORT=3200
ExecStart=/usr/bin/pnpm run start
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

The unit's `WorkingDirectory` matters: `config.json` is read from the working directory at startup.

Then proxy it from Apache next to the existing `/api`, `/health`, and `/ready` blocks — for example on a subdomain or path prefix:

```apache
ProxyPass        /galileo/ http://127.0.0.1:3200/
ProxyPassReverse /galileo/ http://127.0.0.1:3200/
```

WebContainers run client-side in the browser, so no browser-side infrastructure changes are needed for self-hosting.
