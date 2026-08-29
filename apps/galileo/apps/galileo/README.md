# Galileo: AI-Powered Full-Stack Web Development in the Browser

Galileo is an AI-powered web development agent that allows you to prompt, run, edit, and deploy full-stack applications directly from your browser—no local setup required. It is part of the AGP Studios / Ashat Hub ecosystem and is built on the open-source Bolt codebase.

## Ashat Hub communication

Galileo does not call any model provider or coding agent directly. All AI operations go to the **Ashat Hub Alpha gateway** (`crates/alpha-server` in the AshatHub repository), an OpenAI-compatible `/v1/chat/completions` endpoint. Alpha owns mode routing, local worker selection, agent dispatch, retries, capacity, and agent health.

The target operation paths are:

```text
Chat / Plan → Alpha → local 350M
Vision      → Alpha → local 450M VL
Build       → Alpha → Omega/Beta/Delta job
Debug       → Alpha → Omega/Beta/Delta validation job
Deploy      → Galileo deployment boundary → AshatHub snapshot
```

Galileo knows operation state and structured results, not individual model instances, agent selection, retry policy, or agent telemetry internals. Per-agent telemetry is owned by AshatHub.

## Configuration

Runtime gateway settings live in **`config.json`** in the project root:

```json
{
  "gateway": {
    "url": "http://127.0.0.1:3000",
    "api_key": ""
  }
}
```

- `gateway.url` — base URL of the Alpha gateway; the app appends `/v1` automatically.
- `gateway.api_key` — gateway credential when Alpha is not reached through its protected local boundary.

Galileo must not contain Omega/Beta/Delta endpoint configuration. Missing keys—or no file—fall back to the local Alpha gateway. Restart Galileo after editing. Do not commit real credentials.

Galileo is designed to run on the Alpha host itself. Only off-host deployments need the public proxy—see [`docs/ashat-gateway-apache-proxy.md`](./docs/ashat-gateway-apache-proxy.md).

`GET /api/status` reports Alpha reachability, queue capacity, local worker health, and Alpha-managed coding capacity. AshatHub remains the source for per-agent telemetry.

## What Makes Galileo Different

- **Full-Stack in the Browser**: Galileo integrates AI models routed through Ashat Hub with an in-browser development environment powered by **StackBlitz's WebContainers**. This allows you to:
  - Install and run npm tools and libraries (like Vite, Next.js, and more)
  - Run Node.js servers
  - Interact with third-party APIs
  - Deploy to production from chat
  - Share your work via a URL

- **AI with Environment Control**: Galileo provides the live WebContainer workspace, filesystem, node server, package manager, terminal, and browser console. Alpha and its coding-agent pool provide software-engineering operations; explicit deployment creates the durable AshatHub snapshot.

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
