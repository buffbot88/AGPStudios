#!/usr/bin/env python3
"""Surgical refactor of app.py for the pure-FastAPI pivot (v2).

Uses ASCII-only anchors so the unicode divider line in the file matches
the marker we look for. Finds Section 12 by the literal text
'# 12.  Launch' near the top of that section, and locates the end of
the file by 'uvicorn.run(app' which is the LAST line of the old main.

The block between (Section 12 header) and (end of file) gets replaced
with the pure-FastAPI serving section.
"""
import pathlib
import re
import sys

p = pathlib.Path("app.py")
txt = p.read_text()

# Find Section 12 header line (anchored at start of line via regex).
m = re.search(r"(?m)^# 12\.  Launch", txt)
if not m:
    sys.exit("could not locate Section 12 'Launch' header line")
sec12_start = m.start()
print(f"Section 12 starts at offset {sec12_start}")

# End of old content = the trailing uvicorn.run line. Everything from
# sec12_start through the end of the file is replaced.
sec12_end = len(txt)

# Build the new tail block.
divider = "# " + ("-" * 73)
new_tail_lines = [
    divider,
    "# 12.  Pure-FastAPI serving. The dashboard is rendered server-side as a",
    "#      complete <!DOCTYPE html> document at GET /; live updates come",
    "#      from a small JS setInterval polling /api/dashboard_html and",
    "#      swapping the innerHTML of the status + brainstem card divs in",
    "#      place. This mirrors the previous Gradio `gr.Timer` behavior but",
    "#      lives in plain FastAPI + browser fetch -- NO Gradio runtime at",
    "#      all, NO auth shim hazard, NO monkeypatch. With `sdk: docker`",
    "#      HF Spaces runs `uvicorn app:app --host 0.0.0.0 --port 7860`",
    "#      directly against this FastAPI.",
    divider,
    "",
    "",
    "# Hoist the chat-completions inner async handler to module scope so",
    "# every request reuses the same closure rather than rebuilding one",
    "# via `_make_http_chat_completions()(request)` on every request.",
    "_chat_completions_handler = _make_http_chat_completions()",
    "",
    "",
    "_app = FastAPI(title=\"AshatOS Neural Host\")",
    "",
    "",
    "@_app.get(\"/api/public_status\")",
    "async def http_public_status() -> JSONResponse:",
    "    return JSONResponse(content=_build_status())",
    "",
    "",
    "@_app.get(\"/api/public_metrics\")",
    "async def http_public_metrics() -> JSONResponse:",
    "    return JSONResponse(content=_snapshot().render_metrics())",
    "",
    "",
    "@_app.get(\"/api/dashboard_html\")",
    "async def http_dashboard_html() -> JSONResponse:",
    "    \"\"\"Live-refresh companion to GET /; client JS polls this endpoint.",
    "",
    "    Returns server-rendered status-row + brainstem-card HTML",
    "    snippets. The browser script in render_index_html polls this",
    "    endpoint and innerHTML-swaps the corresponding divs. Styling",
    "    logic stays in ONE place (dashboard.py) rather than being",
    "    duplicated in client JavaScript.",
    "    \"\"\"",
    "    snap = _snapshot()",
    "    return JSONResponse(content=render_dashboard_html_json(snap))",
    "",
    "",
    "@_app.get(\"/health\")",
    "async def http_health() -> JSONResponse:",
    "    return JSONResponse(content={",
    "        \"status\": \"ok\",",
    "        \"uptime_seconds\": round(time.time() - _started_at, 1),",
    "        \"brainstem_ready\": bool(",
    "            LANE_CONFIG[Lane.BRAINSTEM][\"model_path\"]",
    "            and os.path.isfile(LANE_CONFIG[Lane.BRAINSTEM][\"model_path\"])",
    "        ),",
    "        \"llama_server_available\": _llama_bin_path is not None,",
    "    })",
    "",
    "",
    "@_app.get(\"/v1/models\")",
    "async def http_list_models() -> JSONResponse:",
    "    return JSONResponse(content={",
    "        \"object\": \"list\",",
    "        \"data\": [",
    "            {",
    "                \"id\": lane_cfg(Lane.BRAINSTEM)[\"file\"],",
    "                \"object\": \"model\",",
    "                \"created\": int(_started_at),",
    "                \"owned_by\": \"ashatos\",",
    "            },",
    "        ],",
    "    })",
    "",
    "",
    "@_app.post(\"/v1/chat/completions\")",
    "async def http_chat_completions(request: FastRequest) -> JSONResponse:",
    "    return await _chat_completions_handler(request)",
    "",
    "",
    "@_app.get(\"/\", response_class=HTMLResponse)",
    "async def http_landing() -> HTMLResponse:",
    "    \"\"\"Public-telemetry dashboard.",
    "",
    "    Server-rendered HTML at request time; the embedded JS",
    "    setInterval polls /api/dashboard_html every",
    "    PUBLIC_REFRESH_SECONDS and updates the status row + the",
    "    single BrainStem lane card in place. Replaces the previous",
    "    Gradio `gr.Timer` behaviour with plain FastAPI + fetch.",
    "    \"\"\"",
    "    return HTMLResponse(",
    "        content=render_index_html(",
    "            snapshot_provider=_snapshot,",
    "            refresh_seconds=PUBLIC_REFRESH_SECONDS,",
    "        )",
    "    )",
    "",
    "",
    "app = _app",
    "",
    "_log.info(",
    "    \"FastAPI routes: /, /v1/chat/completions, /v1/models, /health, \"",
    "    \"/api/public_status, /api/public_metrics, /api/dashboard_html\"",
    ")",
    "",
    "",
    "if __name__ == \"__main__\":",
    "    # Local dev only. HF Spaces (sdk: docker) runs uvicorn from",
    "    # Dockerfile's ENTRYPOINT and never reaches this branch.",
    "    import uvicorn",
    "    uvicorn.run(app, host=\"0.0.0.0\", port=7860)",
]
new_tail = "\n".join(new_tail_lines) + "\n"

