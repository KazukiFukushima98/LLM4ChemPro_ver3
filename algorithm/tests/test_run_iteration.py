"""Unit tests for the numbering logic of get_next_iter_num (no Aspen, deterministic).

Builds an iterations/ tree with tempfile and checks that the returned number is
max + 1 over the numeric part of iter_NNN. The focus is on not reusing a number
after a deletion (max+1, not count+1), on gaps, and on skipping foreign entries.

importing run_iteration transitively imports simulator (-> pythoncom) and ga
(-> deap) at top level, but it never starts Aspen itself, so these tests remain
Aspen-free.

Run:
    uv run python -m unittest algorithm.tests.test_run_iteration
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import run_iteration  # noqa: E402
from evaluator import DetailedResult, Metrics  # noqa: E402

try:
    from test_topology import make_bypass_toggle_ss
except ImportError:  # pragma: no cover
    from tests.test_topology import make_bypass_toggle_ss


def _mkdirs(base: str, names: list[str]) -> None:
    """Create base/iterations/ and a directory for each entry in names."""
    iter_dir = os.path.join(base, "iterations")
    os.makedirs(iter_dir, exist_ok=True)
    for n in names:
        os.makedirs(os.path.join(iter_dir, n), exist_ok=True)


class TestBuildResultsGhostParams(unittest.TestCase):
    """Exclude the free dimensions of pruned units from optimal_params (prevents ghost signals).

    Since the x-alignment fix, the variables of a pruned membrane are free
    dimensions that do not affect the evaluation. Recording whatever value the
    optimizer happens to leave there (often a bound) would make
    extract_bounds_hit report a "pinned bound on a membrane that does not exist"
    and mislead the SST. This checks that only the variables present in the best
    topology are recorded.
    """

    _METRICS = Metrics(specific_energy=300.0, purity=0.96, recovery=0.92,
                       energy_breakdown={"VP1": 100.0})

    def _build(self, q1: float, q2: float) -> dict:
        ss = make_bypass_toggle_ss()
        # chromosome: [q_1, q_2 | MEMB1_area, MEMB1_p_perm, MEMB2_area, MEMB2_p_perm]
        best = [q1, q2, 111000.0, 0.5, 222000.0, 0.4]
        detailed = DetailedResult(metrics=self._METRICS)
        return run_iteration.build_results_dict(1, best, ss, detailed, [], 0)

    def test_pruned_membrane_params_excluded(self) -> None:
        """Bypass ON (MEMB2 pruned) -> the MEMB2 variables are not recorded."""
        results = self._build(q1=0.0, q2=1.0)
        params = results["optimal_params"]
        self.assertEqual(params["q_1"], 0)
        self.assertEqual(params["q_2"], 1)
        self.assertAlmostEqual(params["MEMB1_area"], 111000.0)
        self.assertAlmostEqual(params["MEMB1_p_perm"], 0.5)
        self.assertNotIn("MEMB2_area", params)
        self.assertNotIn("MEMB2_p_perm", params)

    def test_active_membrane_params_kept(self) -> None:
        """Feed ON (MEMB2 present) -> all variables are recorded."""
        results = self._build(q1=1.0, q2=0.0)
        params = results["optimal_params"]
        self.assertAlmostEqual(params["MEMB2_area"], 222000.0)
        self.assertAlmostEqual(params["MEMB2_p_perm"], 0.4)


class TestGetNextIterNum(unittest.TestCase):
    def test_iterations_dir_absent(self) -> None:
        """If iterations/ does not exist, return 1 and create the directory."""
        with tempfile.TemporaryDirectory() as base:
            self.assertFalse(os.path.isdir(os.path.join(base, "iterations")))
            self.assertEqual(run_iteration.get_next_iter_num(base), 1)
            self.assertTrue(os.path.isdir(os.path.join(base, "iterations")))

    def test_empty(self) -> None:
        """An empty iterations/ gives 1."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, [])
            self.assertEqual(run_iteration.get_next_iter_num(base), 1)

    def test_consecutive(self) -> None:
        """iter_001, iter_002 -> 3."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_002"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)

    def test_single(self) -> None:
        """iter_001 only -> 2."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 2)

    def test_no_overwrite_after_delete(self) -> None:
        """iter_002 only (after iter_001 was deleted) -> 3 (not 2, which count+1 would give)."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_002"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)

    def test_gap(self) -> None:
        """iter_001, iter_003 (a gap) -> 4."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_003"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 4)

    def test_ignore_non_numeric_and_non_dir(self) -> None:
        """Foreign entries (non-numeric, non-directory) are skipped; iter_001 and iter_002 count, giving 3."""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_002", "iter_foo", "notiter", "iter_"])
            # create iter_005_log.txt as a file (a non-directory foreign entry)
            with open(os.path.join(base, "iterations", "iter_005_log.txt"), "w") as f:
                f.write("log\n")
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)


