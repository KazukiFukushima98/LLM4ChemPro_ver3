"""Unit tests for bo.run_bo (no Aspen required; mock evaluator).

They pull in the heavy torch/botorch imports, so they are written to run on either CPU or GPU.

Run:
    uv run python -m unittest algorithm.tests.test_bo

Kept light with n_init=4, n_iter=2, q_batch=2 (the point of the tests is determinism,
completion, and absorbing bad results).
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
from evaluator import BAD_VALUE, DetailedResult, Metrics  # noqa: E402

SEED_PATH = os.path.normpath(os.path.join(HERE, "..", "ss_seed.json"))

# Bypass-toggle SS fixture (for verifying the pruning of gated membranes).
# Under discover (-s tests) it is top-level; under `-m unittest tests.test_bo` it is a package name.
try:
    from test_topology import make_bypass_toggle_ss
except ImportError:  # pragma: no cover
    from tests.test_topology import make_bypass_toggle_ss


def _load_seed() -> dict:
    return T.load_ss(SEED_PATH)


# Lightweight BO settings (test-only; fixed values overriding the defaults)
_TEST_BO = {"n_init": 4, "n_iter": 2, "q_batch": 2}


def _make_case(bo_override: dict | None = None) -> dict:
    case = {
        "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7},
        "penalty_weight": 1.0e5,
        "bo": {**_TEST_BO, **(bo_override or {})},
    }
    return case


class _SmoothMock:
    """Returns deterministic Metrics whose energy is a norm-like function of the continuous variables (a shape the GP fit handles stably).

    binary is ignored (the topology units alone already reflect q). purity/recovery are fixed.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        self.call_count += len(x_list)
        out: list[Metrics] = []
        for x in x_list:
            energy = 100.0 + sum((float(v) % 100.0) for v in x)  # finite, smooth pseudo energy
            out.append(Metrics(specific_energy=energy, purity=0.95, recovery=0.85))
        return out

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        m = self.evaluate_topology(topology, [x])[0]
        return DetailedResult(metrics=m)


class _AllBadMock:
    """Always returns bad (edge case: does the GP fit break down?)."""

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        return [Metrics.bad() for _ in x_list]

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        return DetailedResult(metrics=Metrics.bad())


class _ShortfallLandscapeMock:
    """A landscape where every point is infeasible and shortfall is inversely correlated with energy (for verifying 12.5(a)).

    For the continuous variable a = MEMB1_area (identified by name; what matters is that it is a
    large-scale variable, so that at penalty_weight=1 the energy term (0.01*a ~= 1e3-1e4)
    dominates the shortfall term (<=0.4)):
        purity   = 0.5 + 0.399 * t   (t=(a-lo)/(hi-lo). At most 0.899 < 0.9 -> always infeasible)
        recovery = 0.85              (>= 0.7 -> shortfall comes from purity only)
        energy   = 100 + 0.01 * a
    -> the min-shortfall observation is "a maximal", whereas penalty-min (energy-dominated at
    penalty_weight=1) is "a minimal", so the two axes for returning best can be told apart.
    Every evaluated a is recorded in self.seen.
    """

    IDX = next(i for i, v in enumerate(T.continuous_variables(_load_seed()))
               if v["name"] == "MEMB1_area")
    _LO, _HI = T.continuous_variables(_load_seed())[IDX]["bounds"]

    def __init__(self) -> None:
        self.seen: list[float] = []

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        out: list[Metrics] = []
        for x in x_list:
            a = float(x[self.IDX])
            self.seen.append(a)
            t = (a - self._LO) / (self._HI - self._LO)
            out.append(Metrics(specific_energy=100.0 + 0.01 * a,
                               purity=0.5 + 0.399 * t, recovery=0.85))
        return out

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])


