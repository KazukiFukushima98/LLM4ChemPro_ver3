"""Unit tests for the process isolation and wiring of SubprocessEvaluator (no Aspen, deterministic).

Injects a dummy worker (_dummy_worker.py) to exercise spawn, the result stream,
stall detection, kill, bad-filling and the preservation of partial results,
without the real thing.

A natural wedge is timing-dependent (see COORDINATION), so a forced hang is used
instead. The parent treats a wedge and a forced hang identically ("timeout ->
kill"), so this is sufficient.

Run:
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

# The dummy worker never looks at the topology (it only uses the length of x_list). A minimal valid topology.
TOPO = {"vertices": {"V0": {"role": "feed"}}, "arcs": {}, "units": {}}


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


def _is_canned(m: Metrics) -> bool:
    return m.specific_energy == 100.0 and m.purity == 0.9 and m.recovery == 0.8


class _KillRecorder:
    """For injecting kill_aspen. Counts the calls (never kills real Aspen)."""

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
        stall_sec=5.0,          # detects a forced hang. First-spawn jitter (AV scanning etc.) can reach
                                # several seconds on Windows; at 3.0 the happy path could flake as a false wedge
        kill_aspen=kill_rec,    # a recorder that does not kill real Aspen
    )


class TestSubprocessEvaluatorTopology(unittest.TestCase):
    def test_normal_all_canned(self):
        """Happy path: every item is canned and kill is never called."""
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 4)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(_is_canned(m) for m in out))
        self.assertEqual(rec.calls, 0)

    def test_partial_hang_preserves_received(self):
        """Mid-way wedge: the first half is preserved (canned), the second half is bad, and kill is called.

        This is the crux of streaming results back: the x values already
        completed must not be dragged down with the wedge. With n=4 the first
        half is 2 items.
        """
        rec = _KillRecorder()
        ev = _make_evaluator("partial_hang", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 4)
        self.assertEqual(len(out), 4)
        # the first n//2 = 2 items were received (preserved); the last 2 were not -> bad
        self.assertTrue(_is_canned(out[0]))
        self.assertTrue(_is_canned(out[1]))
        self.assertTrue(_is_bad(out[2]))
        self.assertTrue(_is_bad(out[3]))
        self.assertGreaterEqual(rec.calls, 1)

    def test_abnormal_exit_fills_bad(self):
        """Abnormal exit (exit 2): index 0 was received, the rest are bad. One cleanup kill."""
        rec = _KillRecorder()
        ev = _make_evaluator("abnormal", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual(len(out), 3)
        self.assertTrue(_is_canned(out[0]))
        self.assertTrue(_is_bad(out[1]))
        self.assertTrue(_is_bad(out[2]))
        # an abnormal exit rather than a wedge -> one cleanup kill for any leftover Aspen
        self.assertEqual(rec.calls, 1)

    def test_full_hang_all_bad(self):
        """Full wedge: nothing comes back, every item is bad, and kill is called."""
        rec = _KillRecorder()
        ev = _make_evaluator("full_hang", rec)
        out = ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(_is_bad(m) for m in out))
        self.assertGreaterEqual(rec.calls, 1)


class TestTimingRecords(unittest.TestCase):
    """Timing: the record-only wall-clock measurements are consistent with the evaluation results."""

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
        # the time lost is >= about stall_sec (5s): from the last receipt to the kill
        self.assertGreaterEqual(g["lost_sec"], 4.0)

    def test_groups_accumulate_across_calls(self):
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        ev.evaluate_topology(TOPO, [[0.0]] * 2)
        ev.evaluate_topology(TOPO, [[0.0]] * 3)
        self.assertEqual([g["n_requested"] for g in ev.timing["groups"]], [2, 3])


class TestSubprocessEvaluatorDetailed(unittest.TestCase):
    def test_detailed_normal(self):
        """detailed happy path: the metrics are canned."""
        rec = _KillRecorder()
        ev = _make_evaluator("normal", rec)
        d = ev.evaluate_detailed(TOPO, [0.0])
        self.assertTrue(_is_canned(d.metrics))
        self.assertEqual(rec.calls, 0)

    def test_detailed_full_hang_bad(self):
        """detailed wedge: the metrics are bad and kill is called."""
        rec = _KillRecorder()
        ev = _make_evaluator("full_hang", rec)
        d = ev.evaluate_detailed(TOPO, [0.0])
        self.assertTrue(_is_bad(d.metrics))
        self.assertGreaterEqual(rec.calls, 1)


if __name__ == "__main__":
    unittest.main()
