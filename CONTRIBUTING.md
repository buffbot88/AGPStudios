# Contributing

Internal project only.

## Active vs retired code

The active product in this repo is the Vite portal (`apps/ashat-hub-web/`) and the Python
API service (`services/python-api/`). Those are the only active runtime pieces.

Live games are Paws & Parcels and Everenfell only. World of Ra, `more-titles`,
Omega, and Galileo references are stubs or history — do not present them as
live servers.

Anything under `archive/` (if present) is historical record only. It is not
built, not authoritative, and not part of the current system. See
`docs/system-architecture.md` for the active boundary.

There is no in-repo AI runtime, model host, or agent gateway in this repo.

## Rules

- Keep changes scoped.
- Do not commit secrets, keys, backups, or runtime data.
- Validate touched code before pushing.
- For API changes, update `docs/python-api-contracts.md`.
- For architectural or scope changes, update `docs/system-architecture.md` and `README.md`.
- Use the repo root as the source of truth for project-wide changes.