class TestRunBO(unittest.TestCase):

    def setUp(self) -> None:
        # bo.py imports torch. Make sure it is importable.
        from bo import run_bo  # noqa: F401

    def test_completes_on_seed_with_binary_and_continuous(self) -> None:
        """run_bo completes on the seed SS (which has both binary and continuous variables)."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _SmoothMock(), seed=1)
        # Shape of the return value
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertGreaterEqual(n_evals, _TEST_BO["n_init"])
        # best is a plain list as long as the variable dimension (binary + continuous)
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(best), len(bin_vars) + len(cont_vars))
        for v in best:
            self.assertIsInstance(v, float)
        # best_fitness is monotonically non-increasing (BO best tracking)
        bests = [g["best_fitness"] for g in gen_log]
        for prev, curr in zip(bests, bests[1:]):
            self.assertLessEqual(curr, prev + 1e-6)

    def test_deterministic_with_same_seed(self) -> None:
        """Two calls with the same seed give identical best and n_evals (determinism)."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best_a, log_a, n_a = run_bo(ss, case, _SmoothMock(), seed=42)
            best_b, log_b, n_b = run_bo(ss, case, _SmoothMock(), seed=42)
        self.assertEqual(n_a, n_b)
        # the whole gen_log matches
        self.assertEqual(
            [g["best_fitness"] for g in log_a],
            [g["best_fitness"] for g in log_b],
        )
        # best_x matches too (floating point)
        for va, vb in zip(best_a, best_b):
            self.assertAlmostEqual(va, vb, places=6)

    def test_handles_all_bad_metrics(self) -> None:
        """Even when every evaluation is bad, the GP fit does not break down and the run finishes."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _AllBadMock(), seed=7)
        # everything is bad, so best_fitness = BAD_VALUE
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        for g in gen_log:
            self.assertAlmostEqual(g["best_fitness"], BAD_VALUE, delta=1.0)

    def test_bootstrap_phase_when_all_infeasible(self) -> None:
        """While there are zero feasible points the run stays in phase 1 (bootstrap) and completes."""
        from bo import run_bo

        class _InfeasibleMock(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                out = super().evaluate_topology(topology, x_list)
                # always misses the constraints (purity 0.5 / recovery 0.5), but the observations are finite = valid
                return [Metrics(specific_energy=m.specific_energy, purity=0.5, recovery=0.5)
                        for m in out]

        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _InfeasibleMock(), seed=11)
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertTrue(all(g["phase"] == "bootstrap" for g in gen_log),
                        f"expected bootstrap on every iteration, got {[g['phase'] for g in gen_log]}")

    def test_cei_phase_when_feasible_exists(self) -> None:
        """Feasible from the initial samples (targets 0.9/0.7 vs mock 0.95/0.85) -> CEI on every iteration."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _SmoothMock(), seed=12)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log))

    def test_bootstrap_off_restores_legacy_behavior(self) -> None:
        """With bootstrap: off, CEI is used even when everything is infeasible (checks the rollback switch)."""
        from bo import run_bo

        class _InfeasibleMock(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                out = super().evaluate_topology(topology, x_list)
                return [Metrics(specific_energy=m.specific_energy, purity=0.5, recovery=0.5)
                        for m in out]

        ss = _load_seed()
        case = _make_case({"bootstrap": "off"})
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _InfeasibleMock(), seed=13)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log))

    def test_retry_bad_recovers_transient_wedge(self) -> None:
        """A transient wedge (bad on the first call, success on re-evaluation) is recovered by retry and does not contaminate the GP."""
        from bo import run_bo

        class _TransientWedgeMock(_SmoothMock):
            """bad on the first call for each x, real values from the second call on (reproduces a transient wedge)."""

            def __init__(self) -> None:
                super().__init__()
                self._seen: set[tuple] = set()

            def evaluate_topology(self, topology, x_list):
                out = []
                for x in x_list:
                    key = tuple(round(float(v), 9) for v in x)
                    if key in self._seen:
                        out.extend(super().evaluate_topology(topology, [x]))
                    else:
                        self._seen.add(key)
                        out.append(Metrics.bad())
                return out

        ss = _load_seed()
        case = _make_case()
        nominal = _TEST_BO["n_init"] + _TEST_BO["n_iter"] * _TEST_BO["q_batch"]
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _TransientWedgeMock(), seed=14)
        # every point is bad on the first call -> all recovered by retry => twice the nominal evaluation count, best is finite
        self.assertEqual(n_evals, 2 * nominal)
        self.assertLess(gen_log[-1]["best_fitness"], BAD_VALUE)

    def test_bootstrap_best_returns_min_shortfall(self) -> None:
        """12.5(a): when the run ends with everything infeasible, best is the min-shortfall observation, not penalty-min.

        At penalty_weight=1 the legacy penalty-min picks the energy-dominated point (= a minimal),
        but the search axis of the bootstrap phase is shortfall, so it should return a maximal
        (the best purity).
        """
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["penalty_weight"] = 1.0  # separates the penalty-min choice from the shortfall-min choice
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=21)
        self.assertTrue(all(g["phase"] == "bootstrap" for g in gen_log))
        # the largest observed a (= the smallest shortfall) is returned
        self.assertAlmostEqual(best[mock.IDX], max(mock.seen), places=6)

    def test_bootstrap_off_best_keeps_legacy_penalty_min(self) -> None:
        """12.5(a) rollback switch: with bootstrap: off, best is still returned by the legacy penalty-min."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"bootstrap": "off"})
        case["penalty_weight"] = 1.0
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=22)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log))
        # at penalty_weight=1, fitness ~= energy = 100 + 0.01a -> the smallest a is returned
        self.assertAlmostEqual(best[mock.IDX], min(mock.seen), places=6)

    def test_bootstrap_yaml_false_treated_as_off(self) -> None:
        """YAML 1.1 parses `off` as the bool False. False must be treated as off too."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"bootstrap": False})
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=23)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log),
                        f"bootstrap=False should mean off, got {[g['phase'] for g in gen_log]}")

    def test_fixed_features_exclude_unbuildable_combos(self) -> None:
        """Unbuildable combinations of a toggle pair (only one may be ON) are excluded from the search space."""
        import bo as bo_mod
        import topology as T2
        ss = _load_seed()
        # V6 has a fixed (V6,V8) residue. Adding two candidates makes out-degree>1 when ON -> unbuildable
        ss["arcs"][("V6", "V1")] = {"type": "recycle", "candidate": "q_1"}
        ss["arcs"][("V6", "V4")] = {"type": "recycle", "candidate": "q_2"}
        bin_vars = T2.binary_variables(ss)
        combos = bo_mod._build_fixed_features(ss, bin_vars)
        # only (0,0) survives (either one ON gives V6 out-degree 2, a non-membrane split = unbuildable)
        self.assertEqual(combos, [{0: 0.0, 1: 0.0}])

    def test_gp_failure_falls_back_to_sobol(self) -> None:
        """The loop does not die on a GP fit exception; it completes every iteration via the Sobol fallback.

        Even if fit_gpytorch_mll throws because of degenerate initial observations (zero variance)
        or numerical instability, this stability guard keeps a whole BO iteration (= an Aspen budget
        of several hours) from being lost.
        """
        import bo
        ss = _load_seed()
        case = _make_case()

        def _boom(*args, **kwargs):
            raise RuntimeError("forced GP failure (test)")

        orig = bo.fit_gpytorch_mll
        bo.fit_gpytorch_mll = _boom
        try:
            with redirect_stdout(io.StringIO()):
                best, gen_log, n_evals = bo.run_bo(ss, case, _SmoothMock(), seed=3)
        finally:
            bo.fit_gpytorch_mll = orig

        # every iteration proceeds via the fallback, and the evaluation count covers all iterations
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertEqual(
            n_evals,
            _TEST_BO["n_init"] + _TEST_BO["n_iter"] * _TEST_BO["q_batch"],
        )
        # best has the normal shape (as long as the variable dimension)
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(best), len(bin_vars) + len(cont_vars))


