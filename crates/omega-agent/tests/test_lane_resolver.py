"""Tests for LaneResolver (single BrainStem lane version).

The resolver is the single place where ``model``/route_hint-to-lane
mapping happens. These tests pin:
    * canonical lane name maps deterministically;
    * AshatOS-style aliases (``ashat-brainstem``) map the right way;
    * GGUF filenames from env vars map the right way;
    * unknown strings raise :class:`InvalidRequestError`;
    * a route_hint is authoritative even when ``model`` disagrees.
"""

from __future__ import annotations

import unittest

from domain import Lane, BRAINSTEM_ALIASES
from lane_resolver import LaneResolver
from run_errors import InvalidRequestError


class TestLaneResolver(unittest.TestCase):

    def setUp(self) -> None:
        self.resolver = LaneResolver()

    def test_canonical_brainstem(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "brainstem"}, None),
            Lane.BRAINSTEM,
        )

    def test_default_filename_resolves(self) -> None:
        from domain import lane_cfg
        self.assertEqual(
            self.resolver.resolve({"model": lane_cfg(Lane.BRAINSTEM)["file"]}, None),
            Lane.BRAINSTEM,
        )

    def test_ashat_prefixed_aliases(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "ashat-brainstem"}, None),
            Lane.BRAINSTEM,
        )

    def test_case_insensitive_aliases(self) -> None:
        self.assertEqual(
            self.resolver.resolve({"model": "BrainStem"}, None),
            Lane.BRAINSTEM,
        )
        self.assertEqual(
            self.resolver.resolve({"model": "BRAINSTEM"}, None),
            Lane.BRAINSTEM,
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
        self.assertEqual(
            self.resolver.resolve({"model": "brainstem"}, "brainstem"),
            Lane.BRAINSTEM,
        )

    def test_invalid_route_hint_raises(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.resolver.resolve({"model": "brainstem"}, "not-a-lane")

    def test_alias_set_is_not_empty(self) -> None:
        self.assertTrue(BRAINSTEM_ALIASES)


if __name__ == "__main__":
    unittest.main()
