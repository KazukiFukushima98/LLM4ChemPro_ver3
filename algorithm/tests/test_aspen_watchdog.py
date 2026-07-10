"""aspen_watchdog 発火ロジックの単体テスト（経路②）。Aspen 不要・決定的。

実行:
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
        aspen_watchdog._CHECK_INTERVAL = 0.02          # 高速化
        aspen_watchdog._stop.set()
        aspen_watchdog._armed.clear()
        aspen_watchdog._last_activity[0] = time.time()
        # taskkill とダイアログクリックを無害化（実 AspenPlus を殺さない）。
        # 実装は subprocess.run（タイムアウト付き。旧 subprocess.call から変更）。
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
        """武装中に心拍が止まると taskkill が呼ばれる（本命）。"""
        aspen_watchdog.start_watchdog(stall_sec=0.1)
        with aspen_watchdog.armed():
            # 検知後に worker が time.sleep(3) を挟む（dialog→kill の間隔）。
            # その後ろにある subprocess.call が呼ばれるまで余裕を持って待つ。
            fired = _wait_until(self._taskkill_called, timeout=5.0)
        self.assertTrue(fired, "stalled+armed なのに発火しなかった")
        args = self.mock_call.call_args[0][0]
        self.assertIn("taskkill", args)
        self.assertTrue(any("AspenPlus" in a for a in args))

    def test_does_not_fire_when_disarmed(self):
        """武装していなければ心拍が止まっても発火しない。"""
        aspen_watchdog.start_watchdog(stall_sec=0.1)
        time.sleep(0.5)
        self.assertFalse(self.mock_call.called, "disarmed なのに発火した")

    def test_does_not_fire_while_beating(self):
        """武装中でも心拍が続けば発火しない。"""
        aspen_watchdog.start_watchdog(stall_sec=0.3)
        with aspen_watchdog.armed():
            t_end = time.time() + 0.8
            while time.time() < t_end:
                aspen_watchdog.beat()
                time.sleep(0.02)
        self.assertFalse(self.mock_call.called, "心拍があるのに発火した")

    def test_armed_context_sets_clears_and_resets_heartbeat(self):
        """armed() が _armed を上下させ、入口で心拍をリセットする。"""
        aspen_watchdog._armed.clear()
        old = aspen_watchdog._last_activity[0]
        time.sleep(0.05)
        with aspen_watchdog.armed():
            self.assertTrue(aspen_watchdog._armed.is_set())
            self.assertGreater(aspen_watchdog._last_activity[0], old)
        self.assertFalse(aspen_watchdog._armed.is_set())


if __name__ == "__main__":
    unittest.main()