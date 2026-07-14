# API Contract — AshatOS BrainStem Inference Host

## Overview

This document defines the API contract for the Hugging Face Space that hosts
a single private GGUF inference lane (BrainStem).

---

## Endpoints

### 1. BrainStem Inference

**Gradio API:** `POST /gradio_api/call/brainstem`  
**HTTP API:** `POST /v1/chat/completions` (with `model` field containing "brainstem" or "1.2b")

**Authentication:** Required (X-Ashat-Key header matching `ASHAT_BRAINSTEM_KEY`)

**Request:**
```json
{
  "request_id": "uuid-optional",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the Moon?"}
  ],
  "max_tokens": 64,
  "temperature": 0.7,
  "top_p": 0.9
}
```

**Response (success):**
```json
{
  "id": "ashat-uuid",
  "object": "chat.completion",
  "created": 1783650000,
  "model": "LFM2.5-1.2B-Instruct-Q8_0.gguf",
  "lane": "brainstem",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "The Moon is Earth's only natural satellite..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 18,
    "completion_tokens": 96,
    "total_tokens": 114
  },
  "performance": {
    "cold_start": true,
    "server_start_ms": 1600,
    "model_load_ms": 1200,
    "total_latency_ms": 3100,
    "time_to_first_token_ms": null,
    "prompt_tokens_per_second": 420.5,
    "generation_tokens_per_second": 52.1,
    "backend": "cuda",
    "gpu_offload_verified": true
  },
  "request_id": "original-request-id",
  "ok": true
}
```

---

### 2. List Models

**HTTP API:** `GET /v1/models`

**Authentication:** None

**Response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "LFM2.5-1.2B-Instruct-Q8_0.gguf",
      "object": "model",
      "created": 1783650000,
      "owned_by": "ashatos"
    }
  ]
}
```

---

### 3. Health Check

**HTTP API:** `GET /health`

**Authentication:** None

**Response:**
```json
{
  "status": "ok",
  "uptime_seconds": 300.5,
  "brainstem_ready": true,
  "llama_server_available": true
}
```

---

### 4. Public Status

**Gradio API:** `POST /gradio_api/call/public_status`  
**HTTP API:** `GET /api/public_status`

**Authentication:** None

Returns lane status, request counts, performance summaries.

---

### 5. Public Metrics

**Gradio API:** `POST /gradio_api/call/public_metrics`  
**HTTP API:** `GET /api/public_metrics`

**Authentication:** None

Returns sanitized aggregate metrics.

---

### 6. Admin Benchmark

**Gradio API:** `POST /gradio_api/call/admin_benchmark`

**Authentication:** Required (X-Ashat-Key header matching `ASHAT_ADMIN_KEY`)

Runs a predefined benchmark on the BrainStem lane.

**Input:** `{ "lane": "brainstem" }`

---

## Authentication

Use the `X-Ashat-Key` HTTP header:

```
X-Ashat-Key: <lane-specific-key>
```

Keys are stored as Hugging Face Space Secrets:

| Secret | Lane |
|---|---|
| `ASHAT_BRAINSTEM_KEY` | BrainStem |
| `ASHAT_ADMIN_KEY` | Admin/benchmark |

Key comparison uses `hmac.compare_digest()` (constant-time).

---

## Error Codes

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
