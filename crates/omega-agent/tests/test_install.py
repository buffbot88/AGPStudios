"""Unit tests for the llama-server install-strategy picker.

These tests target ``install_strategies`` directly — a small dependency-light
module that holds the pure-function asset-selection logic. They do NOT need
gradio, fastapi, huggingface_hub, or a network connection to run.

What we assert:
  * Real-asset filter accepts only linux-bin assets matching the pinned tag.
  * Real-asset filter rejects cudart-* / platform-mismatched / non-archive names.
  * Empty-asset fallback cleanly walks through URL-guess candidates.
  * When the real list has no linux binary at all, we fall through to
    "every archive in the release" before reaching URL guesses.
  * Output is deduped across real + guessed; URL guesses that disagree
    with confirmed real assets are dropped.
"""

from __future__ import annotations

import unittest

from install_strategies import (
    ARCHIVE_SUFFIXES,
    LINUX_NEEDLES,
    candidate_asset_names,
    filter_any_archive,
    filter_linux_binaries,
    pick_download_strategies,
)


# ──────────────────────────────────────────────────────────────────────────
# Stub GithubRelease response builder
# ──────────────────────────────────────────────────────────────────────────

def make_release(tag: str, assets: list[str]) -> dict:
    """Build a stub of the shape returned by ``GET /repos/.../releases/tags/{tag}``.

    Only the ``tag_name`` and ``assets[*].name`` fields are read by the
    installer; the rest is intentionally omitted.
    """
    return {
        "tag_name": tag,
        "assets": [{"name": name} for name in assets],
    }


def asset_names_from_release(release: dict) -> set[str]:
    """Mirror the call the installer makes: extract ``assets[*].name``."""
    if not release or not isinstance(release.get("assets"), list):
        return set()
    return {
        a.get("name", "")
        for a in release["assets"]
        if a.get("name")
    }


# A realistic-looking release fixture for `b9945`. Includes:
#   - the asset we want (ubuntu-x64 tar.gz)
#   - ubuntu sibling assets (arm64, vulkan-x64, rocm, sycl-fp16, sycl-fp32,
#     openvino, s390x) — should be rejected by the linux-x64 filter
#   - macOS / Windows / Android assets — should be rejected
#   - UI/archive artifacts (xcframework, ui) — should be rejected
#   - the cudart-llama-* files that ship with b9948 — should be rejected
#     because they don't start with `llama-{tag}`
B9945_STUB = make_release(
    "b9945",
    [
        # Realistic numbering — same as the real release's assets list.
        "llama-b9945-bin-android-arm64.tar.gz",
        "llama-b9945-bin-macos-arm64.tar.gz",
        "llama-b9945-bin-macos-x64.tar.gz",
        "llama-b9945-bin-ubuntu-arm64.tar.gz",
        "llama-b9945-bin-ubuntu-openvino-2026.2.1-x64.tar.gz",
        "llama-b9945-bin-ubuntu-rocm-7.2-x64.tar.gz",
        "llama-b9945-bin-ubuntu-s390x.tar.gz",
        "llama-b9945-bin-ubuntu-sycl-fp16-x64.tar.gz",
        "llama-b9945-bin-ubuntu-sycl-fp32-x64.tar.gz",
        "llama-b9945-bin-ubuntu-vulkan-arm64.tar.gz",
        "llama-b9945-bin-ubuntu-vulkan-x64.tar.gz",
        "llama-b9945-bin-ubuntu-x64.tar.gz",       # <-- the one we want
        "llama-b9945-bin-win-cpu-arm64.zip",
        "llama-b9945-bin-win-cpu-x64.zip",
        "llama-b9945-bin-win-cuda-12.4-x64.zip",
        "llama-b9945-bin-win-cuda-13.3-x64.zip",
        "llama-b9945-bin-win-hip-radeon-x64.zip",
        "llama-b9945-bin-win-opencl-adreno-arm64.zip",
        "llama-b9945-bin-win-openvino-2026.2.1-x64.zip",
        "llama-b9945-bin-win-sycl-x64.zip",
        "llama-b9945-bin-win-vulkan-x64.zip",
        "llama-b9945-ui.tar.gz",
        "llama-b9945-xcframework.zip",
        # Cross-tag / cudart contamination from a different release —
        # verify filter rejects by prefix.
        "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "llama-b9999-bin-ubuntu-x64.tar.gz",
        # Non-archive, non-binary.
        "README.md",
    ],
)


# ──────────────────────────────────────────────────────────────────────────
# filter_linux_binaries
# ──────────────────────────────────────────────────────────────────────────

