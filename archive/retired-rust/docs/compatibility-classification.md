# Compatibility classification

| Area | Classification | Reason |
|---|---|---|
| `compat.rs` | MIGRATION ONLY | Preserves legacy AshatHub responses during client migration. |
| Legacy auth/session paths | MIGRATION ONLY | Existing accounts and sessions still require compatibility handling. |
| `import_legacy.rs` | KEEP | Explicit one-shot migration utility. |
| `/api/jobs` | KEEP | Durable jobs/checkpoints and execution infrastructure, not a conversational mode. |
| Alpha runtime config | KEEP, deployment-managed | Contains local runtime settings and is excluded from Git. |
| Omega JSON action protocol | REMOVED | Native structured tool calls are authoritative. |
| Omega web/skill tools | REMOVED | Owned by Alpha/Galileo. |
| Retired model-router path | REMOVED | Execution peers are 1.2B-only. |

Compatibility code must not create model requests, semantic routing, or an alternate agent controller.