class TestMembraneModelIntegration(unittest.TestCase):
    """12.1: with membrane_model enabled, the permeance variables enter the BO search dimensions and the run completes."""

    def test_tie_mode_adds_one_shared_dimension(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["membrane_model"] = {"tie": True}
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=51)
        # the seed has 4 continuous variables (2 membranes x 2; COMP1 is fixed at blower pressure = not a variable) + the shared MEMB_perm = 5 dimensions
        self.assertEqual(len(best), 5)
        lo, hi = T.continuous_variables(ss, {"tie": True})[0]["bounds"]
        self.assertGreaterEqual(best[0], lo - 1e-9)  # the first entry is MEMB_perm
        self.assertLessEqual(best[0], hi + 1e-9)

    def test_untied_mode_adds_per_membrane_dimension(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["membrane_model"] = {"tie": False}
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=52)
        # 4 continuous variables (2 membranes x 2) + MEMB1_perm + MEMB2_perm = 6 dimensions
        self.assertEqual(len(best), 6)


class TestPhasePatience(unittest.TestCase):
    """12.5(c) The _PhasePatience state machine (pure logic)."""

    def test_patience_zero_is_disabled(self) -> None:
        import bo as bo_mod
        p = bo_mod._PhasePatience(0, floor_iters=1)
        for it in range(50):
            self.assertFalse(p.update("cei", 1.0, it))

    def test_counts_stall_but_respects_floor(self) -> None:
        import bo as bo_mod
        p = bo_mod._PhasePatience(2, floor_iters=5)
        self.assertFalse(p.update("cei", 1.0, 0))   # phase established (reset)
        self.assertFalse(p.update("cei", 1.0, 1))   # count=1
        self.assertFalse(p.update("cei", 1.0, 2))   # count=2 but below the floor (3<5)
        self.assertFalse(p.update("cei", 1.0, 3))   # count=3 but below the floor (4<5)
        self.assertTrue(p.update("cei", 1.0, 4))    # count=4 and the floor is reached (5>=5) -> stop

    def test_improvement_resets_counter(self) -> None:
        import bo as bo_mod
        p = bo_mod._PhasePatience(2, floor_iters=1)
        self.assertFalse(p.update("cei", 10.0, 0))
        self.assertFalse(p.update("cei", 10.0, 1))  # count=1
        self.assertFalse(p.update("cei", 9.0, 2))   # improvement -> reset
        self.assertFalse(p.update("cei", 9.0, 3))   # count=1
        self.assertTrue(p.update("cei", 9.0, 4))    # count=2 -> stop

    def test_phase_switch_resets_counter(self) -> None:
        import bo as bo_mod
        p = bo_mod._PhasePatience(2, floor_iters=1)
        self.assertFalse(p.update("bootstrap", 0.5, 0))
        self.assertFalse(p.update("bootstrap", 0.5, 1))  # count=1
        self.assertFalse(p.update("cei", 300.0, 2))      # phase switch -> reset (a different axis as well)
        self.assertFalse(p.update("cei", 300.0, 3))      # count=1
        self.assertTrue(p.update("cei", 300.0, 4))       # count=2 -> stop


