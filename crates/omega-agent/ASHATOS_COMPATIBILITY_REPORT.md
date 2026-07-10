# AshatOS Compatibility Audit Report

> **Date:** 2026-07-10
> **Audited Repository:** `GitHub/AshatOS/`
> **Audited Files:** `Framework/config.py`, `Framework/bridge.py`, `Framework/external_inference.py`, `Framework/engine.py`, `Framework/microbrain.py`, `server_config.json`, `Modules/Discord/brain_router.py`

---

## 1. How AshatOS currently sends MicroBrain requests

MicroBrain requests are sent via `Framework/microbrain.py` → `MicroBrainService._call_microbrain()`:

- **API mode:** Uses `ExternalInferenceClient.generate()` with the configured `api_config` dict
- **Local mode:** `httpx.post()` to `http://{host}:{port}/v1/chat/completions`
- The payload is the standard OpenAI chat completions format

The Discord module (`Modules/Discord/brain_router.py`) also sends MicroBrain requests directly via `httpx.AsyncClient` to its standalone lane endpoint with OpenAI-compatible payloads.

---

## 2. How AshatOS currently sends MainBrain requests

MainBrain requests go through `Framework/engine.py` → `AIEngine.generate()`:

- Delegates to `ExternalInferenceClient.generate()` with the server's `api_config`
- The payload is the standard OpenAI chat completions format
- Supports both sync and async generation

---

## 3. Does either lane expect an OpenAI-compatible API?

**Yes — both lanes expect OpenAI-compatible APIs.**

Both `ExternalInferenceClient._call_openai_compatible()` and `MicroBrainService._call_microbrain()` use:

```python
messages=[{"role": "system", ...}, {"role": "user", ...}]
```

And parse responses via:

```python
choices[0]["message"]["content"]
```

---

## 4. Request format (messages vs prompt vs custom JSON)

Both lanes use the OpenAI `messages` format:

```json
{
  "model": "LFM2.5 1.2B Instruct",
  "messages": [
    {"role": "system", "content": "System instructions"},
    {"role": "user", "content": "User message"}
  ],
  "max_tokens": 4096,
  "temperature": 0.7
}
```

No raw `prompt` field is used. No custom JSON format is used.

Streaming is supported (`stream: true` → yields SSE deltas), but the current bridge/host path is primarily non-streaming.

---

## 5. Response fields AshatOS expects

Parsed via `ExternalInferenceClient._call_openai_compatible()`:

```python
choices = body.get("choices", [])
text = choices[0].get("message", {}).get("content", "")
```

Expected response structure:

```json
{
  "choices": [
    {
      "message": {
        "content": "Generated response text"
      }
    }
  ]
}
```

The `Model` and `usage` fields are consumed by `AIEngine._generate_external()` for diagnostics/metadata but are not strictly required.

---

## 6. Expected response properties

- **Primary:** `choices[0].message.content` — the generated text
- **Secondary:** `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens` — used for metadata
- **Tertiary:** `model` — used for logging and diagnostics
- **Streaming:** `choices[0].delta.content` — used when streaming is enabled

AshatOS does NOT expect custom `response` or `output` properties from the API.

---

## 7. How the current MainBrain Tunnel is configured

In `server_config.json`, the `LlmApi.MainBrain` block:

```json
{
  "Provider": "openai",
  "Endpoint": "http://127.0.0.1:18080/v1/chat/completions",
  "ApiKey": "",
  "Model": "LFM2.5 1.2B Instruct",
  "MaxTokens": 4096,
  "Temperature": 0.7
}
```

When the provider is `"internal"`, the bridge tunnel system routes requests through a WebSocket or HTTP polling tunnel. For our hosted Space using `"openai"` provider with a direct endpoint URL, no tunnel logic is needed.

---

## 8. Where API URLs and keys are stored

- **Primary:** `server_config.json` → `LlmApi.MainBrain` / `LlmApi.MicroBrain` blocks
- **Runtime:** `Framework/config.py` → `LlmApiConfig` dataclass → `to_api_config_dict()`
- **Account overrides:** `Account/store.py` → `get_user_api_config()` per-user overrides
- **Environment:** `EnvOverrides` in `server_config.json` maps into `os.environ`

---

## 9. Timeout and retry logic

- **`ExternalInferenceClient`:** `request_timeout=120.0` (default), single attempt
- **`BridgeConnector._proxy_to_llama()`:** `timeout=180`
- **`send_through_tunnel()`:** `timeout=180.0`
- No automatic retry on timeout — errors propagate to the caller
- **`AIEngine.generate_async()`:** runs in `ThreadPoolExecutor` with no timeout enforcement

