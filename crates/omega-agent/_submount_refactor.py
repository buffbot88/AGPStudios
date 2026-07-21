"""Sub-mount Gradio on `/ui/` to isolate its ASGI middleware.

The previous fixes (1f7b3c7, 3975520, 7797dc4, d0163e4) addressed module-level
detection (wrong FastAPI placeholder; literal FastAPI() AST match; synchronous
startup() blocking port bind) and type-detection of `gr.Blocks` (lazy-build
refactor inside `_build_gradio_blocks()`). Locally all four public endpoints
serve 200 -- the architecture is correct.

But the LIVE Space still returns Gradio's auth shim HTML on every /api/* /v1/*
path. The blocker is now MIDDLEWARE, not globals()/AST/type-scanning:

  `gr.mount_gradio_app(app=_fastapi_app, blocks=..., path="/")` adds Gradio's
  ASGI middlewares (Auth, queue-tracking, span-events) to the *entire* `_fastapi_app`
  instance. Middlewares execute BEFORE Starlette routing, so even when
  `@_fastapi_app.get("/api/public_status")` is registered before `Mount("/{path}")`,
  Gradio's AuthMiddleware short-circuits every request with the shim HTML.

Surgical escape: create a SEPARATE FastAPI sub-app for Gradio, mount Gradio
inside it, then `_fastapi_app.mount("/ui", _gradio_subapp)`. Starlette's
`Mount` confines the sub-app's middlewares to matching requests only -- so
Gradio's Auth fires for `/ui/*` requests but never for `/api/*` / `/v1/*`.

Side effects:
  - Root HTTP path `/` no longer exists (no Gradio UI on `/`). Add a tiny
    `@_fastapi_app.get("/")` HTML welcome that links to `/ui/`.
  - Module-level `app` (the thing HF Spaces' ASGI dispatch reads) becomes
    `_fastapi_app` post-sub-mount. Same FastAPI-shaped object our `__main__`
    and any external `uvicorn app:app` invocation will pick up.
  - /v1/chat/completions, /v1/models, /health, /api/public_status, /api/public_metrics
    all stay where AshatOS clients + operators expect them.
"""
from pathlib import Path

p = Path("app.py")
src = p.read_text(encoding="utf-8")
original_size = len(src)


# === A. Replace the final mount call with a sub-mount pattern ===

# Anchors: the file currently has the inline mount + the post-mount defensive
# check + the `_log.info("FastAPI routes mounted via gr.mount_gradio_app: ...")`
# line. We replace the whole region from `app = gr.mount_gradio_app(` through
# `_log.info(...)` with: sub-app creation + sub-mount + module-level alias
# + a `/` welcome endpoint + the existing routes-present defensive check.

OLD_BLOCK_START = "app = gr.mount_gradio_app(\n    app=_fastapi_app,\n    blocks=_build_gradio_blocks(),\n    path=\"/\",\n)\n"
OLD_BLOCK_END = """_log.info(
    "FastAPI routes mounted via gr.mount_gradio_app: /v1/chat/completions, "
    "/v1/models, /health, /api/public_status, /api/public_metrics"
)
"""

assert OLD_BLOCK_START in src, "OLD_BLOCK_START not found"
assert OLD_BLOCK_END in src, "OLD_BLOCK_END not found"

s_idx = src.find(OLD_BLOCK_START)
e_idx = src.find(OLD_BLOCK_END, s_idx) + len(OLD_BLOCK_END)
old_region = src[s_idx:e_idx]