class TestPatienceIntegration(unittest.TestCase):
    """12.5(c) Early stopping in run_bo (floor n_iter/3; recorded in gen_log)."""

    class _ConstantFeasibleMock:
        """The same feasible observation at every point (a landscape where improvement can never happen)."""

        def evaluate_topology(self, topology, x_list):
            return [Metrics(specific_energy=300.0, purity=0.96, recovery=0.92)
                    for _ in x_list]

        def evaluate_detailed(self, topology, x):
            return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])

    def test_early_stop_after_floor(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"n_iter": 12, "patience": 2})
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, self._ConstantFeasibleMock(), seed=71)
        # floor = 12//3 = 4. Even once count reaches 2 the run continues to the floor, and stops there
        self.assertEqual(len(gen_log), 4, f"floor+patience should give 4 iterations: {len(gen_log)}")
        self.assertIn("early_stop", gen_log[-1])

    def test_patience_off_runs_full(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"n_iter": 3, "patience": 0})
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, self._ConstantFeasibleMock(), seed=72)
        self.assertEqual(len(gen_log), 3)
        self.assertNotIn("early_stop", gen_log[-1])
        # the timing field "t" (elapsed seconds) is present and monotonically non-decreasing
        ts = [g["t"] for g in gen_log]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])), ts)
        # timing breakdown: t_fit / t_acq / t_eval (non-negative) on every iteration, t_init_eval on the first
        for g in gen_log:
            for k in ("t_fit", "t_acq", "t_eval"):
                self.assertIn(k, g)
                self.assertGreaterEqual(g[k], 0.0)
        self.assertIn("t_init_eval", gen_log[0])
        self.assertGreaterEqual(gen_log[0]["t_init_eval"], 0.0)


