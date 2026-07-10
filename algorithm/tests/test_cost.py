"""evaluator のコスト目的関数（ver3 12.2）の単体テスト（Aspen 不要）。

Lee et al., J. Membr. Sci. 563 (2018) Table 1 / Eq.15-16 に整合する
cost_per_tco2 / feed_co2_t_per_h / membrane_areas_from_x を検証する。

実行:
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
    "totflow": 2440000.0,   # kg/h（Lee の 500 Nm3/s 相当）
    "co2_frac": 0.15,
}


def _case(economics: dict | None = None) -> dict:
    return {"feed": dict(_FEED), "economics": economics or {}}


class TestFeedCO2MassFlow(unittest.TestCase):

    def test_mole_frac_to_mass_conversion(self):
        # 質量分率 = 0.15*44 / (0.15*44 + 0.85*28) = 6.6/30.4
        expected = 2440000.0 * (6.6 / 30.4) / 1000.0   # ≈ 529.74 t/h
        self.assertAlmostEqual(feed_co2_t_per_h(_FEED), expected, places=6)

    def test_rejects_unsupported_basis(self):
        with self.assertRaises(ValueError):
            feed_co2_t_per_h({**_FEED, "flowbase": "MOLE"})
        with self.assertRaises(ValueError):
            feed_co2_t_per_h({**_FEED, "basis": "MASS-FRAC"})


class TestCostPerTCO2(unittest.TestCase):
    """手計算値との一致（Lee Table 1 既定値・HX 省略）。"""

    METRICS = Metrics(
        specific_energy=300.0, purity=0.96, recovery=0.90,
        energy_breakdown={"VP1": 50000.0, "COMP1": 30000.0},   # kW
    )
    AREAS = {"MEMB1": 500000.0}   # m2

    def test_matches_hand_computed_value(self):
        # η=0.72（Aspen Compr 既定と統一・2026-07-10 決定）
        # C_TCC = 50*5e5 + 1341*50000/0.72 + 670*30000/0.72 = 146,041,667 $
        # 年間 CAPEX = 0.2*1.6*C_TCC = 46,733,333 $/y
        # M_CO2 = 0.9 * 2440*(6.6/30.4) = 476.763 t/h → 年間 3,549,978 t
        # capex/t = 13.164、opex/t = 300*0.04 = 12.0 → 合計 ≈ 25.16 $/t
        cost = cost_per_tco2(self.METRICS, self.AREAS, _case())
        self.assertAlmostEqual(cost, 25.16, delta=0.01)

    def test_opex_term_equals_spec_e_times_ce(self):
        # 膜も圧力機器も無ければ CAPEX=0 → cost = E*Ce
        m = Metrics(specific_energy=300.0, purity=0.96, recovery=0.9)
        cost = cost_per_tco2(m, {}, _case())
        self.assertAlmostEqual(cost, 300.0 * 0.04, places=9)

    def test_negative_wnet_is_costed_as_expander(self):
        """WNET<0（膨張機・電力回収）は expander_cost=500 $/kW で CAPEX 計上。"""
        base = cost_per_tco2(self.METRICS, self.AREAS, _case())
        m_exp = Metrics(
            specific_energy=300.0, purity=0.96, recovery=0.90,
            energy_breakdown={**self.METRICS.energy_breakdown, "EXP1": -10000.0},
        )
        cost = cost_per_tco2(m_exp, self.AREAS, _case())
        # 追加 CAPEX/t = 0.32 * 500*10000/0.72 / (476.763*7446) ≈ 0.6260 $/t
        self.assertAlmostEqual(cost - base, 0.6260, delta=0.001)

    def test_bad_metrics_returns_bad(self):
        self.assertEqual(cost_per_tco2(Metrics.bad(), self.AREAS, _case()), BAD_VALUE)

    def test_zero_recovery_returns_bad(self):
        m = Metrics(specific_energy=300.0, purity=0.96, recovery=0.0)
        self.assertEqual(cost_per_tco2(m, self.AREAS, _case()), BAD_VALUE)

    def test_economics_override(self):
        """case.yaml の economics: が既定より優先される（膜単価ゼロで膜項が消える）。"""
        cost_default = cost_per_tco2(self.METRICS, self.AREAS, _case())
        cost_free    = cost_per_tco2(self.METRICS, self.AREAS,
                                     _case({"membrane_cost": 0.0}))
        # 膜項 = 0.32 * 50*5e5 / (476.763*7446) ≈ 2.254 $/t
        self.assertAlmostEqual(cost_default - cost_free, 2.254, delta=0.005)


class TestMembraneAreasFromX(unittest.TestCase):

    _CVS = [
        {"name": "MEMB1_area",   "unit_param": ["MEMB1", "area"]},
        {"name": "MEMB1_p_perm", "unit_param": ["MEMB1", "p_permeate"]},
        {"name": "MEMB2_area",   "unit_param": ["MEMB2", "area"]},
        {"name": "MEMB2_p_perm", "unit_param": ["MEMB2", "p_permeate"]},
    ]

    def test_extracts_areas_for_topology_units_only(self):
        """pruned 膜（MEMB2）の面積は CAPEX に入れない。"""
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
        """area 変数が x に無い膜は params の値を使う（防御経路）。"""
        topo = {"units": {"MEMB1": {"type": "MEMB", "params": {"area": 777.0}}}}
        areas = membrane_areas_from_x([], [], topo)
        self.assertEqual(areas, {"MEMB1": 777.0})


if __name__ == "__main__":
    unittest.main()
