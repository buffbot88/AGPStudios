# Security Notes — AshatOS Dual-Lane Inference Host

## Authentication Architecture

### Two-Layer Authentication

Requests to inference endpoints require two authentication layers:

1. **Hugging Face Authentication** (ZeroGPU Space)
   - Required for accessing private Spaces
   - Uses `Authorization: Bearer <HF_TOKEN>` standard header
   - HF token must have read permission on the Space

2. **Lane-Specific API Key**
   - Uses custom `X-Ashat-Key` header
   - Separate keys for MicroBrain, MainBrain, and Admin
   - Constant-time comparison via `hmac.compare_digest()`

### Why separate headers?

The standard `Authorization` header may be consumed by Hugging Face's own
authentication proxy. Using `X-Ashat-Key` as a custom header avoids conflicts
and allows independent key management per lane.

## Key Management

### Storage

- Keys are stored as Hugging Face **Space Secrets** (encrypted at rest)
- Never stored in source code, configuration files, or README
- Never logged or exposed in error messages
- Never sent to the client browser

### Generation

Generate with Python's `secrets` module (cryptographically secure):

```python
import secrets
print(secrets.token_urlsafe(48))  # 64 characters, ~256 bits of entropy
```

### Rotation

- Keys can be rotated independently per lane
- Changing a key in Space Secrets takes effect immediately (no reboot needed
  if the app reads env vars dynamically — current implementation reads at
  startup; a restart is required)

## Security Boundaries

### What the Space protects

- Model files (GGUF) from unauthorized download
- Inference compute (zeroGPU time) from unauthorized consumption
- Request/response data from exposure on the public dashboard

### What the Space does NOT do

- User authentication (AshatOS handles user auth)
- Rate limiting (in-memory only for now — see `QUEUE_LIMIT`)
- Request/response encryption beyond HTTPS (inherited from Hugging Face)
- Audit logging (metrics are sanitized aggregate data)

## Threat Model

### Mitigated threats

| Threat | Mitigation |
|---|---|
| Unauthorized inference | Lane-specific keys, constant-time comparison |
| Key brute force | 256-bit entropy makes this infeasible |
| GPU quota theft | Auth checked before GPU allocation |
| Model theft | All model access through authenticated endpoints only |
| Prompt leakage | No prompts stored in metrics or logs |
| Response leakage | No responses stored in metrics or logs |

### Not yet mitigated (future work)

| Threat | Planned mitigation |
|---|---|
| Replay attacks | Request signing with nonce/timestamp (future) |
| Timing attacks on auth | Current implementation is constant-time on comparison |
| Queue exhaustion | Implement per-key rate limiting |
| DoS via large payloads | Payload size limits are enforced |

## Logging Policy

### What is logged

- Request IDs (UUIDs, no user data)
- Lane name and model used
- Timing and performance metrics
- Error codes (no stack traces)
- Authentication success/failure (no key material)

### What is NEVER logged

- Full prompts or messages
- Full responses
- API keys or tokens
- Session identifiers
- Hugging Face tokens

## Dashboard Safety

The public dashboard displays:
- Aggregate request counts and success rates
- Rolling averages for tokens/second and latency
- Model metadata (file names, context sizes)
- Recent health events (no user data)

The dashboard does NOT display:
- Individual prompts or responses
- API keys or tokens
- User identifiers
- Private IP addresses or filesystem paths
- Stack traces or error details