class TestCostObjective(unittest.TestCase):
    """12.2: with objective=minimize_cost, the BO objective and best selection move to the cost axis."""

    @staticmethod
    def _cost_case() -> dict:
        case = _make_case()
        case["feed"] = {"flowbase": "MASS", "basis": "MOLE-FRAC",
                        "totflow": 80307.0, "co2_frac": 0.15}   # kmol/h (molar interpretation)
        case["optimization_targets"]["objective"] = "minimize_cost"
        return case

    def test_best_is_min_cost_not_min_energy(self) -> None:
        """Even when energy is identical at every point, the observation with the lowest cost (proportional to membrane area) is chosen as best."""
        from bo import run_bo

        class _FlatEnergyMock:
            """Constant energy, always feasible. A landscape where cost is set by membrane area alone."""

            def __init__(self) -> None:
                self.area_sums: list[float] = []

            def evaluate_topology(self, topology, x_list):
                out = []
                for x in x_list:
                    # cont vars = [M1_area, M1_pp, M2_area, M2_pp]
                    # (the seed, without membrane_model. COMP1 is fixed at blower pressure = not a variable)
                    self.area_sums.append(float(x[0]) + float(x[2]))
                    out.append(Metrics(specific_energy=300.0, purity=0.96, recovery=0.92))
                return out

            def evaluate_detailed(self, topology, x):
                return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])

        ss = _load_seed()
        mock = _FlatEnergyMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, self._cost_case(), mock, seed=61)
        # every point is feasible -> best is the minimum cost = the observation with the smallest total membrane area
        best_sum = best[0] + best[2]
        self.assertAlmostEqual(best_sum, min(mock.area_sums), places=6)

    def test_energy_mode_unaffected(self) -> None:
        """With objective unspecified (energy) the run completes as before (regression)."""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()   # no objective
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=62)
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])


