# API Contract — Ashat Neural Network BrainStem

## Overview

This document defines the API contract for the Oracle Linux ARM64 service that
hosts a single private GGUF inference lane (BrainStem).

## Endpoints

### BrainStem inference

**HTTP API:** `POST /v1/chat/completions`  
**Authentication:** Required via `X-Ashat-Key`, matching the BrainStem key in the
protected production `server-config.json`.

**Request:**

```json
{
  "request_id": "uuid-optional",
  "model": "brainstem",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the Moon?"}
  ],
  "max_tokens": 64,
  "temperature": 0.7,
  "top_p": 0.9
}
```

The response is OpenAI-compatible and includes sanitized usage and performance
aggregates. Prompts, responses, keys, and filesystem paths are not stored in
public metrics.

### List models

**HTTP API:** `GET /v1/models`  
**Authentication:** None

### Health check

**HTTP API:** `GET /health`  
**Authentication:** None

The response reports service status, BrainStem readiness, and local
llama-server availability without exposing secrets.

### Public telemetry

- `GET /api/public_status`
- `GET /api/public_metrics`
- `GET /api/dashboard_html`
- `GET /api/dashboard_timeseries`

All are unauthenticated and sanitized for public display.

## Authentication

Use the custom header:

```text
X-Ashat-Key: <production-key>
```

The key is generated/stored in the protected production file:

```text
/home/opc/Projects/AshatNueralHost/server-config.json
```

That file is ignored by Git and protected as `root:opc` mode `640`. The
repository template `server-config.example.json` contains only a placeholder
for local testing. Never commit a real key or place it in a public dashboard.
Key comparison uses `hmac.compare_digest()`.

## Error codes

| HTTP Code | Error Code | Description |
|---|---|---|
| 400 | `invalid_request_error` | Invalid request body or parameters |
| 401 | `authentication_error` | Missing or invalid authentication key |
| 500 | `internal_error` | Internal server error |
| 503 | `server_start_failed` | llama-server failed to start |
| 503 | `inference_timeout` | Inference timed out |

All errors return:

```json
{
  "error": {
    "message": "Human-readable description",
    "type": "error_code"
  }
}
```