---

## 10. Separate URLs and keys for two lanes

**Yes — each lane has independent configuration.**

```python
config.llm_api.mainbrain  # endpoint, api_key, model, etc.
config.llm_api.microbrain # separate endpoint, api_key, model, etc.
```

The bridge connector connects each lane independently and maintains separate `BridgeConnectionStatus` objects.

---

## 11. Local MicroBrain as mandatory fallback

**Yes.** The local MicroBrain (350M running on port 18081) is a mandatory final fallback.

The fallback hierarchy:

```
Requested inference
    ↓
MainBrain available?
    ├── yes → MainBrain
    └── no
         ↓
Hosted MicroBrain available?
    ├── yes → hosted MicroBrain
    └── no
         ↓
Local mandatory MicroBrain (port 18081)
```

Defined in `Framework/self_healing/capability_profile.py` as `MICROBRAIN_SURVIVAL` mode.

---

## 12. Is streaming expected from the remote host?

**Not required but supported.**

`ExternalInferenceClient.generate_stream()` exists and uses SSE (server-sent events). The primary use case for the hosted lanes is non-streaming. The streaming path is used by the Discord bot and chat interface but the bridge tunnel always uses non-streaming.

**Recommendation:** Implement non-streaming first. Add streaming support later if needed.

---

## 13. Are model names validated or ignored?

**Model names are informational, not strictly validated.**

The `model` field in the endpoint config is used for:
- Logging and diagnostics
- Display in the IDE dashboard
- Bridge health reports

The `ExternalInferenceClient` sends the model name in the request payload but does not validate the response model. The actual model used is determined by which endpoint receives the request.

---

## 14. Does AshatOS send system prompts or construct them locally?

**System prompts are constructed locally by AshatOS.**

The chat pipeline (`Framework/chat.py` and `Modules/Assistant/chat_interface.py`) builds system prompts from:
- Persona configuration (`Framework/persona.py`)
- Pillar/context guidance
- Companion module sentiment/hints
- Conversation history

The system prompt is included in the `messages` array sent to the inference endpoint. The host does **not** need to add or manage system prompts.

---

## 15. Is conversation history trimmed or summarized?

**Yes — handled by AshatOS locally.**

The chat interface manages conversation history with:
- `max_session_history: 20` (configurable)
- MicroBrain context compression (when enabled)
- History pruning before sending to the inference API

Each request to the hosted lane typically includes the full conversation context that AshatOS has already trimmed. The host should not manage conversation state.

---

## Compatibility Summary

| Aspect | Finding |
|---|---|
| **MicroBrain request format** | OpenAI `messages` array, `http://127.0.0.1:18081/v1/chat/completions` |
| **MicroBrain response format** | `choices[0].message.content` |
| **MainBrain request format** | OpenAI `messages` array, `http://127.0.0.1:18080/v1/chat/completions` |
| **MainBrain response format** | `choices[0].message.content` |
| **Authentication** | Currently no auth on local endpoints (ApiKey is empty) |
| **Timeout behavior** | 120–180s per request, no automatic retry |
| **Fallback behavior** | MainBrain → hosted MicroBrain → local MicroBrain (mandatory) |
| **Streaming behavior** | Not required for hosted lanes; supported but not needed |
| **Host compatibility changes** | Return OpenAI-compatible response with `choices[0].message.content` |
| **AshatOS compatibility changes** | AshatOS only needs to change `Endpoint` URLs and add `ApiKey` to `server_config.json` |

## Required Changes to the Host

1. **Request format:** Accept OpenAI `messages` array
2. **Response format:** Return `choices[0].message.content` structure
3. **Authentication:** Support `X-Ashat-Key` header (compatible with AshatOS's `api_key` field)
4. **Timeout:** Support 120–180s request timeout
5. **Streaming:** Not required for initial implementation
6. **Model field:** Accept but don't validate; endpoint determines the lane

## Required Changes to AshatOS

**Minimal changes needed.** AshatOS only needs to:

1. Update `server_config.json` `LlmApi.MainBrain.Endpoint` → `https://SPACE_URL/gradio_api/call/mainbrain`
2. Update `server_config.json` `LlmApi.MicroBrain.Endpoint` → `https://SPACE_URL/gradio_api/call/microbrain`
3. Add `AshAt-MicroBrain-Key` and `Ashat-MainBrain-Key` to `server_config.json` or Space secrets
4. No code changes to the inference pipeline required
