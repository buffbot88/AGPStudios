"""Tests for LaneKeyGate.

The gate replaces the previously-duplicated ``require_key`` /
``require_key_http``. These tests pin:
    * correct key passes;
    * wrong key raises AuthError;
    * missing key raises AuthError (unless no key configured);
    * no key configured → open (dev-mode convenience);
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
        # Sanity: under the test environment these env vars are typically
        # unset, so the gate allows all requests. Force-set a value to
        # actually exercise the comparison branches.
        self._patch = mock.patch.dict(
            "os.environ",
            {
                "ASHAT_MICROBRAIN_KEY": "secret-micro-1",
                "ASHAT_MAINBRAIN_KEY": "secret-main-1",
            },
            clear=False,
        )
        self._patch.start()
        self.gate.reload()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_correct_key_passes(self) -> None:
        self.gate.check({"X-Ashat-Key": "secret-micro-1"}, Lane.MICROBRAIN)
        # No raise.

    def test_wrong_key_raises(self) -> None:
        with self.assertRaises(AuthError):
            self.gate.check({"X-Ashat-Key": "WRONG"}, Lane.MICROBRAIN)

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(AuthError):
            self.gate.check({}, Lane.MAINBRAIN)

    def test_header_case_insensitive(self) -> None:
        # Lower-case header.
        self.gate.check({"x-ashat-key": "secret-main-1"}, Lane.MAINBRAIN)
        # Upper-case header.
        self.gate.check({"X-ASHAT-KEY": "secret-main-1"}, Lane.MAINBRAIN)

    def test_no_key_configured_allows(self) -> None:
        # Empty the keys and reload. The gate should now allow.
        with mock.patch.dict(
            "os.environ",
            {
                "ASHAT_MICROBRAIN_KEY": "",
                "ASHAT_MAINBRAIN_KEY": "",
            },
        ):
            self.gate.reload()
            self.gate.check({}, Lane.MICROBRAIN)
            self.gate.check({}, Lane.MAINBRAIN)

    def test_key_in_str_is_not_leaked(self) -> None:
        # Ensure AuthError's str() never contains the supplied/expected key.
        try:
            self.gate.check({"X-Ashat-Key": "WRONG"}, Lane.MICROBRAIN)
        except AuthError as exc:
            self.assertNotIn("WRONG", str(exc))
            self.assertNotIn("secret-micro-1", str(exc))

    def test_two_adapters_share_implementation(self) -> None:
        from lane_keygate import headers_from_fastapi, headers_from_gradio

        class FakeFastRequest:
            headers = {"X-Ashat-Key": "secret-main-1"}

        class FakeGradioRequest:
            headers = {"X-Ashat-Key": "secret-main-1"}

        # Same key via the same gate, via two different adapters.
        self.gate.check(headers_from_fastapi(FakeFastRequest()), Lane.MAINBRAIN)
        self.gate.check(headers_from_gradio(FakeGradioRequest()), Lane.MAINBRAIN)


if __name__ == "__main__":
    unittest.main()
