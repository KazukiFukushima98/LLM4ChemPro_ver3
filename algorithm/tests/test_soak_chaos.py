"""Fault-injection soak/chaos test for SubprocessEvaluator (no Aspen, deterministic).

_chaos_worker.py reproduces the failures a real Aspen worker can exhibit (wedge,
crash, silence, stdout junk, delay), chosen at random per group but deterministic
under the seed, and this test checks three survival invariants of the parent
(SubprocessEvaluator) across many groups:

    1. evaluate_topology / evaluate_detailed never raise, and always return
       len(x_list) Metrics (canned for what was received, bad for what was lost)
    2. kill is called on a wedge or an abnormal exit, and not otherwise
    3. no zombie worker process is left behind after the test

Because the failure mode is predictable through behavior_for() (shared by the
test and the worker), this verifies not merely that the run completed but that
each failure produced the expected result.

Run:
    uv run python -m unittest algorithm.tests.test_soak_chaos          # default 24 groups (~1 min)
    CHAOS_GROUPS=200 uv run python -m unittest algorithm.tests.test_soak_chaos   # full soak
    (PowerShell: $env:CHAOS_GROUPS="200"; uv run python -m unittest ...)
    CHAOS_SEED changes the failure sequence (default 20260707).
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from evaluator import BAD_VALUE, Metrics  # noqa: E402
from subprocess_evaluator import SubprocessEvaluator  # noqa: E402
from _chaos_worker import behavior_for  # noqa: E402

CHAOS_WORKER = os.path.join(HERE, "_chaos_worker.py")

CHAOS_GROUPS = int(os.environ.get("CHAOS_GROUPS", "24"))
CHAOS_SEED = int(os.environ.get("CHAOS_SEED", "20260707"))
STALL_SEC = 4.0  # wait before declaring a failure; large enough to absorb spawn jitter (same basis as test_subprocess_evaluator)

TOPO = {"vertices": {"V0": {"role": "feed"}}, "arcs": {}, "units": {}}

# the failure modes that count as a wedge / abnormal exit and must trigger a kill
KILL_BEHAVIORS = {"crash", "partial_hang", "hang_no_output"}


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


def _is_canned(m: Metrics) -> bool:
    return m.specific_energy == 100.0 and m.purity == 0.9 and m.recovery == 0.8


def _alive_python_pids(pids: set[int]) -> list[int] | None:
    """Return those of the recorded PIDs that are still alive as python processes.

    Returns None where tasklist is unavailable (the check is then skipped).
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return None
    alive: list[int] = []
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 2 and cols[0].lower().startswith("python"):
            try:
                pid = int(cols[1])
            except ValueError:
                continue
            if pid in pids:
                alive.append(pid)
    return alive


def _expected_pattern(behavior: str, n: int) -> list[str]:
    """The expected result per failure mode (a sequence of 'canned' / 'bad')."""
    if behavior in ("normal", "slow", "garbage"):
        return ["canned"] * n
    if behavior == "crash":
        return ["canned"] + ["bad"] * (n - 1)
    if behavior == "partial_hang":
        k = n // 2
        return ["canned"] * k + ["bad"] * (n - k)
    # silent_exit / hang_no_output
    return ["bad"] * n


class _KillRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class TestChaosSoak(unittest.TestCase):

    def test_soak_topology_groups(self) -> None:
        """Injecting failures across many groups still upholds the invariants on result shape, recovery and cleanup."""
        rng = random.Random(CHAOS_SEED)
        rec = _KillRecorder()
        behavior_counts: Counter[str] = Counter()
        expected_kills = 0
        pids: set[int] = set()

        with tempfile.TemporaryDirectory() as pid_dir:
            case = {"_chaos": {"seed": CHAOS_SEED, "pid_dir": pid_dir}}
            ev = SubprocessEvaluator(
                case, aspen_file="<unused>", dmp_dir="<unused>",
                worker_script=CHAOS_WORKER, stall_sec=STALL_SEC, kill_aspen=rec,
            )

            for g in range(CHAOS_GROUPS):
                n = rng.randint(1, 5)
                dim = rng.randint(1, 3)
                x_list = [[round(rng.uniform(0.0, 100.0), 6) for _ in range(dim)]
                          for _ in range(n)]
                behavior = behavior_for(x_list, CHAOS_SEED)
                behavior_counts[behavior] += 1
                if behavior in KILL_BEHAVIORS:
                    expected_kills += 1

                out = ev.evaluate_topology(TOPO, x_list)

                # invariant 1: the shape always matches (reaching this point already proves nothing was raised)
                self.assertEqual(
                    len(out), n,
                    f"group {g} ({behavior}): {n} requested, got {len(out)} back",
                )
                # oracle: the expected pattern for this failure mode
                for i, (m, exp) in enumerate(zip(out, _expected_pattern(behavior, n))):
                    if exp == "canned":
                        self.assertTrue(
                            _is_canned(m),
                            f"group {g} ({behavior}) x[{i}]: expected canned, got {m}",
                        )
                    else:
                        self.assertTrue(
                            _is_bad(m),
                            f"group {g} ({behavior}) x[{i}]: expected bad, got {m}",
                        )

            # invariant 2: the kill count matches the number of wedge / abnormal-exit groups exactly
            self.assertEqual(
                rec.calls, expected_kills,
                f"kill count {rec.calls} != expected {expected_kills} ({dict(behavior_counts)})",
            )

            # invariant 3: no zombie worker is left behind
            for name in os.listdir(pid_dir):
                if name.endswith(".pid"):
                    pids.add(int(name[:-4]))
            alive = _alive_python_pids(pids)
            if alive is not None:
                self.assertEqual(
                    alive, [],
                    f"zombie workers left behind: PID {alive}",
                )

        print(f"\n[soak] groups={CHAOS_GROUPS} seed={CHAOS_SEED} "
              f"behaviors={dict(behavior_counts)} kills={rec.calls} "
              f"workers_spawned={len(pids)}")

    def test_soak_detailed_calls(self) -> None:
        """evaluate_detailed (the path that extracts the details of the best) also always returns a DetailedResult under fault injection."""
        rng = random.Random(CHAOS_SEED + 1)
        rec = _KillRecorder()

        with tempfile.TemporaryDirectory() as pid_dir:
            case = {"_chaos": {"seed": CHAOS_SEED, "pid_dir": pid_dir}}
            ev = SubprocessEvaluator(
                case, aspen_file="<unused>", dmp_dir="<unused>",
                worker_script=CHAOS_WORKER, stall_sec=STALL_SEC, kill_aspen=rec,
            )

            n_calls = max(4, CHAOS_GROUPS // 4)
            for _ in range(n_calls):
                x = [round(rng.uniform(0.0, 100.0), 6)]
                behavior = behavior_for([x], CHAOS_SEED)
                d = ev.evaluate_detailed(TOPO, x)
                if behavior in ("normal", "slow", "garbage", "crash"):
                    # crash exits after emitting index 0, so the single detailed point has been received
                    self.assertTrue(_is_canned(d.metrics),
                                    f"{behavior}: expected canned, got {d.metrics}")
                    self.assertIn("V0", d.stream_results)
                else:
                    self.assertTrue(_is_bad(d.metrics),
                                    f"{behavior}: expected bad, got {d.metrics}")


if __name__ == "__main__":
    unittest.main()
