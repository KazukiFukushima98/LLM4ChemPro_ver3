"""SubprocessEvaluator のプロセス隔離・配線の単体テスト（Aspen 不要・決定的）。

ダミー worker（_dummy_worker.py）を注入し、spawn / 結果ストリーム / stall 検知 /
kill / bad 埋め / 部分結果の保全を実機なしで検証する。

自然 wedge は timing 依存（COORDINATION 参照）なので forced hang を使う。
親は wedge でも forced hang でも「タイムアウト→kill」で同じに扱うため、これで十分。

実行:
    uv run python -m unittest algorithm.tests.test_subprocess_evaluator
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from evaluator import BAD_VALUE, Metrics  # noqa: E402
from subprocess_evaluator import SubprocessEvaluator  # noqa: E402

DUMMY_WORKER = os.path.join(HERE, "_dummy_worker.py")

# ダミー worker は topology を一切見ない（x_list の長さだけ使う）。最小の有効トポロジー。
TOPO = {"vertices": {"V0": {"role": "feed"}}, "arcs": {}, "units": {}}


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


def _is_canned(m: Metrics) -> bool:
    return m.specific_energy == 100.0 and m.purity == 0.9 and m.recovery == 0.8


class _KillRecorder:
    """kill_aspen 注入用。呼び出し回数を数える（実 Aspen は殺さない）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _make_evaluator(behavior: str, kill_rec: _KillRecorder) -> SubprocessEvaluator:
    case = {"_test_behavior": behavior}
    return SubprocessEvaluator(
        case,
        aspen_file="<unused>",
        dmp_dir="<unused>",
        worker_script=DUMMY_WORKER,
        stall_sec=5.0,          # forced hang を検知。初回 spawn jitter（AV スキャン等）は Windows で
                                # 数秒に及ぶことがあり、3.0 だと正常系が誤 wedge でフレークし得る
        kill_aspen=kill_rec,    # 実 Aspen を殺さないレコーダー
    )


class TestSubprocessEvaluatorTopology(unittest.TestCase):
    def test_normal_all_canned(self):
        """ハッピーパス：全件 canned、kill は呼ばれない。"""
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 4)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(_is_canned(m) for m in out))
        self.assertEqual(rec.calls, 0)

    def test_partial_hang_preserves_received(self):
        """途中 wedge：前半は保持（canned）、後半は bad、kill が呼ばれる。

        逐次返しの肝＝通過済みの x が道連れにならないことの検証。n=4 で前半 2 件。
        """
        rec = _KillRecorder()
        ev = _make_evaluator("partial_hang", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 4)
        self.assertEqual(len(out), 4)
        # 前半 n//2 = 2 件は受信済み（保持）、後半 2 件は未受信 → bad
        self.assertTrue(_is_canned(out[0]))
        self.assertTrue(_is_canned(out[1]))
        self.assertTrue(_is_bad(out[2]))
        self.assertTrue(_is_bad(out[3]))
        self.assertGreaterEqual(rec.calls, 1)

    def test_abnormal_exit_fills_bad(self):
        """異常終了（exit 2）：index0 は受信済み、残りは bad。掃除 kill が 1 回。"""
        rec = _KillRecorder()
        ev = _make_evaluator("abnormal", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual(len(out), 3)
        self.assertTrue(_is_canned(out[0]))
        self.assertTrue(_is_bad(out[1]))
        self.assertTrue(_is_bad(out[2]))
        # wedge ではない異常終了 → 残存 Aspen の掃除 kill が 1 回
        self.assertEqual(rec.calls, 1)

    def test_full_hang_all_bad(self):
        """全 wedge：1 件も返らず、全件 bad、kill が呼ばれる。"""
        rec = _KillRecorder()
        ev = _make_evaluator("full_hang", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(_is_bad(m) for m in out))
        self.assertGreaterEqual(rec.calls, 1)


class TestTimingRecords(unittest.TestCase):
    """計測（timing）: record-only の実時間記録が評価結果と整合すること。"""

    def test_normal_group_recorded(self):
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        ev.evaluate_topology(TOPO, [[0.0]] * 4)
        self.assertEqual(len(ev.timing["groups"]), 1)
        g = ev.timing["groups"][0]
        self.assertEqual(g["mode"], "topology")
        self.assertEqual(g["n_requested"], 4)
        self.assertEqual(g["n_results"], 4)
        self.assertFalse(g["wedged"])
        self.assertEqual(len(g["eval_sec"]), 4)
        self.assertTrue(all(s >= 0.0 for s in g["eval_sec"]))
        self.assertEqual(g["lost_sec"], 0.0)
        self.assertGreaterEqual(g["wall_sec"], 0.0)

    def test_wedge_group_records_lost_time(self):
        rec = _KillRecorder()
        ev = _make_evaluator("partial_hang", rec)
        ev.evaluate_topology(TOPO, [[0.0]] * 4)
        g = ev.timing["groups"][0]
        self.assertTrue(g["wedged"])
        self.assertEqual(g["n_results"], 2)
        self.assertEqual(len(g["eval_sec"]), 2)
        # 失った時間 ≈ stall_sec（5s）以上（最後の受信から kill まで）
        self.assertGreaterEqual(g["lost_sec"], 4.0)

    def test_groups_accumulate_across_calls(self):
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        ev.evaluate_topology(TOPO, [[0.0]] * 2)
        ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual([g["n_requested"] for g in ev.timing["groups"]], [2, 3])


class TestSubprocessEvaluatorDetailed(unittest.TestCase):
    def test_detailed_normal(self):
        """detailed ハッピーパス：metrics は canned。"""
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        d = ev.evaluate_detailed(TOPO, [0.0])
        self.assertTrue(_is_canned(d.metrics))
        self.assertEqual(rec.calls, 0)

    def test_detailed_full_hang_bad(self):
        """detailed wedge：metrics は bad、kill が呼ばれる。"""
        rec = _KillRecorder()
        ev = _make_evaluator("full_hang", rec)
        d = ev.evaluate_detailed(TOPO, [0.0])
        self.assertTrue(_is_bad(d.metrics))
        self.assertGreaterEqual(rec.calls, 1)


if __name__ == "__main__":
    unittest.main()
