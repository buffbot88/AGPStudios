# Contributing — Ashat Master Coding Agent (Omega)

Omega is the always-on master of the Ashat coding agent ecosystem: a
universal-source Rust + Axum LLM server (350M text intent router + spawn-on-demand
1.2B Coding Agent pool), a weighted row
chain to the Beta / Delta slave hosts, and verified GitHub sync.

## Docs map

| File | What it is |
| ---- | ---------- |
| `README.md` | Host setup, public surface, config keys, validation commands |
| `ROADMAP.md` | High-level product phases and their status |
| `BUILDPLAN.md` | Implementation order, locked decisions, risks, validation path |
| `Beta_Delta.md` | Peer (Beta/Delta) access, seeding, routing semantics, GitHub sync |
| `VOWS.md` | **Rules of engagement — protected. Read it first; do not modify it.** |

## Rules of engagement

`VOWS.md` is binding for every change in this repo. The parts that matter most
day-to-day:

- **Plan before you build.** Gather all context (read every file the change
  touches), write a plan in the standard shape — goal → files to touch with why
  → change list → risks → validation — and get approval before editing
  (Vows 5–7).
- **No shortcuts, no scaffolds, no AI slop.** Build the real thing or don't
  build it (Vows 2 & 4).
- **Audit for staleness.** Docs, files, and code that no longer match reality
  get updated or removed in the same change (Vow 10).
- **Don't write outside the project tree** without explicit permission (Vow 9).
- Docstrings stay at one or two sentences (Vow 8).

## Development workflow

1. Sync first: `./scripts/github_sync.sh status` (or
   `./scripts/github_sync.sh pull` for a verified ff-only update).
2. Make the change on `main` (single-branch flow); keep commits small and
   atomic.
3. Validate — server: `cargo fmt --all --check`,
   `cargo clippy --all-targets -- -D warnings`, `cargo test`,
   npm run build`.
4. Update the affected docs in the same change: `README.md` for surface /
   config changes, `ROADMAP.md` for phase status, `BUILDPLAN.md` for locked
   decisions / risks, `Beta_Delta.md` for peer / routing changes.
5. Push with `./scripts/github_sync.sh push` (uses the `~/.ssh/ashat_github`
   deploy key), or via `POST /api/admin/github_sync` with `{"mode": "push"}`.

## Git & secrets

- The master is a git repo (branch `main`, origin `buffbot88/ashatnueralhost`).
- **Never commit secrets.** `server-config.json` (live `ASHAT_KEY` + slave
  `api_key`s), `oraclehost_id_rsa`, `*.key` / `*.pem`, `models/`, `target/`,
  `logs/`, and `workspaces/` are gitignored and guard-checked by
  `github_sync.sh` before every stage. Commit config changes to
  `server-config.example.json` with placeholders only.
- The legacy `ASHAT_KEY` from the pre-tooling history is still live — see the
  warning in `Beta_Delta.md`; rotating it touches all three hosts.
- Commit messages follow the repo's existing style: a short subject line, a
  `docs:` prefix for doc-only changes, and descriptive bodies
  ("Add X", "Fix Y", "vN.M ...").

## First-time setup

```bash
cp server-config.example.json server-config.json   # then set ASHAT_KEY (+ ASHAT_ADMIN_KEY for admin routes)
cargo build --release
./target/release/ashat-master-coding-agent
```

Models live in `models/` (auto-discovered) and `bin/llama-server` is bundled.

## Where things live

- `crates/omega-common` — shared types, config + env overlay, GGUF discovery,
  metrics, workspace dirs
- `crates/omega-core` — demand pools, queues, intent classification, weighted
  row-chain router, tool loop, supervision
- `crates/omega-server` — axum handlers, auth, alpha status reporter (binary
  `ashat-master-coding-agent`)
- `scripts/` — `seed_slave.sh` (peer deploy), `github_sync.sh` (GitHub sync)
- `bin/` — bundled `llama-server`

## Common tasks

- **Add an endpoint / change the API** — edit
  `crates/omega-server/src/handlers.rs` and `main.rs`, then update the
  public-surface table in `README.md`.
- **Change routing / add a backend** — `crates/omega-core/src/router.rs` +
  the `row_chain` config; document in `Beta_Delta.md`.
- **Change a phase status** — update the build-phases table in `ROADMAP.md`.
