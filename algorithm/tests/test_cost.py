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
    "totflow": 80307.0,   # kmol/h（Lee の 500 Nm3/s。TOTFLOW は実測上モル流量・smoke_feed）
    "co2_frac": 0.15,
}


def _case(economics: dict | None = None) -> dict:
    return {"feed": dict(_FEED), "economics": economics or {}}


class TestFeedCO2MassFlow(unittest.TestCase):

    def test_molar_totflow_conversion(self):
        # TOTFLOW はモル流量 [kmol/h]（2026-07-10 smoke_feed 実測）:
        # CO2 t/h = totflow × co2_frac × 44/1000
        expected = 80307.0 * 0.15 * 44.0 / 1000.0   # ≈ 530.03 t/h
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
        # CAPEX = C·|WNET|（電気動力そのまま。η では割らない——Lee Fig.3/4 再現で確認）
        # C_TCC = 50*5e5 + 1341*50000 + 670*30000 = 112,150,000 $
        # 年間 CAPEX = 0.2*1.6*C_TCC = 35,888,000 $/y
        # M_CO2 = 0.9 * 80307*0.15*0.044 = 477.02 t/h → 年間 3,551,916 t
        # capex/t = 10.104、opex/t = 300*0.04 = 12.0 → 合計 ≈ 22.10 $/t
        cost = cost_per_tco2(self.METRICS, self.AREAS, _case())
        self.assertAlmostEqual(cost, 22.10, delta=0.01)

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
        # 追加 CAPEX/t = 0.32 * 500*10000 / (477.02*7446) ≈ 0.4505 $/t
        self.assertAlmostEqual(cost - base, 0.4505, delta=0.001)

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
        # 膜項 = 0.32 * 50*5e5 / (477.02*7446) ≈ 2.252 $/t
        self.assertAlmostEqual(cost_default - cost_free, 2.252, delta=0.005)


class TestLeeReproduction(unittest.TestCase):
    """Lee et al. (2018) Fig.3/Fig.4 の4設計で論文記載の C_cap を再現できること。

    各図に記載の膜面積・機器別動力・製品流量（61.5 Nm³/s, 95% CO2）をそのまま入力し、
    論文の C_cap [$/tCO2] と比較する。我々は HX コストを意図的に省略しているため、
    期待値は「論文より 1〜3% 低い」（4設計の実測: −1.9〜−2.2%）。
    η で割る誤実装（+8〜10%）へ退行しないための回帰テスト。
    """

    # Lee の feed: 500 Nm³/s, 13 mol% CO2（0°C, 22.414 L/mol）
    # TOTFLOW はモル流量 [kmol/h]（smoke_feed 実測）: 22,307.5 mol/s × 3.6 = 80,307 kmol/h
    _LEE_FEED = {
        "flowbase": "MASS", "basis": "MOLE-FRAC",
        "totflow": 500.0 / 0.022414 * 3.6,   # ≈ 80,307 kmol/h
        "co2_frac": 0.13,
    }
    _RECOVERY = (61.5 * 0.95) / (500.0 * 0.13)   # 0.8988（全4設計共通）

    _DESIGNS = [
        # (名称, 論文 C_cap, 面積 {unit: m2}, 動力 {block: kW}（負=膨張機）)
        ("Fig3a 2段 no-recycle", 42.5,
         {"MEMB1": 1025913.0, "MEMB2": 100789.0},
         {"COMP1": 91500.0, "VP1": 50500.0, "COMP2": 19000.0,
          "VP2": 29700.0, "EXP1": -33000.0, "EXP2": -3500.0}),
        ("Fig3b 3段 no-recycle", 42.1,
         {"MEMB1": 1338588.0, "MEMB2": 486908.0, "MEMB3": 351678.0},
         {"COMP1": 79700.0, "VP1": 40500.0, "COMP2": 20200.0,
          "VP2": 4900.0, "VP3": 17100.0, "EXP1": -29400.0, "EXP2": -2800.0}),
        ("Fig4a 2段 recycle", 36.3,
         {"MEMB1": 1125888.0, "MEMB2": 97648.0},
         {"COMP1": 81100.0, "VP1": 51100.0, "COMP2": 23300.0,
          "VP2": 4700.0, "EXP1": -31400.0, "EXP2": -1300.0}),
        ("Fig4b 3段 recycle", 36.6,
         {"MEMB1": 1185158.0, "MEMB2": 393146.0, "MEMB3": 72772.0},
         {"COMP1": 70200.0, "VP1": 49700.0, "COMP2": 7700.0,
          "VP2": 2500.0, "VP3": 13800.0, "EXP1": -30100.0}),
    ]

    def test_all_four_designs_within_hx_margin(self):
        case = {"feed": dict(self._LEE_FEED), "economics": {}}   # Lee Table 1 既定
        m_co2 = self._RECOVERY * feed_co2_t_per_h(self._LEE_FEED)
        for name, paper, areas, breakdown in self._DESIGNS:
            spec_e = sum(breakdown.values()) / m_co2
            m = Metrics(specific_energy=spec_e, purity=0.95,
                        recovery=self._RECOVERY, energy_breakdown=breakdown)
            cost = cost_per_tco2(m, areas, case)
            rel = (cost - paper) / paper
            self.assertGreater(rel, -0.03, f"{name}: {cost:.2f} vs 論文 {paper}（低すぎ）")
            self.assertLess(rel, 0.0, f"{name}: {cost:.2f} vs 論文 {paper}"
                                      f"（HX 省略なのに論文以上＝η 誤除算の退行疑い）")


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
