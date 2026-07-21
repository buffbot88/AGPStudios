"""One-shot refactor: hide `_gradio_blocks` construction inside a function so HF Spaces'
static type-scanner does not find a `gr.Blocks` instance at module-level globals().

Background. The live Space at https://huggingface.co/spaces/stressthismess/ashatnh was
returning Gradio's auth login HTML (`Login / TypeError: fetch failed / username / password`)
on /api/public_status while everything else 503'd or 504'd. Root cause: HF Spaces' static
scanner iterates module globals() looking for bindings whose value is a `gr.Blocks` or
`gr.Interface` instance. On a positive match, HF launches its OWN Gradio runner (with
HF-injected auth) and binds port 7860 BEFORE our `__main__` uvicorn or the ASGI dispatch.
Even after our prior commits (1f7b3c7: drop duplicate FastAPI placeholder; 7797dc4:
daemon-thread startup; log-instead-of-assert) the Gradio scanner still fires because it
matches on TYPE (`gr.Blocks` instance), not NAME.

Fix. Wrap the entire `with gr.Blocks(...) as _gradio_blocks:` builder body inside a
function `_build_gradio_blocks()`. The Blocks instance now exists only as a local
variable inside the function during the mount call -- never in module globals(). HF's
scanner can't find it; the only top-level binding visible is `app`, which we WANT uvicorn
to dispatch. The mount's `blocks=` argument becomes a function CALL, not a module binding:
`blocks=_build_gradio_blocks()`.

Side effect. The previous separate `_gradio_blocks.queue(...)` call is redundant -- the
queue configuration moves inside `_build_gradio_blocks()` right before the `return b`.
"""
from pathlib import Path

p = Path("app.py")
src = p.read_text(encoding="utf-8")
original_size = len(src)


# === A. Wrap the `with gr.Blocks(...) as _gradio_blocks:` block in a function ===

# Open anchor -- unique because the title string is unique.
OPEN_LINE = 'with gr.Blocks(title="AshatOS Neural Host") as _gradio_blocks:\n'
assert OPEN_LINE in src, "OPEN_LINE anchor not found"

# Close anchor -- the last `_metrics_trigger.click(...)` block + its closing ')' line.
CLOSE_ANCHOR = """    _metrics_trigger.click(
        fn=_public_metrics_json,
        inputs=[],
        outputs=[gr.Textbox(visible=False)],
        api_name="public_metrics",
        concurrency_limit=1,
    )
"""

assert CLOSE_ANCHOR in src, "CLOSE_ANCHOR anchor not found"

o_start = src.find(OPEN_LINE)
o_end = src.find(CLOSE_ANCHOR, o_start) + len(CLOSE_ANCHOR)
old_block_body = src[o_start:o_end]

# Strip the opening line; indent the rest one more level; swap `as _gradio_blocks:`
# for `as b:` so we can configure the queue on `b` at the end.
opening_line_replacement = "with gr.Blocks(title=\"AshatOS Neural Host\") as b:\n"
body_after_open = old_block_body[len(OPEN_LINE):]  # note: keeps all inner content as-is

# Indent every body line one extra level (4 spaces added).
inner = body_after_open.split("\n")
indented = ["    " + line for line in inner]
indented_text = "\n".join(indented)

new_builder = (
    'def _build_gradio_blocks() -> "gr.Blocks":\n'
    '    """Build the Gradio dashboard Blocks.\n'
    '\n'
    '    Wrapped in a function -- not at module level -- so HF Spaces\' static\n'
    '    type-scanner cannot find a `gr.Blocks` instance in globals(). On a\n'
    '    match HF launches its own Gradio runner with HF-injected auth on port\n'
    '    7860, which is why our `/api/*` endpoints returned Gradio\'s login\n'
    '    HTML instead of our JSON on the live Space. The builder below runs\n'
    '    only at the moment of mount; the instance never enters the module\'s\n'
    '    namespace.\n'
    '    """\n'
    '    with gr.Blocks(title="AshatOS Neural Host") as b:\n'
    + indented_text
    + '\n'
    '    b.queue(default_concurrency_limit=1, max_size=QUEUE_LIMIT)\n'
    '    return b\n'
)