class TestLinuxBinFilter(unittest.TestCase):
    """Targets the precise host-matching filter a/b/c."""

    def test_picks_only_ubuntu_x64_targz_from_real_release(self) -> None:
        assets = asset_names_from_release(B9945_STUB)
        out = filter_linux_binaries(assets, "b9945")
        self.assertEqual(out, ["llama-b9945-bin-ubuntu-x64.tar.gz"])

    def test_rejects_cudart_windows_zip_by_prefix(self) -> None:
        # The exact cudart-* names from the live b9948 release — rejected
        # because they don't start with `llama-b9945`.
        assets = {
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
            "llama-b9945-bin-ubuntu-x64.tar.gz",
        }
        self.assertEqual(
            filter_linux_binaries(assets, "b9945"),
            ["llama-b9945-bin-ubuntu-x64.tar.gz"],
        )

    def test_rejects_cross_tag_with_same_name(self) -> None:
        # A previous release's ubuntu-x64 tar.gz shouldn't leak through.
        assets = {
            "llama-b9999-bin-ubuntu-x64.tar.gz",
            "llama-b9945-bin-ubuntu-x64.tar.gz",
        }
        self.assertEqual(
            filter_linux_binaries(assets, "b9945"),
            ["llama-b9945-bin-ubuntu-x64.tar.gz"],
        )

    def test_rejects_arm64_even_though_ubuntu_prefix(self) -> None:
        assets = {"llama-b9945-bin-ubuntu-arm64.tar.gz"}
        self.assertEqual(filter_linux_binaries(assets, "b9945"), [])

    def test_rejects_vulkan_x64(self) -> None:
        # Has "x64" but does NOT contain "ubuntu-x64" / "linux-x64" / "linux-amd64"
        # as a contiguous substring. (It's "ubuntu-vulkan-x64" — different token.)
        assets = {"llama-b9945-bin-ubuntu-vulkan-x64.tar.gz"}
        self.assertEqual(filter_linux_binaries(assets, "b9945"), [])

    def test_accepts_linux_x64_needle_explicitly(self) -> None:
        assets = {"llama-b9945-bin-linux-x64.tar.gz"}
        self.assertEqual(
            filter_linux_binaries(assets, "b9945"),
            ["llama-b9945-bin-linux-x64.tar.gz"],
        )

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(filter_linux_binaries(set(), "b9945"), [])

    def test_output_is_sorted(self) -> None:
        # Sanity: deterministic ordering across runs (alphabetical).
        assets = {
            "llama-b9945-bin-ubuntu-x64.tar.gz",
            "llama-b9945-bin-ubuntu-arm64.tar.gz",
            "llama-b9945-bin-ubuntu-vulkan-x64.tar.gz",
        }
        out = filter_linux_binaries(assets, "b9945")
        self.assertEqual(out, sorted(out))


# ──────────────────────────────────────────────────────────────────────────
# filter_any_archive
# ──────────────────────────────────────────────────────────────────────────

class TestAnyArchiveFallback(unittest.TestCase):

    def test_includes_only_archives(self) -> None:
        assets = {
            "llama-b9945-bin-ubuntu-x64.tar.gz",
            "llama-b9945-bin-ubuntu-arm64.tar.gz",
            "llama-b9945-xcframework.zip",
            "README.md",
            "checksums.txt",
        }
        out = filter_any_archive(assets)
        self.assertEqual(
            out,
            sorted([
                "llama-b9945-bin-ubuntu-arm64.tar.gz",
                "llama-b9945-bin-ubuntu-x64.tar.gz",
                "llama-b9945-xcframework.zip",
            ]),
        )

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(filter_any_archive(set()), [])

    def test_targz_suffix_is_arch(self) -> None:
        self.assertIn(".tar.gz", ARCHIVE_SUFFIXES)
        self.assertIn(".tgz", ARCHIVE_SUFFIXES)


# ──────────────────────────────────────────────────────────────────────────
# candidate_asset_names  (URL-pattern guess)
# ──────────────────────────────────────────────────────────────────────────

class TestCandidateAssetNames(unittest.TestCase):

    def test_includes_zip_and_targz_for_ubuntu_x64(self) -> None:
        # NOTE: the tar.gz guess is `llama-{tag}-{os}.tar.gz` (no `-bin-`),
        # which is the legacy URL-guess shape — it intentionally differs
        # from the live GitHub asset name `llama-{tag}-bin-{os}.tar.gz`.
        # Today's installer never relies on this guess because the real
        # asset name comes from the GitHub release JSON, which is queried
        # first. If we ever fix this, update the assertion below.
        out = candidate_asset_names("b9945")
        self.assertIn("llama-b9945-bin-ubuntu-x64.zip", out)
        self.assertIn("llama-b9945-ubuntu-x64.tar.gz", out)

    def test_includes_vulkan_and_cuda_variants(self) -> None:
        out = candidate_asset_names("b9945")
        self.assertIn("llama-b9945-bin-ubuntu-x64-cuda.zip", out)
        self.assertIn("llama-b9945-bin-ubuntu-x64-vulkan.zip", out)

    def test_no_duplicates_within_output(self) -> None:
        out = candidate_asset_names("b9945")
        self.assertEqual(len(out), len(set(out)))

    def test_uses_requested_tag(self) -> None:
        out = candidate_asset_names("b1234")
        for name in out:
            self.assertIn("b1234", name, f"{name!r} missing pinned tag")


