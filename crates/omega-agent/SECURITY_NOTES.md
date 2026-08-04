# Security Notes — AshatOS Neural Host

## Authentication

Inference requests require the custom `X-Ashat-Key` header. The key is
compared with `hmac.compare_digest()` and is checked before request validation,
queueing, or model execution.

The production key is stored only in:

```text
/home/opc/Projects/AshatNueralHost/server-config.json
```

The file is owned by `root:opc`, mode `640`, ignored by Git, and never served
by FastAPI. The repository includes `server-config.example.json` only as a
safe template with a placeholder key. Never commit the production file or a
real key.

## Configuration safety

Runtime settings are loaded from JSON, not from an environment file or
process environment variables. The production file includes model and binary
paths, CPU limits, queue settings, and the BrainStem key. Operators should validate
permissions after creating or replacing it:

```bash
sudo chown root:opc /home/opc/Projects/AshatNueralHost/server-config.json
sudo chmod 640 /home/opc/Projects/AshatNueralHost/server-config.json
```

## Threat model

### Mitigated threats

| Threat | Mitigation |
|---|---|
| Unauthorized inference | BrainStem key and constant-time comparison |
| Prompt leakage | Prompts are not stored in metrics or logs |
| Response leakage | Responses are not stored in metrics or logs |
| Key exposure | Protected JSON file; no key in Git or dashboard |
| Queue exhaustion | One admitted inference on the one-OCPU host |
| Oversized payloads | Request body and message limits |
| Backend failure | Health checks and persistent-process invalidation |

### Not yet mitigated

| Threat | Planned mitigation |
|---|---|
| Replay attacks | Request signing with nonce/timestamp |
| Distributed rate limiting | External or persistent rate limiter |
| Audit logging | Privacy-preserving security audit stream |

## Logging policy

Logged data is limited to request IDs, lane/model metadata, timing aggregates,
health events, and sanitized error codes. The service never logs:

- Full prompts or messages
- Full responses
- BrainStem keys or tokens
- Session identifiers
- Private IP addresses or filesystem paths in public surfaces

## Public dashboard safety

The dashboard displays aggregate request counts, success rates, performance
summaries, model metadata, and sanitized health events. It does not display
individual prompts, responses, user identifiers, BrainStem keys, tokens, or stack
traces.
