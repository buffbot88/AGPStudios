# API Contract — AshatOS Dual-Lane Inference Host

## Overview

This document defines the API contract for the Hugging Face Space that hosts
two private GGUF inference lanes (MicroBrain / MainBrain).

---

## Endpoints

### 1. MicroBrain Inference

**Gradio API:** `POST /gradio_api/call/microbrain`  
**HTTP API:** `POST /v1/chat/completions` (with `model` field containing "micro" or "350m")

**Authentication:** Required (X-Ashat-Key header matching `ASHAT_MICROBRAIN_KEY`)

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
  "model": "LFM2.5-350M-Q6_K.gguf",
  "lane": "microbrain",
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

### 2. MainBrain Inference

**Gradio API:** `POST /gradio_api/call/mainbrain`  
**HTTP API:** `POST /v1/chat/completions` (with `model` field containing "main" or "1.2b")

**Authentication:** Required (X-Ashat-Key header matching `ASHAT_MAINBRAIN_KEY`)

**Request:** Same format as MicroBrain.

**Response:** Same structure, with larger context and higher latency.

---

### 3. List Models

**HTTP API:** `GET /v1/models`

**Authentication:** None

**Response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "LFM2.5-1.2B-Instruct-Q6_K.gguf",
      "object": "model",
      "created": 1783650000,
      "owned_by": "ashatos"
    },
    {
      "id": "LFM2.5-350M-Q6_K.gguf",
      "object": "model",
      "created": 1783650000,
      "owned_by": "ashatos"
    }
  ]
}
```

---

### 4. Health Check

**HTTP API:** `GET /health`

**Authentication:** None

**Response:**
```json
{
  "status": "ok",
  "uptime_seconds": 300.5,
  "microbrain_ready": true,
  "mainbrain_ready": true,
  "llama_server_available": true
}
```

---

### 5. Public Status

**Gradio API:** `POST /gradio_api/call/public_status`  
**HTTP API:** `GET /api/public_status`

**Authentication:** None

Returns lane status, request counts, performance summaries.

---

### 6. Public Metrics

**Gradio API:** `POST /gradio_api/call/public_metrics`  
**HTTP API:** `GET /api/public_metrics`

**Authentication:** None

Returns sanitized aggregate metrics.

---

### 7. Admin Benchmark

**Gradio API:** `POST /gradio_api/call/admin_benchmark`

**Authentication:** Required (X-Ashat-Key header matching `ASHAT_ADMIN_KEY`)

Runs a predefined benchmark on the specified lane.

**Input:** `{ "lane": "microbrain" | "mainbrain" | "both" }`

---

## Authentication

Use the `X-Ashat-Key` HTTP header:

```
X-Ashat-Key: <lane-specific-key>
```

Keys are stored as Hugging Face Space Secrets:

| Secret | Lane |
|---|---|
| `ASHAT_MICROBRAIN_KEY` | MicroBrain |
| `ASHAT_MAINBRAIN_KEY` | MainBrain |
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
