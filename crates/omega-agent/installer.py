"""llama-server binary installer — composition layer.

Public interface (unchanged): :func:`ensure_llama_server` returns the local
path to a working llama-server binary, or ``None`` if degraded mode.

Internally split:
    * :class:`GithubReleaseClient` — adapter over ``GET /repos/.../releases/tags/{tag}``.
    * :class:`BinaryCache`          — local filesystem cache (``~/.cache/ashatos/bin``).
    * :class:`ArchiveExtractor`     — tarball/zip → executable bytes.
    * :class:`AssetDownloader`      — single-asset HTTP fetch into a temp file.
    * :func:`pick_download_strategies` (from install_strategies) — the AssetSelector.

This composer walks the "real-asset" → "URL guess" → "HF mirror" tiers,
each strategy attempt produces a diagnostic line, and the final error
message includes EVERY tier name so an operator can tell which tier failed
from the log alone.
"""

from __future__ import annotations

import json
import logging
import os
import os.path
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from install_strategies import (
    ARCHIVE_SUFFIXES,
    candidate_asset_names,
    filter_any_archive,
    filter_linux_binaries,
    pick_download_strategies,
)

_log = logging.getLogger("ashatos")


# ──────────────────────────────────────────────────────────────────────────
# Configuration (the public env vars already documented in README/DEPLOYMENT)
# ──────────────────────────────────────────────────────────────────────────

LLAMA_SERVER_VERSION: str = os.getenv("LLAMA_SERVER_VERSION", "b9945")
LLAMA_SERVER_HF_REPO: str = os.getenv("LLAMA_SERVER_HF_REPO", "RipBuffy/llama-server-mirror")
LLAMA_SERVER_HF_FILE: str = os.getenv("LLAMA_SERVER_HF_FILE", "")
LLAMA_SERVER_PATH: str = os.getenv("LLAMA_SERVER_PATH", "").strip()
USER_AGENT: str = "AshatOS-NeuralHost"


# ──────────────────────────────────────────────────────────────────────────
# GithubReleaseClient
# ──────────────────────────────────────────────────────────────────────────

