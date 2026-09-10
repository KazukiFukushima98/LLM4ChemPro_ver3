"""Unit tests for the comparison target (one-shot optimization, baseline/) (no Aspen required).

Scope:
- structural consistency of ss_lee2.json / ss_lee3.json (Lee Fig.2-style SS, with a fixed COMP per stage)
- analytic enumeration of the valid one-hot combinations (81 / 4096) and exhaustive buildability
  (a design invariant)
- ga_onehot (GA over categorical structure genes): completion, one-hot guarantee, determinism
- completion and one-hot guarantee under runtime injection into BO (patch_bo_for_onehot)

Run:
    uv run python -m unittest tests.test_baseline
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
BASE = os.path.normpath(os.path.join(HERE, "..", "baseline"))
for _p in (SRC, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import topology as T  # noqa: E402
from evaluator import DetailedResult, Metrics  # noqa: E402
from ga_onehot import genes_to_bits, onehot_groups, run_ga_onehot  # noqa: E402
from run_baseline import assert_all_buildable, build_onehot_fixed_features, patch_bo_for_onehot  # noqa: E402

LEE2 = os.path.join(BASE, "ss_lee2.json")
LEE3 = os.path.join(BASE, "ss_lee3.json")

_MM = {"tie": False}  # per-stage independent membranes


class _SmoothMock:
    """Mock returning deterministic, finite Metrics (same shape as in test_bo)."""

    def evaluate_topology(self, topology, x_list):
        out = []
        for x in x_list:
            energy = 100.0 + sum((float(v) % 100.0) for v in x)
            out.append(Metrics(specific_energy=energy, purity=0.95, recovery=0.85))
        return out

    def evaluate_detailed(self, topology, x):
        return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])


class TestLeeSeeds(unittest.TestCase):

    def test_lee2_shape(self):
        ss = T.load_ss(LEE2)
        self.assertEqual(len(T.binary_variables(ss)), 12)
        self.assertEqual([len(g) for g in onehot_groups(ss)], [3, 3, 3, 3])
        # per-stage independent membranes: 2 membranes x (area, p_perm, perm) = 6, plus the pout of each stage's COMP x2 = 8
        self.assertEqual(len(T.continuous_variables(ss, _MM)), 8)

    def test_lee3_shape(self):
        ss = T.load_ss(LEE3)
        self.assertEqual(len(T.binary_variables(ss)), 24)
        self.assertEqual([len(g) for g in onehot_groups(ss)], [4] * 6)
        # 3 membranes x 3, plus the pout of each stage's COMP x3 = 12
        self.assertEqual(len(T.continuous_variables(ss, _MM)), 12)

    def test_lee2_series_no_recycle_topology(self):
        """The one-hot assignment for the series case (M1->M2->P, retentate->R) gives the correct 2-stage structure."""
        ss = T.load_ss(LEE2)
        q = {f"q_{k}": 0 for k in range(1, 13)}
        q.update({"q_2": 1, "q_6": 1, "q_9": 1, "q_12": 1})  # V2->F2, V3->R, V5->P, V6->R
        topo = T.active_topology(ss, q)
        self.assertIsNone(T.is_buildable(topo))
        self.assertEqual(set(topo["units"]), {"COMP1", "COMP2", "MEMB1", "MEMB2"})
        self.assertIn(("V2", "V11"), topo["arcs"])  # M1 permeate -> F2 pre-mixer (upstream of COMP2)
        self.assertIn(("V5", "V7"), topo["arcs"])

    def test_lee2_unfed_m2_prunes_to_single_stage(self):
        """In an assignment where nothing feeds M2, COMP2+M2 are pruned as a chain and a valid single-stage configuration remains."""
        ss = T.load_ss(LEE2)
        q = {f"q_{k}": 0 for k in range(1, 13)}
        q.update({"q_3": 1, "q_6": 1})  # V2->P, V3->R (nothing feeds the M2 branch)
        topo = T.active_topology(ss, q)
        self.assertIsNone(T.is_buildable(topo))
        self.assertEqual(set(topo["units"]), {"COMP1", "MEMB1"})


class TestOnehotEnumeration(unittest.TestCase):

    def test_lee2_combo_count_and_exclusivity(self):
        ss = T.load_ss(LEE2)
        combos = build_onehot_fixed_features(ss)
        self.assertEqual(len(combos), 3 ** 4)  # 81
        groups = onehot_groups(ss)
        for ff in combos:
            for g in groups:
                self.assertEqual(sum(ff[i] for i in g), 1.0)

    def test_lee3_combo_count(self):
        ss = T.load_ss(LEE3)
        combos = build_onehot_fixed_features(ss)
        self.assertEqual(len(combos), 4 ** 6)  # 4096

    def test_lee2_all_combos_buildable(self):
        """Design invariant: with one-hot, every combination is buildable (all 81 verified)."""
        ss = T.load_ss(LEE2)
        assert_all_buildable(ss, build_onehot_fixed_features(ss))

    @unittest.skipUnless(os.environ.get("BASELINE_FULL") == "1",
                         "Heavy exhaustive verification (~10 min). Run it with BASELINE_FULL=1. "
                         "Safety is preserved regardless, because the production runner performs "
                         "the same check on every start-up")
    def test_lee3_all_combos_buildable(self):
        """The same for the 3-stage version (all 4096 verified; the same check the runner performs).

        Run over the full set with BASELINE_FULL=1. Skipped in the everyday suite.
        """
        ss = T.load_ss(LEE3)
        assert_all_buildable(ss, build_onehot_fixed_features(ss))

    def test_lee3_sampled_combos_buildable(self):
        """Spot check of the 3-stage version (128 combinations drawn deterministically from all 4096). For the everyday suite."""
        ss = T.load_ss(LEE3)
        combos = build_onehot_fixed_features(ss)
        assert_all_buildable(ss, combos[::32])  # 128 combinations


class TestGAOnehot(unittest.TestCase):

    def _case(self) -> dict:
        return {
            "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7},
            "penalty_weight": 1.0e5,
            "ga": {"pop_size": 8, "n_gen": 3},
            "membrane_model": dict(_MM),
        }

    def test_genes_to_bits_onehot(self):
        ss = T.load_ss(LEE2)
        groups = onehot_groups(ss)
        bits = genes_to_bits([0, 1, 2, 0], groups, 12)
        self.assertEqual(sum(bits), 4.0)
        for g, choice in zip(groups, [0, 1, 2, 0]):
            self.assertEqual(bits[g[choice]], 1.0)

    def test_completes_and_best_is_onehot(self):
        ss = T.load_ss(LEE2)
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_ga_onehot(ss, self._case(), _SmoothMock(), seed=3)
        n_bin = len(T.binary_variables(ss))
        n_cont = len(T.continuous_variables(ss, _MM))
        self.assertEqual(len(best), n_bin + n_cont)
        for g in onehot_groups(ss):
            self.assertEqual(sum(best[i] for i in g), 1.0, "best is not one-hot")
        self.assertEqual(len(gen_log), 3)
        self.assertGreaterEqual(n_evals, 8)
        ts = [g["t"] for g in gen_log]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])), ts)
        # wall-clock evaluation time t_eval per generation (non-negative)
        for g in gen_log:
            self.assertGreaterEqual(g["t_eval"], 0.0)
        # the continuous part lies within bounds
        for v, cv in zip(best[n_bin:], T.continuous_variables(ss, _MM)):
            lo, hi = cv["bounds"]
            self.assertGreaterEqual(v, lo - 1e-9)
            self.assertLessEqual(v, hi + 1e-9)

    def test_deterministic_with_same_seed(self):
        ss = T.load_ss(LEE3)
        with redirect_stdout(io.StringIO()):
            a = run_ga_onehot(ss, self._case(), _SmoothMock(), seed=11)
            b = run_ga_onehot(ss, self._case(), _SmoothMock(), seed=11)
        self.assertEqual(a[0], b[0])
        self.assertEqual([g["best_fitness"] for g in a[1]],
                         [g["best_fitness"] for g in b[1]])

    def test_x_dims_match_pruned_topology(self):
        """The x reaching the evaluator matches the variable dimension of the concrete topology (after pruning)."""
        ss = T.load_ss(LEE2)
        mismatches = []

        class _DimCheck(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                expected = len(T.continuous_variables(topology, _MM))
                for x in x_list:
                    if len(x) != expected:
                        mismatches.append((len(x), expected))
                return super().evaluate_topology(topology, x_list)

        with redirect_stdout(io.StringIO()):
            run_ga_onehot(ss, self._case(), _DimCheck(), seed=5)
        self.assertEqual(mismatches, [])


class TestBOOnehotInjection(unittest.TestCase):
    """With patch_bo_for_onehot (the same code as the runner), run_bo completes and preserves one-hot."""

    def test_bo_with_injected_onehot_completes(self):
        import bo as bo_mod
        from bo import run_bo
        ss = T.load_ss(LEE2)
        case = {
            "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7},
            "penalty_weight": 1.0e5,
            "bo": {"n_init": 6, "n_iter": 2, "q_batch": 2, "patience": 0},
            "membrane_model": dict(_MM),
        }
        orig_ff, orig_sobol = bo_mod._build_fixed_features, bo_mod._sobol_initial
        try:
            patch_bo_for_onehot(ss, seed=1)
            with redirect_stdout(io.StringIO()):
                best, gen_log, n_evals = run_bo(ss, case, _SmoothMock(), seed=9)
        finally:
            bo_mod._build_fixed_features = orig_ff   # leave no effect on other tests
            bo_mod._sobol_initial = orig_sobol
        n_bin = len(T.binary_variables(ss))
        self.assertEqual(len(best), n_bin + len(T.continuous_variables(ss, _MM)))
        for g in onehot_groups(ss):
            self.assertEqual(sum(round(best[i]) for i in g), 1, "best is not one-hot")
        self.assertEqual(len(gen_log), 2)


if __name__ == "__main__":
    unittest.main()
