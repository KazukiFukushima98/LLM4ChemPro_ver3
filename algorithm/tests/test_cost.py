"""Unit tests for the evaluator's cost objective (ver3 12.2) (no Aspen required).

Verifies cost_per_tco2 / feed_co2_t_per_h / membrane_areas_from_x for consistency with
Lee et al., J. Membr. Sci. 563 (2018) Table 1 / Eq.15-16.

Run:
    uv run python -m unittest tests.test_cost
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from evaluator import (  # noqa: E402
    BAD_VALUE,
    Metrics,
    cost_per_tco2,
    feed_co2_t_per_h,
    membrane_areas_from_x,
)

_FEED = {
    "flowbase": "MASS",
    "basis": "MOLE-FRAC",
    "totflow": 80307.0,   # kmol/h (Lee's 500 Nm3/s. TOTFLOW is a molar flow as measured; smoke_feed)
    "co2_frac": 0.15,
}


def _case(economics: dict | None = None) -> dict:
    return {"feed": dict(_FEED), "economics": economics or {}}


class TestFeedCO2MassFlow(unittest.TestCase):

    def test_molar_totflow_conversion(self):
        # TOTFLOW is a molar flow [kmol/h] (measured with smoke_feed, 2026-07-10):
        # CO2 t/h = totflow x co2_frac x 44/1000
        expected = 80307.0 * 0.15 * 44.0 / 1000.0   # ~= 530.03 t/h
        self.assertAlmostEqual(feed_co2_t_per_h(_FEED), expected, places=6)

    def test_rejects_unsupported_basis(self):
        with self.assertRaises(ValueError):
            feed_co2_t_per_h({**_FEED, "flowbase": "MOLE"})
        with self.assertRaises(ValueError):
            feed_co2_t_per_h({**_FEED, "basis": "MASS-FRAC"})


class TestCostPerTCO2(unittest.TestCase):
    """Agreement with hand-computed values (Lee Table 1 defaults; HX omitted)."""

    METRICS = Metrics(
        specific_energy=300.0, purity=0.96, recovery=0.90,
        energy_breakdown={"VP1": 50000.0, "COMP1": 30000.0},   # kW
    )
    AREAS = {"MEMB1": 500000.0}   # m2

    def test_matches_hand_computed_value(self):
        # CAPEX = C*|WNET| (the electrical power as-is; not divided by eta -- confirmed by reproducing Lee Fig.3/4)
        # C_TCC = 50*5e5 + 1341*50000 + 670*30000 = 112,150,000 $
        # annual CAPEX = 0.2*1.6*C_TCC = 35,888,000 $/y
        # M_CO2 = 0.9 * 80307*0.15*0.044 = 477.02 t/h -> 3,551,916 t/y
        # capex/t = 10.104, opex/t = 300*0.04 = 12.0 -> total ~= 22.10 $/t
        cost = cost_per_tco2(self.METRICS, self.AREAS, _case())
        self.assertAlmostEqual(cost, 22.10, delta=0.01)

    def test_opex_term_equals_spec_e_times_ce(self):
        # with neither membranes nor pressure equipment, CAPEX=0 -> cost = E*Ce
        m = Metrics(specific_energy=300.0, purity=0.96, recovery=0.9)
        cost = cost_per_tco2(m, {}, _case())
        self.assertAlmostEqual(cost, 300.0 * 0.04, places=9)

    def test_negative_wnet_is_costed_as_expander(self):
        """WNET<0 (expander, i.e. power recovery) is charged to CAPEX at expander_cost=500 $/kW."""
        base = cost_per_tco2(self.METRICS, self.AREAS, _case())
        m_exp = Metrics(
            specific_energy=300.0, purity=0.96, recovery=0.90,
            energy_breakdown={**self.METRICS.energy_breakdown, "EXP1": -10000.0},
        )
        cost = cost_per_tco2(m_exp, self.AREAS, _case())
        # additional CAPEX/t = 0.32 * 500*10000 / (477.02*7446) ~= 0.4505 $/t
        self.assertAlmostEqual(cost - base, 0.4505, delta=0.001)

    def test_bad_metrics_returns_bad(self):
        self.assertEqual(cost_per_tco2(Metrics.bad(), self.AREAS, _case()), BAD_VALUE)

    def test_zero_recovery_returns_bad(self):
        m = Metrics(specific_energy=300.0, purity=0.96, recovery=0.0)
        self.assertEqual(cost_per_tco2(m, self.AREAS, _case()), BAD_VALUE)

    def test_economics_override(self):
        """economics: in case.yaml takes precedence over the defaults (a zero membrane price removes the membrane term)."""
        cost_default = cost_per_tco2(self.METRICS, self.AREAS, _case())
        cost_free    = cost_per_tco2(self.METRICS, self.AREAS,
                                     _case({"membrane_cost": 0.0}))
        # membrane term = 0.32 * 50*5e5 / (477.02*7446) ~= 2.252 $/t
        self.assertAlmostEqual(cost_default - cost_free, 2.252, delta=0.005)


class TestHxCost(unittest.TestCase):
    """Folding in the HX (automatic cooler) cost (2026-07-16; Lee Sec. 2.3 / Eq.5-6, Eq.16 C_hx)."""

    def test_hx_area_hand_computed(self):
        """A = |Q|/(U*LMTD). Q=-1000kW, T_in=150degC -> LMTD=(125-15)/ln(125/15)=51.88 K."""
        from simulator import hx_area_m2_from_duty
        area = hx_area_m2_from_duty(-1000.0, 150.0)
        self.assertAlmostEqual(area, 1_000_000.0 / (132.5 * 51.8803), delta=0.05)

    def test_hx_area_equal_end_temps_uses_dt(self):
        """When the end temperature differences are equal (T_in=40degC -> dt1=dt2=15K), LMTD=15K."""
        from simulator import hx_area_m2_from_duty
        area = hx_area_m2_from_duty(-100.0, 40.0)
        self.assertAlmostEqual(area, 100_000.0 / (132.5 * 15.0), delta=0.01)

    def test_hx_area_zero_when_not_cooling(self):
        """A heating duty, a gas inlet at or below 35degC, and missing data all give area 0 (e.g. a cool blower outlet)."""
        from simulator import hx_area_m2_from_duty
        self.assertEqual(hx_area_m2_from_duty(500.0, 150.0), 0.0)   # heating
        self.assertEqual(hx_area_m2_from_duty(-500.0, 30.0), 0.0)   # inlet at or below 35degC
        self.assertEqual(hx_area_m2_from_duty(None, 150.0), 0.0)    # duty missing
        self.assertEqual(hx_area_m2_from_duty(-500.0, None), 0.0)   # temperature missing

    def test_cost_includes_hx_term(self):
        """hx_area_m2 enters CAPEX at C_hx=300 $/m2.
        delta = 0.2*1.6*300*10000 / (477.02*7446) ~= 0.2703 $/t"""
        base = Metrics(specific_energy=300.0, purity=0.96, recovery=0.90,
                       energy_breakdown={"VP1": 50000.0})
        with_hx = Metrics(specific_energy=300.0, purity=0.96, recovery=0.90,
                          energy_breakdown={"VP1": 50000.0}, hx_area_m2=10000.0)
        areas = {"MEMB1": 500000.0}
        delta = cost_per_tco2(with_hx, areas, _case()) - cost_per_tco2(base, areas, _case())
        self.assertAlmostEqual(delta, 0.2703, delta=0.001)

    def test_hx_default_zero_backward_compatible(self):
        """With hx_area_m2 unspecified (equivalent to the old Metrics), the cost is unchanged."""
        m = Metrics(specific_energy=300.0, purity=0.96, recovery=0.90,
                    energy_breakdown={"VP1": 50000.0})
        self.assertEqual(m.hx_area_m2, 0.0)


class TestLeeReproduction(unittest.TestCase):
    """The four designs of Lee et al. (2018) Fig.3/Fig.4 must reproduce the C_cap reported in the paper.

    The membrane areas, per-equipment power and product flow (61.5 Nm3/s, 95% CO2) listed in each
    figure are fed in as-is and compared against the paper's C_cap [$/tCO2]. Because we deliberately
    omit the HX cost, the expected value is "1-3% below the paper" (measured over the four designs:
    -1.9 to -2.2%). This is a regression test against relapsing into the mis-implementation that
    divides by eta (+8-10%).
    """

    # Lee's feed: 500 Nm3/s, 13 mol% CO2 (0degC, 22.414 L/mol)
    # TOTFLOW is a molar flow [kmol/h] (measured with smoke_feed): 22,307.5 mol/s x 3.6 = 80,307 kmol/h
    _LEE_FEED = {
        "flowbase": "MASS", "basis": "MOLE-FRAC",
        "totflow": 500.0 / 0.022414 * 3.6,   # ~= 80,307 kmol/h
        "co2_frac": 0.13,
    }
    _RECOVERY = (61.5 * 0.95) / (500.0 * 0.13)   # 0.8988 (common to all four designs)

    _DESIGNS = [
        # (name, paper C_cap, areas {unit: m2}, power {block: kW} (negative = expander))
        ("Fig3a 2-stage no-recycle", 42.5,
         {"MEMB1": 1025913.0, "MEMB2": 100789.0},
         {"COMP1": 91500.0, "VP1": 50500.0, "COMP2": 19000.0,
          "VP2": 29700.0, "EXP1": -33000.0, "EXP2": -3500.0}),
        ("Fig3b 3-stage no-recycle", 42.1,
         {"MEMB1": 1338588.0, "MEMB2": 486908.0, "MEMB3": 351678.0},
         {"COMP1": 79700.0, "VP1": 40500.0, "COMP2": 20200.0,
          "VP2": 4900.0, "VP3": 17100.0, "EXP1": -29400.0, "EXP2": -2800.0}),
        ("Fig4a 2-stage recycle", 36.3,
         {"MEMB1": 1125888.0, "MEMB2": 97648.0},
         {"COMP1": 81100.0, "VP1": 51100.0, "COMP2": 23300.0,
          "VP2": 4700.0, "EXP1": -31400.0, "EXP2": -1300.0}),
        ("Fig4b 3-stage recycle", 36.6,
         {"MEMB1": 1185158.0, "MEMB2": 393146.0, "MEMB3": 72772.0},
         {"COMP1": 70200.0, "VP1": 49700.0, "COMP2": 7700.0,
          "VP2": 2500.0, "VP3": 13800.0, "EXP1": -30100.0}),
    ]

    def test_all_four_designs_within_hx_margin(self):
        case = {"feed": dict(self._LEE_FEED), "economics": {}}   # Lee Table 1 defaults
        m_co2 = self._RECOVERY * feed_co2_t_per_h(self._LEE_FEED)
        for name, paper, areas, breakdown in self._DESIGNS:
            spec_e = sum(breakdown.values()) / m_co2
            m = Metrics(specific_energy=spec_e, purity=0.95,
                        recovery=self._RECOVERY, energy_breakdown=breakdown)
            cost = cost_per_tco2(m, areas, case)
            rel = (cost - paper) / paper
            self.assertGreater(rel, -0.03, f"{name}: {cost:.2f} vs paper {paper} (too low)")
            self.assertLess(rel, 0.0, f"{name}: {cost:.2f} vs paper {paper}"
                                      f" (above the paper despite omitting HX = suspect a regression to dividing by eta)")


class TestMembraneAreasFromX(unittest.TestCase):

    _CVS = [
        {"name": "MEMB1_area",   "unit_param": ["MEMB1", "area"]},
        {"name": "MEMB1_p_perm", "unit_param": ["MEMB1", "p_permeate"]},
        {"name": "MEMB2_area",   "unit_param": ["MEMB2", "area"]},
        {"name": "MEMB2_p_perm", "unit_param": ["MEMB2", "p_permeate"]},
    ]

    def test_extracts_areas_for_topology_units_only(self):
        """The area of a pruned membrane (MEMB2) is not counted in CAPEX."""
        topo = {"units": {"MEMB1": {"type": "MEMB", "params": {"area": 1.0}}}}
        areas = membrane_areas_from_x([111.0, 0.5, 222.0, 0.4], self._CVS, topo)
        self.assertEqual(areas, {"MEMB1": 111.0})

    def test_x_value_overrides_params_fallback(self):
        topo = {
            "units": {
                "MEMB1": {"type": "MEMB", "params": {"area": 999.0}},
                "MEMB2": {"type": "MEMB", "params": {"area": 888.0}},
            }
        }
        areas = membrane_areas_from_x([111.0, 0.5, 222.0, 0.4], self._CVS, topo)
        self.assertEqual(areas, {"MEMB1": 111.0, "MEMB2": 222.0})

    def test_params_fallback_when_area_not_in_x(self):
        """A membrane whose area variable is absent from x uses the value in params (defensive path)."""
        topo = {"units": {"MEMB1": {"type": "MEMB", "params": {"area": 777.0}}}}
        areas = membrane_areas_from_x([], [], topo)
        self.assertEqual(areas, {"MEMB1": 777.0})


if __name__ == "__main__":
    unittest.main()
