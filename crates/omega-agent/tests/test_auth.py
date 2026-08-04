"""Tests for private BrainStem API authentication."""

from __future__ import annotations

import unittest

from auth import is_valid_api_key


class TestApiKeyValidation(unittest.TestCase):
    def test_matching_key_is_valid(self) -> None:
        self.assertTrue(is_valid_api_key("secret", "secret"))

    def test_missing_expected_key_is_invalid(self) -> None:
        self.assertFalse(is_valid_api_key("secret", ""))

    def test_missing_supplied_key_is_invalid(self) -> None:
        self.assertFalse(is_valid_api_key("", "secret"))

    def test_wrong_key_is_invalid(self) -> None:
        self.assertFalse(is_valid_api_key("wrong", "secret"))


if __name__ == "__main__":
    unittest.main()
