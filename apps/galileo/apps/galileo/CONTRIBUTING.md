# Contributing to Galileo

Welcome to the **Galileo** codebase — an AI-powered, browser-based development studio by AGP Studios. Galileo is built on the open-source Bolt codebase and runs inference through the **Ashat Hub** platform.

## Why Build with Galileo + WebContainer API

Galileo combines AI with sandboxed development environments to create a collaborative experience where code is developed by the assistant and the programmer together. Galileo uses [WebContainer API](https://webcontainers.io/api) to run generated code in the browser — giving the AI direct access to a **Node.js server**, **filesystem**, **package manager**, and **dev terminal** inside the user's browser tab, without remote environments or local installs.

The [WebContainer API](https://webcontainers.io) is free for personal and open source usage. For commercial usage, see [StackBlitz's WebContainer API pricing](https://stackblitz.com/pricing#webcontainer-api).

## Tech Stack

Galileo is built with [Remix](https://remix.run/) and UnoCSS, and runs on Cloudflare Pages.

Galileo uses the [AI SDK](https://sdk.vercel.ai/) to integrate with AI models. All inference is routed through the **Ashat Hub Alpha gateway** (`crates/alpha-server` in the AshatHub repository) — an OpenAI-compatible `/v1/chat/completions` endpoint that routes text requests to the **Liquid** 1.2B worker and image requests to the on-demand **450M VL** worker. No model provider is called directly.

The gateway location is configured with environment variables (see the [README](./README.md) table):

- `ASHAT_GATEWAY_URL` — base URL of the Alpha gateway
- `ASHAT_API_KEY` — bearer key, if the gateway is fronted by auth

## Get Started

```bash
pnpm install
pnpm run dev
```

Before opening a pull request, run the project checks:

```bash
pnpm run typecheck
pnpm run test
pnpm run build
```

## Project Practices

Read [VOWS.md](./VOWS.md) before contributing — it defines the development practices for this repo (gather context before building, show a plan before large changes, no scaffolds or mock filler, and prefer relying on locally hosted Ashat AI for ecosystem tasks).
