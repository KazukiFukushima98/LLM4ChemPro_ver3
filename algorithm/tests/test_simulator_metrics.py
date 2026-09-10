"""Unit tests for the mass-balance guard in _extract_metrics.

A recycle tear can report "converged" over headless COM and still land on a
non-physical point (recovery > 1; e.g. product CO2 188.5 > feed 150, i.e. 125.67%
recovery). These tests check that the
cross-check guard added to _extract_metrics rejects recovery >
recovery_physical_max as BAD.

simulator imports pythoncom / aspen_builder (which depend on pywin32), so the
tests are skipped where those imports fail. Real Aspen is not needed: _safe is
replaced with a mock that injects values without reading the Aspen tree.

Run:
    uv run python -m unittest algorithm.tests.test_simulator_metrics
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from evaluator import BAD_VALUE, Metrics  # noqa: E402

try:
    from simulator import AspenEvaluator, build_unit_params  # noqa: E402
    _SIM_IMPORT_ERR = None
except Exception as e:  # pragma: no cover - environment without pywin32
    AspenEvaluator = None
    build_unit_params = None
    _SIM_IMPORT_ERR = e


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


@unittest.skipUnless(
    AspenEvaluator is not None,
    f"cannot import simulator (e.g. pywin32 missing): {_SIM_IMPORT_ERR}",
)
class TestMassBalanceGuard(unittest.TestCase):
    """Exercise the recovery cross-check guard of _extract_metrics with a _safe mock, without real Aspen."""

    PURITY = 0.9
    V0_MF = 150.0   # feed CO2 flow rate (fixed)
    POWER = 10.0    # WNET of each energy block

    def _evaluator(self, prod_mf: float) -> AspenEvaluator:
        """Build an AspenEvaluator with case={} and replace _safe with a mock that returns a value per path.

        It distinguishes the 4 kinds of path that _extract_metrics reads:
          MOLEFRAC           -> purity
          WNET               -> power
          MOLEFLOW + "\\V0\\" -> feed CO2 flow rate
          MOLEFLOW (other)    -> product CO2 flow rate
        """
        ev = AspenEvaluator({}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return self.PURITY
            if "WNET" in path:
                return self.POWER
            if "MOLEFLOW" in path:
                return self.V0_MF if "\\V0\\" in path else prod_mf
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        return ev

    def _extract(self, prod_mf: float) -> Metrics:
        ev = self._evaluator(prod_mf)
        return ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=["MEMB1"])

    def test_recovery_above_one_is_bad(self):
        """Recovery 1.257 (a non-physical point) -> rejected as BAD."""
        m = self._extract(prod_mf=188.5)  # 188.5 / 150 = 1.2567
        self.assertTrue(_is_bad(m), f"non-physical point (recovery>1) not marked BAD: {m}")

    def test_recovery_below_one_is_ok(self):
        """Recovery 0.773 (the normal range) -> accepted."""
        m = self._extract(prod_mf=115.9)  # 115.9 / 150 = 0.7727
        self.assertFalse(_is_bad(m), f"valid solution wrongly marked BAD: {m}")
        self.assertAlmostEqual(m.recovery, 115.9 / 150.0, places=4)
        self.assertAlmostEqual(m.purity, self.PURITY, places=6)

    def test_recovery_exactly_one_is_ok(self):
        """Recovery 1.0 (the boundary, full recovery) -> accepted, being at or below the default max=1.02."""
        m = self._extract(prod_mf=150.0)  # 150 / 150 = 1.0
        self.assertFalse(_is_bad(m), f"recovery=1.0 wrongly marked BAD: {m}")
        self.assertAlmostEqual(m.recovery, 1.0, places=6)

    def test_threshold_is_configurable(self):
        """Lowering recovery_physical_max in the case moves the threshold."""
        ev = AspenEvaluator({"recovery_physical_max": 0.9}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return self.PURITY
            if "WNET" in path:
                return self.POWER
            if "MOLEFLOW" in path:
                return self.V0_MF if "\\V0\\" in path else 142.5  # 142.5/150 = 0.95 > 0.9
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        m = ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=["MEMB1"])
        self.assertTrue(_is_bad(m), f"0.95 not marked BAD under max=0.9: {m}")


@unittest.skipUnless(
    AspenEvaluator is not None,
    f"cannot import simulator (e.g. pywin32 missing): {_SIM_IMPORT_ERR}",
)
class TestEnergyGuard(unittest.TestCase):
    """Energy cross-check guard: reject the spurious zero-energy solution that arises when every WNET read fails (sum 0)."""

    def _extract(self, wnet: float, energy_blocks: list[str]) -> Metrics:
        ev = AspenEvaluator({}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return 0.9
            if "WNET" in path:
                return wnet
            if "MOLEFLOW" in path:
                return 150.0 if "\\V0\\" in path else 115.9
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        return ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=energy_blocks)

    def test_zero_wnet_with_blocks_is_bad(self):
        """WNET sums to 0 despite VPs being present -> rejected as BAD (a spurious spec_e=0)."""
        m = self._extract(wnet=0.0, energy_blocks=["VP1", "VP2"])
        self.assertTrue(_is_bad(m), f"spurious zero-energy solution not marked BAD: {m}")

    def test_positive_wnet_with_blocks_is_ok(self):
        m = self._extract(wnet=10.0, energy_blocks=["VP1"])
        self.assertFalse(_is_bad(m), f"valid solution wrongly marked BAD: {m}")
        self.assertGreater(m.specific_energy, 0.0)

    def test_no_energy_blocks_zero_energy_allowed(self):
        """An empty energy_blocks (a structure with no VP/COMP) may have spec_e=0 (a separate penalty covers it)."""
        m = self._extract(wnet=0.0, energy_blocks=[])
        self.assertFalse(_is_bad(m), f"zero energy with no blocks wrongly marked BAD: {m}")
        self.assertEqual(m.specific_energy, 0.0)


@unittest.skipUnless(
    build_unit_params is not None,
    f"cannot import simulator (e.g. pywin32 missing): {_SIM_IMPORT_ERR}",
)
class TestBuildUnitParams(unittest.TestCase):
    """Assembling unit_params from x (10.1: tie expansion and Robeson-derived N2). A pure function, no Aspen needed."""

    _TOPO2 = {"units": {"MEMB1": {}, "MEMB2": {}}}

    def test_plain_mapping_without_membrane_model(self):
        cont_vars = [
            {"name": "MEMB1_area",   "unit_param": ["MEMB1", "area"]},
            {"name": "MEMB1_p_perm", "unit_param": ["MEMB1", "p_permeate"]},
        ]
        up = build_unit_params([20000.0, 0.2], cont_vars, self._TOPO2)
        self.assertEqual(up, {"MEMB1": {"area": 20000.0, "p_permeate": 0.2}})

    def test_tie_variable_expands_to_all_membranes_with_n2(self):
        """The shared "MEMB*" permeance expands to every MEMB, and N2 is derived via Robeson."""
        from unit_registry import robeson_alpha
        cont_vars = [
            {"name": "MEMB_perm",  "unit_param": ["MEMB*", "permeance_CO2"]},
            {"name": "MEMB1_area", "unit_param": ["MEMB1", "area"]},
            {"name": "MEMB2_area", "unit_param": ["MEMB2", "area"]},
        ]
        p = 10.82708  # ~= 4000 GPU
        up = build_unit_params([p, 500000.0, 300000.0], cont_vars, self._TOPO2,
                               membrane_model={})
        alpha = robeson_alpha(p, {})
        for u in ("MEMB1", "MEMB2"):
            self.assertAlmostEqual(up[u]["permeance_CO2"], p)
            self.assertAlmostEqual(up[u]["permeance_N2"], p / alpha, places=9)
        self.assertAlmostEqual(up["MEMB1"]["area"], 500000.0)
        self.assertAlmostEqual(up["MEMB2"]["area"], 300000.0)

    def test_tie_expands_only_to_surviving_membranes(self):
        """In a pruned topology, it expands only to the surviving MEMBs."""
        cont_vars = [
            {"name": "MEMB_perm",  "unit_param": ["MEMB*", "permeance_CO2"]},
            {"name": "MEMB1_area", "unit_param": ["MEMB1", "area"]},
        ]
        topo = {"units": {"MEMB1": {}}}
        up = build_unit_params([5.0, 200000.0], cont_vars, topo, membrane_model={})
        self.assertEqual(set(up), {"MEMB1"})
        self.assertIn("permeance_N2", up["MEMB1"])

    def test_per_unit_permeance_gets_n2(self):
        """N2 is derived for per-stage independent (tie=False) {unit}_perm as well."""
        cont_vars = [
            {"name": "MEMB1_perm", "unit_param": ["MEMB1", "permeance_CO2"]},
            {"name": "MEMB2_perm", "unit_param": ["MEMB2", "permeance_CO2"]},
        ]
        up = build_unit_params([2.70677, 13.53385], cont_vars, self._TOPO2,
                               membrane_model={"tie": False})
        # upper-bound alpha ~= 79.6 at 1000 GPU and ~= 45.6 at 5000 GPU -> N2 = CO2/alpha
        self.assertLess(up["MEMB1"]["permeance_N2"], up["MEMB1"]["permeance_CO2"])
        self.assertLess(up["MEMB2"]["permeance_N2"], up["MEMB2"]["permeance_CO2"])
        # the more permeable side has a lower alpha (a larger N2/CO2 ratio)
        ratio1 = up["MEMB1"]["permeance_N2"] / up["MEMB1"]["permeance_CO2"]
        ratio2 = up["MEMB2"]["permeance_N2"] / up["MEMB2"]["permeance_CO2"]
        self.assertLess(ratio1, ratio2)

    def test_no_n2_injection_without_membrane_model(self):
        """With membrane_model=None, permeance_CO2 is written through unchanged (backward compatible)."""
        cont_vars = [{"name": "MEMB1_perm", "unit_param": ["MEMB1", "permeance_CO2"]}]
        up = build_unit_params([2.70677], cont_vars, self._TOPO2, membrane_model=None)
        self.assertNotIn("permeance_N2", up["MEMB1"])


if __name__ == "__main__":
    unittest.main()