# Splice the new builder for the old block in place.
src = src[:o_start] + new_builder + src[o_end:]


# === B. Delete the now-redundant `_gradio_blocks = ... .queue(...)` line ===
# It is now part of `_build_gradio_blocks()` (just before `return b`).

# Find the `_gradio_blocks.queue(...)` line, plus its blank line above, to clean up the
# section 12 header spacing.
import re
queue_pattern = re.compile(
    r"\n\n?_gradio_blocks\.queue\(default_concurrency_limit=1, max_size=QUEUE_LIMIT\)\n\n",
)
m = queue_pattern.search(src)
assert m is not None, "queue() line not found in section 12"
src = src[: m.start()] + src[m.end():]


# === C. Swap `blocks=_gradio_blocks,` for `blocks=_build_gradio_blocks(),` in the mount ===

OLD_MOUNT_LINE = "    blocks=_gradio_blocks,\n"
NEW_MOUNT_LINE = "    blocks=_build_gradio_blocks(),\n"
assert OLD_MOUNT_LINE in src, "OLD_MOUNT_LINE anchor not found"
assert src.count(OLD_MOUNT_LINE) == 1, f"expected 1, got {src.count(OLD_MOUNT_LINE)}"
src = src.replace(OLD_MOUNT_LINE, NEW_MOUNT_LINE, 1)


# === D. Rewrite the stale "ONE FastAPI signpost" comment block above the mount ===
# The previous version's commentary was about removing the duplicate FastAPI placeholder
# -- that fight is over (no longer relevant). Replace with a tighter note about why
# the mount uses `_build_gradio_blocks()` inline.

# Em-dash-free anchors to dodge str_replace escape ambiguity; the whole block is
# a contiguous run between two ASCII anchors.
OLD_COMMENT_START = "# ONE FastAPI signpost for HF Spaces' static scanner AND our ASGI/uvicorn serving.\n"
OLD_COMMENT_END = "#   (b) SCRIPT mode: HF falls back to plain `python app.py`. Our\n#       `__main__` block binds 7860 and serves the same mounted FastAPI.\n"

assert OLD_COMMENT_START in src, "OLD_COMMENT_START not found"
assert OLD_COMMENT_END in src, "OLD_COMMENT_END not found"

s_idx = src.find(OLD_COMMENT_START)
e_idx = src.find(OLD_COMMENT_END, s_idx) + len(OLD_COMMENT_END)
old_comment_block = src[s_idx:e_idx]

NEW_COMMENT_BLOCK = (
    "# Mount Gradio inside the SAME FastAPI so user routes share one port with\n"
    "# Gradio's UI / WS / queue. `gr.mount_gradio_app(...)` mutates `_fastapi_app`\n"
    "# in place (verified empirically: `gr.mount_gradio_app(...) is _fastapi_app`\n"
    "# is True) and returns the same FastAPI object, so `app` and `_fastapi_app`\n"
    "# end up aliased to one object holding our decorator routes AND the Gradio\n"
    "# Mount at path=\"/\".\n"
    "#\n"
    "# The `blocks=_build_gradio_blocks()` argument calls the builder function\n"
    "# inline. Inside that function the `gr.Blocks` instance never enters\n"
    "# module globals() which is what HF Spaces' static scanner iterates to\n"
    "# decide whether to launch its own parallel Gradio runner (with auth).\n"
    "# Without this lazy-build escape, the only top-level binding HF sees is\n"
    "# `app` -- the right thing. With it, every /api/* request hits our\n"
    "# FastAPI route directly on port 7860 instead of Gradio's auth-shim.\n"
)

src = src[:s_idx] + NEW_COMMENT_BLOCK + src[e_idx:]


# === Write back ===
new_size = len(src)
print(
    f"OK: app.py rewritten. {original_size} -> {new_size} bytes "
    f"({new_size - original_size:+d})."
)
p.write_text(src, encoding="utf-8")