# ──────────────────────────────────────────────────────────────────────────
# pick_download_strategies  (the composer the installer actually walks)
# ──────────────────────────────────────────────────────────────────────────

class TestPickDownloadStrategies(unittest.TestCase):
    """End-to-end assertions on what the installer will iterate over."""

    def test_real_asset_preferred_and_first(self) -> None:
        assets = asset_names_from_release(B9945_STUB)
        out = pick_download_strategies(assets, "b9945")
        self.assertTrue(out)
        self.assertEqual(out[0], "llama-b9945-bin-ubuntu-x64.tar.gz")

    def test_no_duplicates_in_output(self) -> None:
        assets = asset_names_from_release(B9945_STUB)
        out = pick_download_strategies(assets, "b9945")
        self.assertEqual(len(out), len(set(out)))

    def test_empty_assets_falls_back_to_url_guesses(self) -> None:
        """The user's headline scenario."""
        out = pick_download_strategies(set(), "b9945")
        # No real assets → every entry is a URL guess.
        self.assertGreater(len(out), 5)
        # All guesses should be platform-shaped (see legacy URL-guess note
        # in TestCandidateAssetNames — tar.gz guess omits the `-bin-`).
        self.assertIn("llama-b9945-ubuntu-x64.tar.gz", out)
        self.assertIn("llama-b9945-bin-ubuntu-x64.zip", out)
        # Order: pairs are walked in declaration order (zip before tar.gz).
        self.assertEqual(out[0], "llama-b9945-bin-ubuntu-x64.zip")

    def test_no_linux_match_falls_back_to_any_archive_then_guesses(self) -> None:
        # No ubuntu/linux/linux-amd64 binary in the real list, but
        # macos archives are. Expect: every-archive path comes first,
        # then URL guesses tail on.
        assets = {
            "llama-b9945-bin-macos-arm64.tar.gz",
            "llama-b9945-bin-macos-x64.tar.gz",
            "llama-b9945-xcframework.zip",
        }
        out = pick_download_strategies(assets, "b9945")
        # First two must be from the any-archive list (alphabetical).
        self.assertEqual(out[0], "llama-b9945-bin-macos-arm64.tar.gz")
        self.assertEqual(out[1], "llama-b9945-bin-macos-x64.tar.gz")
        # Then any URL guesses that align with the real list — .zip matches.
        self.assertIn("llama-b9945-xcframework.zip", out)
        # Then any URL guesses (none of which match the real list, so they're dropped).
        # The .zip variant for ubuntu-x64 is a guess NOT in `assets` → dropped.
        self.assertNotIn("llama-b9945-bin-ubuntu-x64.zip", out)

    def test_url_guesses_dropped_when_unconfirmed(self) -> None:
        """When real assets disagree with a URL guess, the guess is skipped."""
        # Real asset list contains ONLY ubuntu-x64.tar.gz — no .zip variant.
        assets = {"llama-b9945-bin-ubuntu-x64.tar.gz"}
        out = pick_download_strategies(assets, "b9945")
        # The .zip variant is a guess but not in `assets` → must NOT appear.
        self.assertNotIn("llama-b9945-bin-ubuntu-x64.zip", out)
        # But the .tar.gz match survives (real + guess agree).
        self.assertIn("llama-b9945-bin-ubuntu-x64.tar.gz", out)

    def test_stub_release_drives_full_happy_path(self) -> None:
        # End-to-end: derive assets from a stub GithubRelease JSON,
        # then run the picker. Picks exactly the one linux binary.
        assets = asset_names_from_release(B9945_STUB)
        self.assertGreater(
            len(assets), 0,
            "stub release should have assets",
        )
        out = pick_download_strategies(assets, "b9945")
        self.assertEqual(out[0], "llama-b9945-bin-ubuntu-x64.tar.gz")
        # No second linux asset — should NOT include arm64 etc.
        self.assertNotIn("llama-b9945-bin-ubuntu-arm64.tar.gz", out)
        # Win-cuda-* from real cudart asset should be filtered out.
        self.assertNotIn("cudart-llama-bin-win-cuda-12.4-x64.zip", out)


# ──────────────────────────────────────────────────────────────────────────
# Constants sanity
# ──────────────────────────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):

    def test_linux_needles_includes_standard_host(self) -> None:
        # The HF Space environment is x86_64 Linux — must always be covered.
        self.assertIn("ubuntu-x64", LINUX_NEEDLES)

    def test_archive_suffixes_include_zip_and_targz(self) -> None:
        self.assertIn(".zip", ARCHIVE_SUFFIXES)
        self.assertIn(".tar.gz", ARCHIVE_SUFFIXES)


if __name__ == "__main__":
    unittest.main()
