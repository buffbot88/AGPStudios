# Security remediation ledger

This ledger intentionally stores no credential values.

> Scope (2026-10-26): the only in-scope runtime is AshatHub
> (`apps/ashat-hub-web` + `services/python-api`) and its two live games (Paws
> & Parcels, Everenfell). Alpha / Omega / Beta / Delta / Galileo rows below are
> external or historical systems, not code in this checkout.

| Credential category | System/locations | Owner | Rotation | Old revoked | Application validated |
|---|---|---|---|---|---|
| OIDC signing identity | AshatHub runtime | AshatHub operator | BLOCKED — operator action required | BLOCKED | PASS — runtime path required |
| Gateway/shared authentication key | Alpha/Omega/Beta/Delta runtime | Platform operator | BLOCKED — owner action required | BLOCKED | PARTIAL |
| Public/API authentication key | Alpha/Galileo runtime | Platform operator | BLOCKED — owner action required | BLOCKED | PARTIAL |
| Provider credentials | AshatHub credential store | Provider/account owners | BLOCKED — provider action required | BLOCKED | BLOCKED |
| SSH/deployment key | Peer deployment control plane | Infrastructure owner | BLOCKED — owner action required | BLOCKED | PASS — peer deployment validated |

## Current controls

- No credential values belong in tracked source, documentation, browser storage, logs, or API responses.
- OIDC private material resolves through `ASHAT_OIDC_PRIVATE_KEY_FILE` and must point outside repositories.
- Galileo gateway credentials resolve from runtime environment variables.
- Alpha/Omega runtime configuration is deployment-managed and ignored by Git.
- Git-history rewriting is intentionally pending credential rotation and revocation.

## Closure requirements

For each blocked row, record rotation, revocation, and application validation with the authoritative owner before rewriting history or declaring security complete.