# Build the new region:
#  1. Build a separate FastAPI sub-app for Gradio FIRST so the global
#     lambda order mirrors the lazy-build flow.
#  2. Mount Gradio on the sub-app at "/" (root of sub-app).
#  3. Sub-mount the sub-app onto the root at "/ui".
#  4. Add a tiny @_fastapi_app.get("/") welcome HTML (the root URL no longer
#     serves Gradio; without this it'd 404).
#  5. Set module-level `app = _fastapi_app` so HF Spaces' ASGI dispatch
#     reads the right value (and our `__main__` uvicorn.run(app, ...) is unchanged).
#  6. Keep the defensive route-presence check + success log.
NEW_REGION = """# Build Gradio in its OWN sub-app. The earlier `gr.mount_gradio_app(app=_fastapi_app, ...)`
# registered Gradio's Auth/queue/lifespan ASGI middlewares GLOBALLY on _fastapi_app
# which meant they short-circuited every /api/* /v1/* request with the auth shim
# BEFORE Starlette's explicit-route matching could fire. Mounting Gradio on a
# separate FastAPI instance confines those middlewares to /ui/* sub-paths only.
_gradio_subapp = FastAPI()
_gradio_subapp = gr.mount_gradio_app(
    app=_gradio_subapp,
    blocks=_build_gradio_blocks(),
    path="/",
)
# Sub-mount Gradio's app under /ui/ on the root. Starlette's Mount dispatches
# only requests whose path begins with /ui/ to the sub-app -- everything else
# stays on _fastapi_app's explicit routes (and is therefore Gradio-middleware-free).
_fastapi_app.mount("/ui", _gradio_subapp)

# Module-level `app` for HF Spaces' ASGI dispatch. Note: this MUST be
# `_fastapi_app` (not the sub-app) so all our /v1/* /api/* /health routes are
# reachable from the ASGI dispatch root. The Gradio UI lives under /ui/.
app = _fastapi_app


# Tiny HTML landing on `/` so the Space root shows something useful now that
# Gradio is no longer served at the top level (it's behind /ui/ instead).
@_fastapi_app.get("/", response_class=HTMLResponse)
async def http_landing() -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!doctype html>"
            "<html><head><title>AshatOS Neural Host</title></head>"
            "<body style='font-family: sans-serif; max-width: 720px; margin: 40px auto; "
            "padding: 24px;'>"
            "<h1 style='color:#0EA5E9;'>AshatOS Neural Host</h1>"
            "<p>The interactive Gradio dashboard now lives under "
            "<a href='/ui/'>/ui/</a>.</p>"
            "<h2>Public HTTP endpoints</h2>"
            "<ul>"
            "<li><code>GET /health</code> &mdash; liveness + readiness</li>"
            "<li><code>GET /v1/models</code> &mdash; OpenAI-compatible model list</li>"
            "<li><code>POST /v1/chat/completions</code> &mdash; OpenAI-compatible inference</li>"
            "<li><code>GET /api/public_status</code> &mdash; per-lane status + diagnostics</li>"
            "<li><code>GET /api/public_metrics</code> &mdash; sanitized metrics &amp; events</li>"
            "</ul>"
            "</body></html>"
        ),
    )


"""

# The defensive route-presence check + success log line stay exactly as they were,
# but we want them AFTER the sub-mount + welcome endpoint + module-level app alias
# so they validate the *final* mounted app. We'll reconstruct them verbatim.
TAIL_PART = """
# Defensive verification (cheap, future-proof against Gradio's mount
# behaviour silently stripping pre-existing routes). Both `APIRoute` and
# `Route` (Starlette) names appear on `app.routes` after mount -- the
# former for our `@_fastapi_app.get/post` decorators, the latter for
# FastAPI's auto-docs routes at `/docs`, `/openapi.json`, etc.
#
# We do NOT use ``assert`` here -- an AssertionError on import would crash
# the whole module and either trip HF Spaces' restart loop (ASGI mode) or
# leave the script-mode container exiting non-zero. Logging the regression
# at ERROR level instead surfaces the same warning through the dashboard's
# event log + logs tab while keeping port 7860 bound so the Space stays
# RUNNING. An operator can then read the diagnostic line and pin a Gradio
# version that matches the operator's tolerance.
if not any(
    r.__class__.__name__ in ("APIRoute", "Route")
    and getattr(r, "path", None) == "/health"
    for r in app.routes
):
    _log.error(
        "DEFECTIVE FASTAPI MOUNT: /health missing from app.routes -- a future "
        "Gradio release likely stripped pre-existing routes on mount. /health, "
        "/v1/*, /api/* will 404 until a compatible Gradio is re-installed."
    )

_log.info(
    "FastAPI routes: /, /v1/chat/completions, /v1/models, /health, "
    "/api/public_status, /api/public_metrics; Gradio UI at /ui/"
)
"""

src = src[:s_idx] + NEW_REGION + TAIL_PART + src[e_idx:]

new_size = len(src)
print(f"OK: app.py rewritten. {original_size} -> {new_size} bytes ({new_size - original_size:+d})")
p.write_text(src, encoding="utf-8")
