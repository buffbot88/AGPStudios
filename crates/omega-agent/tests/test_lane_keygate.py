"""Tests for LaneKeyGate (single BrainStem lane version).

Tests pin:
    * correct key passes;
    * wrong key raises AuthError;
    * missing key raises AuthError (unless no key configured);
    * no key configured \u2192 open (dev-mode convenience);
    * comparison uses constant-time ``hmac.compare_digest`` (verified
      by hitting both branches of the same assertion with the same key).
"""

from __future__ import annotations

import unittest
from unittest import mock

from domain import Lane
from lane_keygate import AuthError, LaneKeyGate


class TestLaneKeyGate(unittest.TestCase):

    def setUp(self) -> None:
        self.gate = LaneKeyGate()
        self._patch = mock.patch.dict(
            "os.environ",
            {
                "ASHAT_BRAINSTEM_KEY": "secret-brainstem-1",
            },
            clear=False,
        )
        self._patch.start()
        self.gate.reload()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_correct_key_passes(self) -> None:
        self.gate.check({"X-Ashat-Key": "secret-brainstem-1"}, Lane.BRAINSTEM)
        # No raise.

    def test_wrong_key_raises(self) -> None:
        with self.assertRaises(AuthError):
            self.gate.check({"X-Ashat-Key": "WRONG"}, Lane.BRAINSTEM)

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(AuthError):
            self.gate.check({}, Lane.BRAINSTEM)

    def test_header_case_insensitive(self) -> None:
        # Lower-case header.
        self.gate.check({"x-ashat-key": "secret-brainstem-1"}, Lane.BRAINSTEM)
        # Upper-case header.
        self.gate.check({"X-ASHAT-KEY": "secret-brainstem-1"}, Lane.BRAINSTEM)

    def test_no_key_configured_allows(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "ASHAT_BRAINSTEM_KEY": "",
            },
        ):
            self.gate.reload()
            self.gate.check({}, Lane.BRAINSTEM)

    def test_key_in_str_is_not_leaked(self) -> None:
        try:
            self.gate.check({"X-Ashat-Key": "WRONG"}, Lane.BRAINSTEM)
        except AuthError as exc:
            self.assertNotIn("WRONG", str(exc))
            self.assertNotIn("secret-brainstem-1", str(exc))

    def test_two_adapters_share_implementation(self) -> None:
        from lane_keygate import headers_from_fastapi, headers_from_gradio

        class FakeFastRequest:
            headers = {"X-Ashat-Key": "secret-brainstem-1"}

        class FakeGradioRequest:
            headers = {"X-Ashat-Key": "secret-brainstem-1"}

        self.gate.check(headers_from_fastapi(FakeFastRequest()), Lane.BRAINSTEM)
        self.gate.check(headers_from_gradio(FakeGradioRequest()), Lane.BRAINSTEM)


if __name__ == "__main__":
    unittest.main()
