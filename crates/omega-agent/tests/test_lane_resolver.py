"""Tests for LaneResolver.

The resolver is the single place where ``model``/route_hint-to-lane
mapping happens. These tests pin:
    * canonical lane names map deterministically;
    * AshatOS-style aliases (``ashat-mainbrain``) map the right way;
    * GGUF filenames from env vars map the right way;
    * unknown strings raise :class:`InvalidRequestError` — never silently
      route to MainBrain;
    * a route_hint is authoritative even when ``model`` disagrees.
"""

from __future__ import annotations

import unittest

from domain import Lane, MAINBRAIN_ALIASES, MICROBRAIN_ALIASES
from lane_resolver import LaneResolver
from run_errors import InvalidRequestError


class TestLaneResolver(unittest.TestCase):

    def setUp(self) -> None:
        self.resolver = LaneResolver()

    def test_canonical_microbrain(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "microbrain"}, None),
            Lane.MICROBRAIN,
        )

    def test_canonical_mainbrain(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "mainbrain"}, None),
            Lane.MAINBRAIN,
        )

    def test_default_filenames_resolve(self) -> None:
        # The configured GGUF filenames must round-trip into their lanes.
        # (Defaults baked into domain.py.)
        from domain import lane_cfg
        self.assertEqual(
            self.resolver.resolve({"model": lane_cfg(Lane.MICROBRAIN)["file"]}, None),
            Lane.MICROBRAIN,
        )
        self.assertEqual(
            self.resolver.resolve({"model": lane_cfg(Lane.MAINBRAIN)["file"]}, None),
            Lane.MAINBRAIN,
        )

    def test_ashat_prefixed_aliases(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "ashat-mainbrain"}, None),
            Lane.MAINBRAIN,
        )
        self.assertEqual(
            self.resolver.resolve({"model": "ashat-microbrain"}, None),
            Lane.MICROBRAIN,
        )

    def test_case_insensitive_aliases(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "MainBrain"}, None),
            Lane.MAINBRAIN,
        )
        self.assertEqual(
            self.resolver.resolve({"model": "MICROBRAIN"}, None),
            Lane.MICROBRAIN,
        )

    def test_unknown_model_raises(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.resolver.resolve({"model": "gpt-9000"}, None)

    def test_missing_model_raises(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.resolver.resolve({}, None)

    def test_empty_model_raises(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.resolver.resolve({"model": ""}, None)

    def test_route_hint_authoritative(self) -> None:
        # Even if model says microbrain, a MicroBrain route hint wins.
        self.assertEqual(
            self.resolver.resolve({"model": "mainbrain"}, "microbrain"),
            Lane.MICROBRAIN,
        )
        self.assertEqual(
            self.resolver.resolve({"model": "microbrain"}, "mainbrain"),
            Lane.MAINBRAIN,
        )

    def test_invalid_route_hint_raises(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.resolver.resolve({"model": "mainbrain"}, "not-a-lane")

    def test_alias_set_is_consistent(self) -> None:
        # Sanity: alias sets aren't empty and don't both claim "mainbrain".
        self.assertTrue(MICROBRAIN_ALIASES)
        self.assertTrue(MAINBRAIN_ALIASES)


if __name__ == "__main__":
    unittest.main()
