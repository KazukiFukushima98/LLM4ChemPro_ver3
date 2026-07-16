"""Unit tests for ga.run_ga (no Aspen required; mock evaluator).

At present these mainly verify the x alignment (filtering to the pruned topology). BO is the
production optimizer, so there is no exhaustive GA test suite, but the x_for_topology wiring is
checked independently on the GA path as well.

Run:
    uv run python -m unittest tests.test_ga
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import topology as T  # noqa: E402
from evaluator import DetailedResult, Metrics  # noqa: E402

try:
    from test_topology import make_bypass_toggle_ss
except ImportError:  # pragma: no cover
    from tests.test_topology import make_bypass_toggle_ss


class TestGAXAlignmentWithPruning(unittest.TestCase):
    """The continuous x is narrowed to the variables of the concrete topology (after pruning) before reaching the evaluator."""

    def test_x_filtered_to_pruned_topology(self) -> None:
        from ga import run_ga

        ss = make_bypass_toggle_ss()
        case = {
            "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7},
            "penalty_weight": 1.0e5,
            "ga": {"pop_size": 8, "n_gen": 3},
        }

        mismatches: list[tuple[int, int]] = []
        seen_dims: set[int] = set()

        class _DimCheckMock:
            def evaluate_topology(self, topology, x_list):
                expected = len(T.continuous_variables(topology))
                out = []
                for x in x_list:
                    if len(x) != expected:
                        mismatches.append((len(x), expected))
                    seen_dims.add(len(x))
                    energy = 100.0 + sum(float(v) % 100.0 for v in x)
                    out.append(Metrics(specific_energy=energy, purity=0.95, recovery=0.85))
                return out

            def evaluate_detailed(self, topology, x):
                return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])

        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_ga(ss, case, _DimCheckMock(), seed=5)

        self.assertEqual(mismatches, [],
                         f"dimension of x does not match the topology's variable count: {mismatches}")
        # the GA must have visited both the pruned side (2 variables) and the 2-stage side (4 variables)
        # (deterministic for a fixed seed. If it does not, change the seed and check reproducibility)
        self.assertIn(2, seen_dims, f"pruned side never evaluated: dims={seen_dims}")
        self.assertIn(4, seen_dims, f"2-stage side never evaluated: dims={seen_dims}")
        self.assertEqual(len(gen_log), 3)
        # the timing field "t" (elapsed seconds) is present and monotonically non-decreasing
        ts = [g["t"] for g in gen_log]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])), ts)
        # wall-clock evaluation time t_eval per generation (non-negative)
        for g in gen_log:
            self.assertGreaterEqual(g["t_eval"], 0.0)


class TestGACostObjective(unittest.TestCase):
    """12.2: with objective=minimize_cost the GA fitness runs on the cost axis and the run completes."""

    def test_cost_mode_completes(self) -> None:
        from ga import run_ga

        class _FeasibleMock:
            def evaluate_topology(self, topology, x_list):
                return [
                    Metrics(specific_energy=300.0, purity=0.96, recovery=0.92,
                            energy_breakdown={"VP1": 100.0})
                    for _ in x_list
                ]

            def evaluate_detailed(self, topology, x):
                return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])

        ss = make_bypass_toggle_ss()
        case = {
            "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7,
                                     "objective": "minimize_cost"},
            "feed": {"flowbase": "MASS", "basis": "MOLE-FRAC",
                     "totflow": 80307.0, "co2_frac": 0.15},   # kmol/h (molar interpretation)
            "ga": {"pop_size": 6, "n_gen": 2},
        }
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_ga(ss, case, _FeasibleMock(), seed=7)
        self.assertEqual(len(gen_log), 2)
        # cost scale (tens of $/t) with no penalty -> fitness is a small value, not BAD
        self.assertLess(gen_log[-1]["best_fitness"], 1000.0)


if __name__ == "__main__":
    unittest.main()
