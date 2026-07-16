"""Unit tests for the firing logic of aspen_watchdog (path 2). No Aspen, deterministic.

Run:
    uv run python -m unittest algorithm.tests.test_aspen_watchdog
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import aspen_watchdog  # noqa: E402


def _wait_until(predicate, timeout=2.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class AspenWatchdogFireTest(unittest.TestCase):

    def setUp(self):
        aspen_watchdog._CHECK_INTERVAL = 0.02          # speed the test up
        aspen_watchdog._stop.set()
        aspen_watchdog._armed.clear()
        aspen_watchdog._last_activity[0] = time.time()
        # Neutralise taskkill and the dialog click (so real AspenPlus is not killed).
        # The implementation uses subprocess.run (with a timeout; changed from the old subprocess.call).
        self._p_call = mock.patch.object(aspen_watchdog.subprocess, "run")
        self._p_dialog = mock.patch.object(
            aspen_watchdog, "_auto_close_aspen_dialog", return_value=False
        )
        self.mock_call = self._p_call.start()
        self.mock_dialog = self._p_dialog.start()

    def tearDown(self):
        aspen_watchdog.stop_watchdog()
        self._p_call.stop()
        self._p_dialog.stop()
        aspen_watchdog._CHECK_INTERVAL = 5.0

    def _taskkill_called(self):
        return self.mock_call.called

    def test_fires_when_armed_and_stalled(self):
        """When the heartbeat stops while armed, taskkill is called (the main case)."""
        aspen_watchdog.start_watchdog(stall_sec=0.1)
        with aspen_watchdog.armed():
            # After detection the worker inserts a time.sleep(3) (the dialog -> kill interval).
            # Wait with plenty of margin for the subprocess.call that follows it.
            fired = _wait_until(self._taskkill_called, timeout=5.0)
        self.assertTrue(fired, "did not fire despite being stalled and armed")
        args = self.mock_call.call_args[0][0]
        self.assertIn("taskkill", args)
        self.assertTrue(any("AspenPlus" in a for a in args))

    def test_does_not_fire_when_disarmed(self):
        """If not armed, it does not fire even when the heartbeat stops."""
        aspen_watchdog.start_watchdog(stall_sec=0.1)
        time.sleep(0.5)
        self.assertFalse(self.mock_call.called, "fired despite being disarmed")

    def test_does_not_fire_while_beating(self):
        """It does not fire while armed as long as the heartbeat continues."""
        aspen_watchdog.start_watchdog(stall_sec=0.3)
        with aspen_watchdog.armed():
            t_end = time.time() + 0.8
            while time.time() < t_end:
                aspen_watchdog.beat()
                time.sleep(0.02)
        self.assertFalse(self.mock_call.called, "fired despite a live heartbeat")

    def test_armed_context_sets_clears_and_resets_heartbeat(self):
        """armed() raises and lowers _armed, and resets the heartbeat on entry."""
        aspen_watchdog._armed.clear()
        old = aspen_watchdog._last_activity[0]
        time.sleep(0.05)
        with aspen_watchdog.armed():
            self.assertTrue(aspen_watchdog._armed.is_set())
            self.assertGreater(aspen_watchdog._last_activity[0], old)
        self.assertFalse(aspen_watchdog._armed.is_set())


if __name__ == "__main__":
    unittest.main()