# Replace from sec12_start through end-of-file with the new tail.
new_txt = txt[:sec12_start] + new_tail

# Replace `from dashboard import build_dashboard` with the new helpers.
new_txt = new_txt.replace(
    "from dashboard import build_dashboard",
    "from dashboard import render_dashboard_html_json, render_index_html",
)

# Drop the dead _build_gradio_blocks() between section 10's NOTE comment
# and `_BINARY_FAILURE_EXC`.
gr_start_marker = "def _build_gradio_blocks() -> "
gr_end_marker = "_BINARY_FAILURE_EXC: dict[str, type[RunError]] = {"
i = new_txt.find(gr_start_marker)
j = new_txt.find(gr_end_marker)
if i >= 0 and j > i:
    # Find the section-10 NOTE-comment line that introduces _build_gradio_blocks.
    note_marker = (
        "#      `uvicorn.run`) AND on the FastAPI that `demo.launch()` returns\n"
        "#      (for HF Spaces' `sdk: gradio`). See `_hf_register_routes` below.\n"
    )
    note_idx = new_txt.find(note_marker, i - 500, i + 100)
    if note_idx < 0:
        # fallback: cut just the def
        cut_start = max(0, new_txt.rfind("\n\n", 0, i) + 2)
    else:
        # cut from the line AFTER the NOTE block ends (next \n)
        cut_start = new_txt.find("\n\n", note_idx + len(note_marker), i) + 2
        if cut_start < 2:
            cut_start = note_idx + len(note_marker)
    # Walk back to start of line
    while cut_start > 0 and new_txt[cut_start - 1] != "\n":
        cut_start -= 1
    print(f"Dropping _build_gradio_blocks() from offset {cut_start} to {j}")
    new_txt = new_txt[:cut_start] + new_txt[j:]
else:
    print("WARN: _build_gradio_blocks boundaries not found")

# Also drop section 10's NOTE comment that referenced the now-deleted
# `_hf_register_routes`. Replace with a short updated note.
note_old = (
    "# 10.  Dashboard \u2014 redesigned neural host homepage (single BrainStem lane)\n"
    "#      NOTE: section 10 used to hold a separate `_fastapi_app = FastAPI(...)`\n"
    "#      with five `@_fastapi_app.get/post(...)` decorator routes. That block\n"
    "#      was deleted when the auth-shim defense refactor landed; the same\n"
    "#      five endpoints are now registered on `_LOCAL_FASTAPI` (for local\n"
    "#      `uvicorn.run`) AND on the FastAPI that `demo.launch()` returns\n"
    "#      (for HF Spaces' `sdk: gradio`). See `_hf_register_routes` below.\n"
)
note_new = (
    "# 10.  Pure-FastAPI routes \u2014 registered directly on `_app` at the\n"
    "#      bottom of the file. The previous Gradio-mounted FastAPI +\n"
    "#      `_hf_register_routes` + `_LOCAL_FASTAPI` plumbing is gone\n"
    "#      now that we are on `sdk: docker` with no Gradio runtime.\n"
)
if note_old in new_txt:
    new_txt = new_txt.replace(note_old, note_new)
    print("OK updated section 10 NOTE")

# Remove the helper `_gradio_lane_handler`, `_envelope_to_response`, and
# the adapter `handler(payload_json, request)` -- all dead now that
# there's no Gradio. But this is a clean-up pass -- we'll leave them for
# now if not trivially safe to remove. They're not called by anything
# in the new _app routes.

p.write_text(new_txt)
print(f"OK rewrote app.py. New length: {len(new_txt.splitlines())} lines")
