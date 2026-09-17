# AGPStudios monorepo migration

Phase 0 preparation manifest. This file records planning state only; it does not rewrite Git history or change remotes.

## Baselines

| Repository | Branch | Baseline |
|---|---|---|
| Galileo | main | `3c7909c96bd91954cfa57d9edc79592fa6da71a3` |
| AshatHub | main | `54142b1d8809436698dc9b345c8d2de9c9215610` |
| AshatCodingAgent | main | `f0cc3f5731abebcece2b06999800ac5530a78ed2` |

All three working trees were clean during Phase 0 inventory.

## Planned tree

- `apps/galileo/`
- `crates/ashat-hub/`
- `crates/alpha-common/`
- `crates/alpha-core/`
- `crates/alpha-server/`
- `crates/omega-agent/`
- `scripts/`
- `docs/`

## Never import

- `node_modules/`
- Rust `target/`
- model files and model caches
- logs and workspaces
- runtime storage and databases
- `.env*` files
- private keys, SSH keys, PEM files, and provider credentials
- generated `build/` output unless explicitly required by deployment

## Migration rules

- Preserve source history with `git subtree` where useful.
- Do not rewrite existing remote history in Phase 0.
- Do not delete the three source repositories until monorepo validation is complete.
- Replace Beta/Delta-specific code during the Omega migration; do not import it into the active tree.
- Keep secrets runtime-managed and out of Git, logs, API responses, and browser state.

## Phase 0 findings

- No sensitive-looking files were tracked by the three repositories.
- Runtime-only model, target, log, workspace, node_modules, and storage directories are ignored.
- The repositories have overlapping top-level documentation and contribution files; these require deliberate merge decisions.
- Galileo and AshatHub use JavaScript/Rust application roots; AshatCodingAgent is an independent Rust workspace.
- Existing remotes remain unchanged.

## Pending before Phase 1

- Create the empty `buffbot88/AGPStudios` remote.
- Choose the canonical root `README.md`, `LICENSE`, `VOWS.md`, and contribution policy.
- Define the Omega deployment artifact boundary.
- Confirm the Omega host SSH target through runtime configuration only.
