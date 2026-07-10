#!/usr/bin/env python3
"""Pure-function asset-strategy picking for the llama-server installer.

Dependency-light on purpose: importing this module must not transitively import
gradio, fastapi, huggingface_hub, or any other runtime-only library, so unit
tests can exercise the install-strategy logic without booting the Space.

What lives here, what does not:
    IN:  a set of asset names returned by ``GET /repos/.../releases/tags/{tag}``
         (the ``assets[*].name`` array) and the pinned tag string.
    OUT: an ordered, deduplicated list of asset filenames the installer should
         attempt, in priority order. The orchestrator (``app.py``) still owns
         HTTP fetch, temp-file lifecycle, archive extraction, and logging.
"""

from __future__ import annotations

__all__ = [
    "candidate_asset_names",
    "pick_download_strategies",
    "filter_linux_binaries",
    "filter_any_archive",
    "LINUX_NEEDLES",
    "ARCHIVE_SUFFIXES",
]

# ──────────────────────────────────────────────────────────────────────────
# Constants — kept here so tests can assert against them.
# ──────────────────────────────────────────────────────────────────────────

LINUX_NEEDLES: tuple[str, ...] = ("ubuntu-x64", "linux-x64", "linux-amd64")
ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".tar.gz", ".tgz")

# Asset-name prefix used by llama.cpp's GitHub release for every per-host
# binary, e.g. ``llama-b9945-bin-ubuntu-x64.tar.gz``. Checked case-insensitive.
_BIN_PREFIX_TEMPLATE = "llama-{tag}"


# ──────────────────────────────────────────────────────────────────────────
# URL-pattern guess — used when GitHub metadata is unavailable.
# ──────────────────────────────────────────────────────────────────────────

def candidate_asset_names(tag: str) -> list[str]:
    """Return ordered list of likely llama.cpp release-asset filenames.

    These are URL-guess candidates — not names that have been confirmed to
    exist. Used as a fallback when ``GET /releases/tags/{tag}`` returns no
    assets (or fails entirely). The orchestrator must drop any guess that
    isn't confirmed by ``available_assets`` if it has that info.
    """
    pairs: list[tuple[str, str]] = [
        ("ubuntu-x64", "zip"),
        ("ubuntu-x64-cuda", "zip"),
        ("ubuntu-x64-vulkan", "zip"),
        ("ubuntu-arm64", "zip"),
        ("linux-x64", "zip"),
        ("linux-x64-cuda", "zip"),
        ("linux-amd64", "zip"),
        ("ubuntu-x64", "tar.gz"),
        ("linux-x64", "tar.gz"),
        ("linux-amd64", "tar.gz"),
    ]
    seen: set[str] = set()
    names: list[str] = []
    for os_name, ext in pairs:
        fname = (
            f"{_BIN_PREFIX_TEMPLATE.format(tag=tag)}-{os_name}.tar.gz"
            if ext == "tar.gz"
            else f"{_BIN_PREFIX_TEMPLATE.format(tag=tag)}-bin-{os_name}.{ext}"
        )
        if fname not in seen:
            seen.add(fname)
            names.append(fname)
    return names


# ──────────────────────────────────────────────────────────────────────────
# Filters against a real GitHub ``assets[]`` array.
# ──────────────────────────────────────────────────────────────────────────

def filter_linux_binaries(available: set[str], tag: str) -> list[str]:
    """Pick real linux binary assets from the GitHub release's ``assets[]``.

    Filters in this order:
      1. ``name.lower().startswith(llama-{tag})``        — discards cudart-*,
         readmes, source tarballs, signed checksums whose name doesn't match
         the canonical per-tag prefix.
      2. contains ``-bin-`` *or* ``llama-server``        — must look like a
         per-host binary artifact.
      3. ends with ``.zip`` / ``.tar.gz`` / ``.tgz``      — must be an archive
         we can extract.
      4. contains one of ``LINUX_NEEDLES``               — host-family filter
         (ubuntu-x64, linux-x64, linux-amd64).

    Sorted alphabetically so the output is deterministic across Python runs.
    Returns ``[]`` (never raises) when nothing matches.
    """
    prefix = _BIN_PREFIX_TEMPLATE.format(tag=tag).lower()
    out: list[str] = []
    for asset in sorted(available):
        lower = asset.lower()
        if not lower.startswith(prefix):
            continue
        if "llama-server" not in lower and "-bin-" not in lower:
            continue
        if not lower.endswith(ARCHIVE_SUFFIXES):
            continue
        if any(needle in lower for needle in LINUX_NEEDLES):
            out.append(asset)
    return out


def filter_any_archive(available: set[str]) -> list[str]:
    """Fallback bucket: every archive in the asset list regardless of platform.

    Used only when ``filter_linux_binaries`` returns nothing. The installer
    will still try to extract each; non-linux binaries will simply fail to
    produce a runnable ``llama-server`` and the loop will move on. Mostly
    useful for niche cases (custom llama.cpp flavors where the asset is
    named unusually).
    """
    out: list[str] = []
    for asset in sorted(available):
        if asset.lower().endswith(ARCHIVE_SUFFIXES):
            out.append(asset)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Composer — what the installer actually walks.
# ──────────────────────────────────────────────────────────────────────────

def pick_download_strategies(
    available_assets: set[str],
    tag: str,
) -> list[str]:
    """Compose the ordered list of asset names the installer should try.

    Strategy order, with rationale:
      1. **Real GitHub assets filtered to linux binaries** (preferred — these
         are names that are KNOWN to exist on the release, so no wasted 404s).
      2. **Fallback to every archive in the release** if step 1 is empty
         (e.g. an unfamiliar platform-specific asset name).
      3. **URL-pattern guesses** always appended — covers the empty-assets
         case (GitHub metadata fetch failed) AND provides a defensive last
         tier even when real assets were found.

    After assembly:
      - Deduplicates (preserving first occurrence).
      - Skips any URL-guess name that is NOT in ``available_assets`` when
        ``available_assets`` is non-empty (so we don't probe URLs the release
        proves don't exist).

    Returns a list of asset filenames (not URLs). The caller builds URLs.
    """
    primary: list[str] = []
    if available_assets:
        primary = filter_linux_binaries(available_assets, tag)
        if not primary:
            primary = filter_any_archive(available_assets)

    candidates: list[str] = list(primary) + candidate_asset_names(tag)

    seen: set[str] = set()
    deduped: list[str] = []
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        if available_assets and name not in available_assets:
            # Real GitHub metadata disagrees with this guess — don't probe it.
            continue
        deduped.append(name)
    return deduped