class GithubReleaseClient:
    """Adapter over the GitHub release JSON."""

    def __init__(
        self,
        owner: str = "ggerganov",
        repo: str = "llama.cpp",
        user_agent: str = USER_AGENT,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.user_agent = user_agent
        self._api_base = f"https://api.github.com/repos/{owner}/{repo}/releases"
        self._release_base = (
            f"https://github.com/{owner}/{repo}/releases/download"
        )

    def latest(self) -> str | None:
        """Resolve ``latest`` to a tag string from GitHub's latest release."""
        try:
            req = urllib.request.Request(
                f"{self._api_base}/latest",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return data.get("tag_name") or None
        except urllib.error.HTTPError as exc:
            _log.warning("llama: GitHub API HTTP %s: %s", exc.code, exc.reason)
            return None
        except Exception as exc:
            _log.warning(
                "llama: GitHub API error: %s: %s",
                type(exc).__name__, exc,
            )
            return None

    def assets_for(self, tag: str) -> set[str]:
        """Return :class:`set` of asset names found on the tag's release page.

        Empty set on any error (network, 404, rate-limit, malformed body).
        """
        url = f"{self._api_base}/tags/{tag}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            _log.warning(
                "llama: release %s not found via API (HTTP %s)",
                tag, exc.code,
            )
            return set()
        except Exception as exc:
            _log.warning(
                "llama: GitHub API error for %s: %s: %s",
                tag, type(exc).__name__, exc,
            )
            return set()
        names: set[str] = set()
        for a in data.get("assets") or []:
            n = a.get("name") if isinstance(a, dict) else None
            if n:
                names.add(n)
        return names

    def download_url(self, tag: str, asset: str) -> str:
        return f"{self._release_base}/{tag}/{asset}"


# ──────────────────────────────────────────────────────────────────────────
# BinaryCache
# ──────────────────────────────────────────────────────────────────────────

class BinaryCache:
    """Local filesystem cache at ``~/.cache/ashatos/bin``.

    Both :class:`ArchiveExtractor` and the HF-mirror fallback write into
    this directory.
    """

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or (Path.home() / ".cache" / "ashatos" / "bin")
        self.base.mkdir(parents=True, exist_ok=True)

    def find_existing(self) -> str | None:
        for name in ("llama-server", "llama-server.exe"):
            p = self.base / name
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        return None

    def writable(self) -> bool:
        return os.access(self.base, os.W_OK)


# ──────────────────────────────────────────────────────────────────────────
# ArchiveExtractor
# ──────────────────────────────────────────────────────────────────────────

class ArchiveExtractor:
    """Extract a tarball/zip into a target directory, find llama-server.

    Path-flattened so zip-slip-style entries collapse to basenames before
    write (defence-in-depth).
    """

    def __init__(self, target_dir: Path) -> None:
        self.target_dir = target_dir
        self.target_dir.mkdir(parents=True, exist_ok=True)

    def extract(self, archive_path: str) -> str | None:
        """Extract and return the path to the llama-server binary, if found."""
        extracted: dict[str, bytes] = {}
        try:
            if archive_path.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as zf:
                    for n in zf.namelist():
                        if n.endswith("/"):
                            continue
                        extracted[Path(n).name] = zf.read(n)
            elif archive_path.endswith((".tar.gz", ".tgz")):
                with tarfile.open(archive_path, "r:gz") as tf:
                    for m in tf.getmembers():
                        if m.isdir():
                            continue
                        src = tf.extractfile(m)
                        if src is not None:
                            extracted[Path(m.name).name] = src.read()
            else:
                return None
        except Exception as exc:
            _log.info(
                "llama: archive open failed (%s): %s: %s",
                archive_path, type(exc).__name__, exc,
            )
            return None

        for fname, content in extracted.items():
            target = self.target_dir / fname
            try:
                target.write_bytes(content)
                target.chmod(0o755)
            except Exception as exc:
                _log.info("llama: write %s failed: %s", fname, exc)

        candidate = self.target_dir / "llama-server"
        if candidate.is_file():
            return str(candidate)
        # Some flavours ship the binary under a slightly-different name.
        for f in self.target_dir.iterdir():
            if f.is_file() and "llama-server" in f.name and os.access(f, os.X_OK):
                shutil.copy2(str(f), str(candidate))
                candidate.chmod(0o755)
                return str(candidate)
        return None


# ──────────────────────────────────────────────────────────────────────────
# AssetDownloader
# ──────────────────────────────────────────────────────────────────────────

class AssetDownloader:
    """HTTP-downloads one asset to a temp file, then delegates extraction."""

    def __init__(
        self,
        client: GithubReleaseClient,
        cache: BinaryCache,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.client = client
        self.cache = cache
        self.extractor = ArchiveExtractor(cache.base)
        self.user_agent = user_agent

    def fetch_one(self, tag: str, asset: str) -> str | None:
        """Try one asset; return its extracted llama-server path or None."""
        if not asset.lower().endswith(ARCHIVE_SUFFIXES):
            # Single-file binary, not an archive — write straight to cache.
            url = self.client.download_url(tag, asset)
            try:
                target = self.cache.base / asset
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/octet-stream",
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    target.write_bytes(resp.read())
                target.chmod(0o755)
                return str(target)
            except Exception as exc:
                _log.info(
                    "llama: %s → %s: %s",
                    asset, type(exc).__name__, exc,
                )
                return None

        suffix = ".tar.gz" if asset.endswith((".tar.gz", ".tgz")) else ".zip"
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp_path = tmp.name
            tmp.close()
        except Exception as exc:
            _log.warning(
                "llama: tempfile creation failed for %s: %s: %s",
                asset, type(exc).__name__, exc,
            )
            return None
        try:
            try:
                url = self.client.download_url(tag, asset)
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/octet-stream",
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    with open(tmp_path, "wb") as f:
                        f.write(resp.read())
            except urllib.error.HTTPError as exc:
                _log.info("llama: %s → HTTP %s (%s)", asset, exc.code, exc.reason)
                return None
            except urllib.error.URLError as exc:
                _log.info("llama: %s → URL error: %s", asset, exc.reason)
                return None
            except Exception as exc:
                _log.info(
                    "llama: %s → %s: %s",
                    asset, type(exc).__name__, exc,
                )
                return None

            result = self.extractor.extract(tmp_path)
            if result:
                _log.info("llama: ready at %s (via %s)", result, asset)
                return result
            _log.info("llama: %s → archive did not contain llama-server", asset)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ──────────────────────────────────────────────────────────────────────────
# HF-mirror adapter (kept here so the installer composer stays self-contained)
# ──────────────────────────────────────────────────────────────────────────

class HfMirror:
    """Try to grab the binary directly from a Hugging Face mirror repo.

    The mirror is expected to host one LFS file per pinned tag, named
    ``llama-server-{tag}`` (or the override ``LLAMA_SERVER_HF_FILE``).
    """

    def __init__(
        self,
        repo: str = LLAMA_SERVER_HF_REPO,
        file_template: str = LLAMA_SERVER_HF_FILE,
        token: str | None = None,
    ) -> None:
        self.repo = repo
        self.file_template = file_template
        self.token = token

    def fetch(self, tag: str) -> str | None:
        if not self.repo:
            _log.info("llama: HF mirror not configured; skipping")
            return None
        fname = (self.file_template or "").strip() or f"llama-server-{tag}"
        _log.info("llama: trying HF mirror %s/%s ...", self.repo, fname)
        try:
            # Imported lazily so unit tests can run without huggingface_hub.
            from huggingface_hub import hf_hub_download
            path = hf_hub_download(
                repo_id=self.repo,
                filename=fname,
                revision="main",
                token=self.token,
            )
            p = Path(path)
            if not p.is_file():
                _log.warning("llama: HF mirror returned non-file path: %s", path)
                return None
            try:
                p.chmod(0o755)
                size_mb = round(p.stat().st_size / (1024 * 1024), 1)
                _log.info("llama: HF mirror ready at %s (%.1f MiB)", str(p), size_mb)
            except OSError:
                _log.info("llama: HF mirror ready at %s", str(p))
            return str(p)
        except Exception as exc:
            _log.info(
                "llama: HF mirror download failed: %s: %s",
                type(exc).__name__, exc,
            )
            return None


# ──────────────────────────────────────────────────────────────────────────
# LlamaBinaryInstaller — the composer
# ──────────────────────────────────────────────────────────────────────────

class LlamaBinaryInstaller:
    """Orchestrates the cache-check → GitHub real → URL-guess → HF mirror tiers."""

    def __init__(
        self,
        gh: GithubReleaseClient | None = None,
        cache: BinaryCache | None = None,
        downloader: AssetDownloader | None = None,
        mirror: HfMirror | None = None,
    ) -> None:
        self.gh = gh or GithubReleaseClient()
        self.cache = cache or BinaryCache()
        self.downloader = downloader or AssetDownloader(self.gh, self.cache)
        self.mirror = mirror or HfMirror(
            token=os.getenv("HF_TOKEN") or None,
        )

    # ── Public ────────────────────────────────────────────────────────

    def ensure(self, version: str = LLAMA_SERVER_VERSION) -> str | None:
        # 0. Operator-pinned path wins regardless of tier.
        explicit = self._look_for_explicit_path()
        if explicit:
            return explicit

        # 1. Cache hit is fastest.
        cached = self.cache.find_existing()
        if cached:
            _log.info("llama: using cached binary at %s", cached)
            return cached

        # 2. Resolve tag (literal vs 'latest').
        tag = self._resolve_tag(version)
        if not tag:
            _log.warning("llama: tag resolution failed; no binary")
            return None
        _log.info("llama: release tag: %s", tag)

        # 3. GitHub tier — real assets + URL guesses.
        release_assets = self.gh.assets_for(tag)
        if release_assets:
            _log.info(
                "llama: GitHub reports %d assets for %s",
                len(release_assets), tag,
            )
        strategies = pick_download_strategies(release_assets, tag)
        for asset in strategies:
            result = self.downloader.fetch_one(tag, asset)
            if result:
                return result

        _log.warning(
            "llama: GitHub tiers exhausted (%d strategies); falling back to HF mirror",
            len(strategies),
        )

        # 4. HF mirror tier.
        mirror_result = self.mirror.fetch(tag)
        if mirror_result:
            return mirror_result

        # 5. Final failure — opacity today; let the caller see why from the log.
        _log.error("llama: ALL TIERS FAILED (GitHub + HF mirror)")
        return None

    # ── Private ───────────────────────────────────────────────────────

    def _look_for_explicit_path(self) -> str | None:
        if not LLAMA_SERVER_PATH:
            return None
        p = Path(LLAMA_SERVER_PATH)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None

    def _resolve_tag(self, version: str) -> str | None:
        if version and version != "latest":
            return version
        return self.gh.latest()


# ──────────────────────────────────────────────────────────────────────────
# Module-level facade — `from installer import ensure_llama_server`
# (matches the previous public symbol so app.py imports stay unchanged)
# ──────────────────────────────────────────────────────────────────────────

_INSTALLER_SINGLETON: LlamaBinaryInstaller | None = None


def _get_installer() -> LlamaBinaryInstaller:
    global _INSTALLER_SINGLETON
    if _INSTALLER_SINGLETON is None:
        _INSTALLER_SINGLETON = LlamaBinaryInstaller()
    return _INSTALLER_SINGLETON


def ensure_llama_server() -> str | None:
    """Public install facade. Returns the binary path or ``None``."""
    return _get_installer().ensure(LLAMA_SERVER_VERSION)
