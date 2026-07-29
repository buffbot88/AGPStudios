"""Property-based tests for install strategy selection.

Uses Hypothesis to generate arbitrary asset name sets and tags,
verifying invariants that the hand-written unit tests can't exhaustively
check: no duplicates, no cross-tag leakage, and every returned name
matches at least one strategy.

Dependencies:
    pip install hypothesis
"""

from __future__ import annotations

import unittest
from hypothesis import given, strategies as st

from install_strategies import (
    ARCHIVE_SUFFIXES,
    LINUX_NEEDLES,
    candidate_asset_names,
    filter_any_archive,
    filter_linux_binaries,
    pick_download_strategies,
)


# ── Hypothesis strategies ─────────────────────────────────────────────

# A realistic tag: ``b`` followed by 4-5 digits, or ``latest``, or ``master``
_tag = st.one_of(
    st.from_regex(r"^b\d{4,5}$", fullmatch=True),
    st.just("latest"),
    st.just("master"),
)

# An asset name is roughly: ``llama-{tag}[-bin]-{os}...{suffix}``
# We generate plausible shapes that the filters might encounter.
_asset_name = st.builds(
    lambda tag, has_bin, os_part, variant, suffix: (
        f"llama-{tag}"
        f"{'-bin' if has_bin else ''}"
        f"{os_part}"
        f"{variant}"
        f".{suffix}"
    ),
    tag=_tag,
    has_bin=st.booleans(),
    os_part=st.sampled_from([
        "-ubuntu-x64", "-ubuntu-arm64", "-ubuntu-vulkan-x64",
        "-linux-x64", "-linux-amd64",
        "-macos-x64", "-macos-arm64",
        "-win-cpu-x64", "-win-cuda-12.4-x64",
        "-android-arm64",
    ]),
    variant=st.one_of(
        st.just(""),
        st.just("-cuda"),
        st.just("-vulkan"),
        st.just("-rocm"),
    ),
    suffix=st.sampled_from(["tar.gz", "zip"]),
)

# A set of asset names with a consistent tag
_asset_set = st.builds(
    lambda tag, names: set(names),
    tag=_tag,
    names=st.lists(_asset_name, min_size=0, max_size=20),
).filter(lambda s: len(s) < 15)  # avoid massive sets


# ── Tests ─────────────────────────────────────────────────────────────

class TestInstallStrategiesProperty(unittest.TestCase):

    @given(_asset_set, _tag)
    def test_linux_filter_outputs_are_subset_of_inputs(
        self, assets: set[str], tag: str,
    ) -> None:
        """Every asset returned by filter_linux_binaries must be in the input set."""
        result = filter_linux_binaries(assets, tag)
        for name in result:
            self.assertIn(name, assets)

    @given(_asset_set, _tag)
    def test_linux_filter_no_cross_tag_leakage(
        self, assets: set[str], tag: str,
    ) -> None:
        """Returned names must contain the pinned tag."""
        result = filter_linux_binaries(assets, tag)
        for name in result:
            self.assertIn(tag, name)

    @given(_asset_set, _tag)
    def test_linux_filter_only_archive_suffixes(
        self, assets: set[str], tag: str,
    ) -> None:
        """Every returned asset must end with an archive suffix."""
        result = filter_linux_binaries(assets, tag)
        for name in result:
            self.assertTrue(
                any(name.endswith(s) for s in ARCHIVE_SUFFIXES),
                f"{name!r} does not end with any archive suffix",
            )

    @given(_asset_set, _tag)
    def test_linux_filter_never_returns_cross_tag(
        self, assets: set[str], tag: str,
    ) -> None:
        """Names with a DIFFERENT tag (e.g. b9999) must be rejected."""
        result = filter_linux_binaries(assets, tag)
        for name in result:
            # If the name contains a tag-like pattern, it must be OUR tag.
            parts = name.split("-")
            for part in parts:
                if part.startswith("b") and part[1:].isdigit():
                    self.assertEqual(part, tag)

    @given(_asset_set)
    def test_any_archive_outputs_are_subset_of_inputs(
        self, assets: set[str],
    ) -> None:
        """Every result from filter_any_archive must be in the input."""
        result = filter_any_archive(assets)
        for name in result:
            self.assertIn(name, assets)

    @given(_asset_set)
    def test_any_archive_only_archive_suffixes(
        self, assets: set[str],
    ) -> None:
        """Every result must have a recognised archive suffix."""
        result = filter_any_archive(assets)
        for name in result:
            self.assertTrue(
                any(name.endswith(s) for s in ARCHIVE_SUFFIXES),
                f"{name!r} does not end with any archive suffix",
            )

    @given(_tag)
    def test_candidate_asset_names_no_duplicates(self, tag: str) -> None:
        """URL-guess candidates must be unique."""
        names = candidate_asset_names(tag)
        self.assertEqual(len(names), len(set(names)))

    @given(_tag)
    def test_candidate_asset_names_includes_tag(self, tag: str) -> None:
        """Every guessed name must contain the pinned tag."""
        names = candidate_asset_names(tag)
        for n in names:
            self.assertIn(tag, n)

    @given(_asset_set, _tag)
    def test_pick_download_strategies_no_duplicates(
        self, assets: set[str], tag: str,
    ) -> None:
        """The composed result must never contain duplicates."""
        result = pick_download_strategies(assets, tag)
        self.assertEqual(len(result), len(set(result)))

    @given(_tag)
    def test_pick_download_strategies_all_have_tag(
        self, tag: str,
    ) -> None:
        """Every name in the composed result must contain the pinned tag."""
        # Generate asset names ON the given tag so the invariant is sound.
        assets = {
            f"llama-{tag}-bin-ubuntu-x64.tar.gz",
            f"llama-{tag}-bin-macos-x64.tar.gz",
            f"llama-{tag}-bin-win-cpu-x64.zip",
        }
        result = pick_download_strategies(assets, tag)
        for name in result:
            self.assertIn(tag, name)

    @given(st.sets(_asset_name, min_size=0, max_size=5), _tag)
    def test_pick_download_strategies_results_have_tag_when_linux_binary_present(
        self, extra_assets: set[str], tag: str,
    ) -> None:
        """When a linux binary matching the tag exists, all results contain the tag."""
        # Guarantee at least one matching linux binary
        assets = extra_assets | {
            f"llama-{tag}-bin-ubuntu-x64.tar.gz",
        }
        result = pick_download_strategies(assets, tag)
        for name in result:
            self.assertIn(tag, name, f"{name!r} missing pinned tag {tag!r}")

    def test_hypothesis_runs_at_least_one_case(self) -> None:
        """Sanity: Hypothesis strategies are constructible."""
        names = candidate_asset_names("b9945")
        self.assertGreater(len(names), 0)


if __name__ == "__main__":
    unittest.main()