class TestApplyGaOverrides(unittest.TestCase):
    """The pop/gen overrides from the CLI take effect in memory only, without dirtying case.yaml."""

    def test_overrides_applied_in_memory_only(self) -> None:
        case = run_iteration.load_case()
        snap = copy.deepcopy(case)
        with open(run_iteration.CASE_PATH, "r", encoding="utf-8") as f:
            disk_before = f.read()

        out = run_iteration.apply_ga_overrides(case, 4, 3)

        # the overridden result is returned
        self.assertEqual(out["ga"]["pop_size"], 4)
        self.assertEqual(out["ga"]["n_gen"], 3)
        # the input dict is untouched (pure function, returns a copy)
        self.assertEqual(case, snap)
        # case.yaml on disk is unchanged
        with open(run_iteration.CASE_PATH, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), disk_before)

    def test_partial_override_keeps_other(self) -> None:
        case = {"ga": {"pop_size": 20, "n_gen": 50}}
        out = run_iteration.apply_ga_overrides(case, 4, None)
        self.assertEqual(out["ga"]["pop_size"], 4)
        self.assertEqual(out["ga"]["n_gen"], 50)  # gen was not given, so it is left as is

    def test_none_passthrough_no_change(self) -> None:
        case = {"ga": {"pop_size": 20, "n_gen": 50}}
        out = run_iteration.apply_ga_overrides(case, None, None)
        self.assertEqual(out["ga"], {"pop_size": 20, "n_gen": 50})


class TestDetailedEvalRetry(unittest.TestCase):
    """Retry on a transient wedge in the detailed evaluation (a regression test for the failure mode measured in run22)."""

    class _FlakyEvaluator:
        """Mock that returns bad for the first fail_n calls and real values afterwards."""

        def __init__(self, fail_n: int) -> None:
            from evaluator import DetailedResult, Metrics
            self._fail_n = fail_n
            self.calls = 0
            self._bad = DetailedResult(metrics=Metrics.bad())
            self._good = DetailedResult(
                metrics=Metrics(specific_energy=999.0, purity=0.78, recovery=0.82),
                stream_results={"V0": {"CO2_molfrac": 0.15}},
            )

        def evaluate_detailed(self, topology, x):
            self.calls += 1
            return self._bad if self.calls <= self._fail_n else self._good

    def test_first_success_no_retry(self) -> None:
        ev = self._FlakyEvaluator(fail_n=0)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0])
        self.assertEqual(ev.calls, 1)
        self.assertAlmostEqual(d.metrics.specific_energy, 999.0)

    def test_transient_wedge_recovers_on_retry(self) -> None:
        """The pattern measured in run22: a wedge on the first call, real values recovered on retry."""
        ev = self._FlakyEvaluator(fail_n=1)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0])
        self.assertEqual(ev.calls, 2)
        self.assertAlmostEqual(d.metrics.specific_energy, 999.0)
        self.assertIn("V0", d.stream_results)

    def test_persistent_failure_returns_bad_after_budget(self) -> None:
        """Even if every attempt fails, return bad rather than raising (keeping the caller's existing path)."""
        from evaluator import BAD_VALUE
        ev = self._FlakyEvaluator(fail_n=10)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0], retries=2)
        self.assertEqual(ev.calls, 3)  # give up after the first call plus 2 retries
        self.assertGreaterEqual(d.metrics.specific_energy, BAD_VALUE)


class TestCliCommitWiring(unittest.TestCase):
    """--no-commit / --pop / --gen are wired through to run_one_iteration correctly (no Aspen).

    run_one_iteration is replaced with a mock so that only the CLI wiring of
    main() is exercised. The mock invokes neither the GA nor the auto-commit, so
    no git subprocess is started.
    """

    def _run_main(self, argv: list[str]) -> dict:
        captured: dict = {}

        def fake_run_one_iteration(base_dir, case, commit=True):
            captured["commit"] = commit
            captured["pop"] = case["ga"]["pop_size"]
            captured["gen"] = case["ga"]["n_gen"]
            return {}

        orig_fn = run_iteration.run_one_iteration
        orig_argv = sys.argv
        run_iteration.run_one_iteration = fake_run_one_iteration
        sys.argv = argv
        try:
            run_iteration.main()
        finally:
            run_iteration.run_one_iteration = orig_fn
            sys.argv = orig_argv
        return captured

    def test_no_commit_and_overrides_thread_through(self) -> None:
        cap = self._run_main(
            ["run_iteration.py", "--base-dir", "X", "--no-commit", "--pop", "4", "--gen", "3"]
        )
        self.assertFalse(cap["commit"])  # --no-commit -> commit=False (auto-commit does not fire)
        self.assertEqual(cap["pop"], 4)
        self.assertEqual(cap["gen"], 3)

    def test_default_commits(self) -> None:
        cap = self._run_main(["run_iteration.py", "--base-dir", "X"])
        self.assertTrue(cap["commit"])  # the default is commit=True


if __name__ == "__main__":
    unittest.main()