class TestXAlignmentWithPruning(unittest.TestCase):
    """The continuous x is narrowed to the variables of the concrete topology (after pruning) before reaching the evaluator.

    Before the fix, "x over all template dimensions" and "the continuous_variables of the pruned
    topology" were zipped positionally, so pruning a unit in the middle wrote the preceding unit's
    value into the following one -- an alignment bug (fixed in topology.x_for_topology).
    """

    def test_evaluate_batch_filters_x_to_topology(self) -> None:
        import numpy as np

        import bo as bo_mod
        ss = make_bypass_toggle_ss()
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(bin_vars), 2)
        self.assertEqual(len(cont_vars), 4)

        seen: list[tuple[int, int]] = []  # (number of units in the topology, dimension of the x received)

        class _DimRecorder(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                for x in x_list:
                    seen.append((len(topology["units"]), len(x)))
                return super().evaluate_topology(topology, x_list)

        # columns: [q_1, q_2 | MEMB1_area, MEMB1_p_perm, MEMB2_area, MEMB2_p_perm]
        x_np = np.array([
            [1.0, 0.0, 200000.0, 0.5, 300000.0, 0.4],  # 2-stage -> x has 4 variables
            [0.0, 1.0, 200000.0, 0.5, 300000.0, 0.4],  # MEMB2 pruned -> x has 2 variables
        ])
        o, e, p, r, v, n = bo_mod._evaluate_batch_multi(
            x_np, ss, bin_vars, cont_vars, 2, _DimRecorder(), retry_bad=0
        )
        self.assertIn((2, 4), seen, f"wrong dimension on the 2-stage side: {seen}")
        self.assertIn((1, 2), seen, f"wrong dimension on the pruned side: {seen}")
        self.assertTrue(all(v))


class TestLogScaleInputs(unittest.TestCase):
    """12.5(b) Log scaling (search positive continuous variables whose bounds ratio exceeds 50 in log space)."""

    def test_mask_on_seed_with_lee_bounds_is_all_false(self) -> None:
        """With ver3's Lee-consistent bounds (area ratio 15, p_permeate ratio 9.9) nothing in the seed is log-scaled.

        The 12.5(b) implementation remains as insurance for cases with a bounds ratio above 50
        (bounds_override, or wider ranges in the future). The positive case is covered by the
        synthetic test below.
        """
        import bo as bo_mod
        cont_vars = T.continuous_variables(_load_seed())
        mask = bo_mod._log_scale_mask(0, cont_vars, enabled=True)
        self.assertFalse(mask.any())

    def test_mask_excludes_narrow_nonpositive_and_binary(self) -> None:
        """Continuous variables with a ratio <=50 or a lower bound <=0, and binary dimensions, are excluded."""
        import bo as bo_mod
        cont_vars = [
            {"name": "narrow", "bounds": [1.0, 10.0]},    # ratio 10 <= 50 -> excluded
            {"name": "nonpos", "bounds": [0.0, 100.0]},   # lower bound 0 -> excluded
            {"name": "wide",   "bounds": [0.01, 10.0]},   # ratio 1000 -> included
        ]
        mask = bo_mod._log_scale_mask(2, cont_vars, enabled=True)  # 2 binaries
        self.assertEqual(mask.tolist(), [False, False, False, False, True])

    def test_mask_disabled_is_all_false(self) -> None:
        """Equivalent to log_scale_inputs: off (enabled=False): all False, i.e. linear scale."""
        import bo as bo_mod
        cont_vars = T.continuous_variables(_load_seed())
        mask = bo_mod._log_scale_mask(0, cont_vars, enabled=False)
        self.assertFalse(mask.any())

    def test_to_eval_space_exps_only_masked_columns(self) -> None:
        """_to_eval_space exponentiates only the masked columns; binary and linear columns are unchanged."""
        import math

        import bo as bo_mod
        import numpy as np
        mask = np.array([False, True, False])
        x = np.array([[1.0, math.log(20000.0), 0.5]])
        out = bo_mod._to_eval_space(x, mask)
        self.assertAlmostEqual(out[0, 0], 1.0)
        self.assertAlmostEqual(out[0, 1], 20000.0, places=6)
        self.assertAlmostEqual(out[0, 2], 0.5)
        # the source array is not modified
        self.assertAlmostEqual(x[0, 1], math.log(20000.0))

    def test_best_and_evaluated_x_within_original_bounds(self) -> None:
        """Even with log enabled (the default), the x the evaluator receives and best are on the original real bounds scale."""
        from bo import run_bo

        cont_vars = T.continuous_variables(_load_seed())
        bounds = [cv["bounds"] for cv in cont_vars]

        class _RangeCheckMock(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                for x in x_list:
                    for v, (lo, hi) in zip(x, bounds):
                        assert lo - 1e-9 <= float(v) <= hi + 1e-9, \
                            f"eval x={v} is outside bounds [{lo}, {hi}] (suspect it was passed through still in log space)"
                return super().evaluate_topology(topology, x_list)

        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _RangeCheckMock(), seed=31)
        for v, (lo, hi) in zip(best, bounds):
            self.assertGreaterEqual(v, lo - 1e-9)
            self.assertLessEqual(v, hi + 1e-9)

    def test_log_scale_off_restores_legacy_and_completes(self) -> None:
        """With log_scale_inputs: off (including YAML's bool False) the run stays on linear scale and completes."""
        from bo import run_bo
        ss = _load_seed()
        for off_value in ("off", False):
            case = _make_case({"log_scale_inputs": off_value})
            with redirect_stdout(io.StringIO()):
                best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=32)
            self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
            cont_vars = T.continuous_variables(ss)
            for v, cv in zip(best, cont_vars):
                lo, hi = cv["bounds"]
                self.assertGreaterEqual(v, lo - 1e-9)
                self.assertLessEqual(v, hi + 1e-9)


if __name__ == "__main__":
    unittest.main()
