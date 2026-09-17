# Retired components

This doc records subsystems that were removed from the active AGP Studios repo.

## Removed from the active workspace

- Rust Alpha gateway, core, common types, and server crate
- Omega inference supervisor crate
- Galileo app shell
- Rust workspace root and lockfile

Those crates and the Galileo app directory no longer exist in this repo. They
were part of earlier generations of the system and were removed because they are
no longer part of the current product scope.

## Archive

Historical material from earlier Rust implementations is kept under:

- `archive/retired-rust/`

That material is **archived historical record only**. It is not built, run, or
authoritative for the current repo. See `archive/README.md`.

## Current product

The current repo is a Vite + React portal and a Python API service. The active
boundary is:

```text
Browser -> Python API (/api/*)
```

See `docs/system-architecture.md`.
