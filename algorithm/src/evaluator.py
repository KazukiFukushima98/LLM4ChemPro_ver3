"""評価の抽象境界（Evaluator Protocol）と結果型定義、コスト目的関数（12.2）。

ARCHITECTURE 5.2 の Evaluator Protocol と、Metrics / DetailedResult の dataclass を定義する。
実装は simulator.AspenEvaluator が行う。将来サロゲート（FMQA/BOQA 等）に差し替える際は
別実装を注入するだけでよい。テストはモック注入。

コスト目的関数（ver3 12.2）:
    Metrics + 膜面積 + case（economics/feed）から年間換算回収コスト [$/tCO2] を合成する
    純関数 cost_per_tco2 をここに置く（Metrics のすぐ隣＝評価境界の一部として。
    optimizer 側（ga/bo）と run_iteration の双方から同じ定義を使う）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


BAD_VALUE: float = 1.0e6

# economics: セクションの既定値（Lee et al., J. Membr. Sci. 563 (2018) Table 1）。
# case.yaml の economics: で上書き可能（人間管理）。
# 注: 熱交換器コスト（Chx=300 $/m2）はフローシートに冷却器を持たないため**省略**
# （2026-07-10 ユーザ決定）。膨張機は 12.4 で実装されるまで負の WNET が現れない。
ECONOMICS_DEFAULTS: dict[str, float] = {
    "membrane_cost": 50.0,          # $/m2（モジュール・スキッド込み）
    "compressor_cost": 670.0,       # $/kW
    "vacuum_pump_cost": 1341.0,     # $/kW
    "expander_cost": 500.0,         # $/kW（12.4 で膨張機実装後に効く）
    "installation_factor": 1.6,     # f_in（総 CAPEX に乗算）
    "capital_charge_rate": 0.2,     # /y（年間資本賦課率）
    "electricity_cost": 0.04,       # $/kWh
    "operating_hours": 7446.0,      # h/y（稼働率 85%）
    "pressure_unit_efficiency": 0.72,  # η。CAPEX 式 C·W/η（Lee Eq.16 の形）に使う。
                                       # シミュレーション（Aspen Compr の既定等エントロピー
                                       # 効率 0.72）と統一（2026-07-10 ユーザ決定。Lee は 0.8。
                                       # 実際の既定値は run24 前 smoke でノード読取確認）
    "penalty_weight": 1000.0,       # コスト目的の shortfall² 係数（Lee の r）
}


def feed_co2_t_per_h(feed: dict[str, Any]) -> float:
    """case.yaml.feed から feed 中の CO2 質量流量 [t/h] を計算する。

    現行の組合せ（FLOWBASE=MASS の totflow [kg/h] ＋ BASIS=MOLE-FRAC の co2_frac）
    のみ対応。二成分系（CO2/N2）を仮定し、モル分率→質量分率に換算する。
    """
    if str(feed.get("flowbase", "MASS")).upper() != "MASS" or \
       str(feed.get("basis", "MOLE-FRAC")).upper() != "MOLE-FRAC":
        raise ValueError(
            "feed_co2_t_per_h は FLOWBASE=MASS + BASIS=MOLE-FRAC のみ対応 "
            f"(got flowbase={feed.get('flowbase')!r}, basis={feed.get('basis')!r})"
        )
    x = float(feed["co2_frac"])            # CO2 モル分率
    mw_co2, mw_n2 = 44.0, 28.0             # simulator の spec_e 換算（44.0）と揃える
    mass_frac = x * mw_co2 / (x * mw_co2 + (1.0 - x) * mw_n2)
    return float(feed["totflow"]) * mass_frac / 1000.0   # kg/h → t/h


def _pressure_unit_cost_per_kw(block_name: str, wnet: float, econ: dict[str, float]) -> float:
    """energy_breakdown のブロック名と WNET 符号から単価 [$/kW] を引く。

    VP{n}=真空ポンプ、WNET<0=膨張機（タービン・電力回収）、それ以外（COMP 等）=圧縮機。
    """
    if wnet < 0.0:
        return float(econ["expander_cost"])
    if block_name.startswith("VP"):
        return float(econ["vacuum_pump_cost"])
    return float(econ["compressor_cost"])


def cost_per_tco2(
    metrics: Metrics,
    membrane_areas: dict[str, float],
    case: dict[str, Any],
) -> float:
    """年間換算 CO2 回収コスト [$/tCO2] を合成する（Lee 2018 Eq.15/16、HX 項は省略）。

        cost = (capital_charge · f_in · C_TCC) / (M_CO2 · t_op) + E · Ce
        C_TCC = Σ_memb Cm·A + Σ_blk C_unit(blk) · |WNET_blk| / η
        M_CO2 = recovery × feed CO2 質量流量 [t/h]
        E     = 比エネルギー [kWh/tCO2]（OPEX/tCO2 = E · Ce と等価）

    Parameters
    ----------
    metrics        : 有効な Metrics（bad は BAD_VALUE を返す）
    membrane_areas : {unit_name: area_m2}。**具体トポロジーに存在する膜だけ**を渡す
                     （pruned 膜の面積を CAPEX に入れない）
    case           : case.yaml の内容（economics / feed を参照）

    Returns
    -------
    float : $/tCO2。metrics が bad、または回収ゼロなら BAD_VALUE。
    """
    if metrics.specific_energy >= BAD_VALUE:
        return BAD_VALUE
    econ = {**ECONOMICS_DEFAULTS, **(case.get("economics") or {})}

    m_co2 = metrics.recovery * feed_co2_t_per_h(case["feed"])   # t/h
    if m_co2 <= 0.0:
        return BAD_VALUE

    eta = float(econ["pressure_unit_efficiency"])
    c_tcc = sum(float(econ["membrane_cost"]) * float(a) for a in membrane_areas.values())
    c_tcc += sum(
        _pressure_unit_cost_per_kw(blk, w, econ) * abs(float(w)) / eta
        for blk, w in metrics.energy_breakdown.items()
    )

    annual_capex = float(econ["capital_charge_rate"]) * float(econ["installation_factor"]) * c_tcc
    capex_per_t  = annual_capex / (m_co2 * float(econ["operating_hours"]))
    opex_per_t   = metrics.specific_energy * float(econ["electricity_cost"])
    return capex_per_t + opex_per_t


def membrane_areas_from_x(
    x: list[float],
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> dict[str, float]:
    """具体トポロジー次元の x から膜面積 {unit: m2} を取り出す（cost_per_tco2 用）。

    x は evaluator に渡すものと同じ「具体トポロジー次元」（x_for_topology 適用後）、
    cont_vars は continuous_variables(topology, membrane_model) と同順であること。
    area 変数が x に無い膜（あり得ないが防御）は units.params の値にフォールバックする。
    """
    areas: dict[str, float] = {}
    for uname, udef in topology.get("units", {}).items():
        if udef.get("type") == "MEMB" or uname.startswith("MEMB"):
            p_area = (udef.get("params") or {}).get("area")
            if p_area is not None:
                areas[uname] = float(p_area)
    for val, cv in zip(x, cont_vars):
        uname, param = cv["unit_param"]
        if param == "area" and uname in topology.get("units", {}):
            areas[uname] = float(val)
    return areas


@dataclass
class Metrics:
    """GA ループ中の fitness 計算に必要な最小指標セット（ARCHITECTURE 5.2）。"""

    specific_energy: float                        # kWh/tCO2
    purity: float                                 # CO2 mol fraction [0, 1]
    recovery: float                               # CO2 recovery [0, 1]
    energy_breakdown: dict[str, float] = field(default_factory=dict)  # block → WNET [kW]

    @classmethod
    def bad(cls) -> "Metrics":
        """Aspen 非収束・クラッシュ・ビルド失敗時の番兵値を返す。"""
        return cls(specific_energy=BAD_VALUE, purity=0.0, recovery=0.0)


@dataclass
class DetailedResult:
    """best 解 1 点の詳細抽出結果（results.json の元になる）。"""

    metrics: Metrics
    stream_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {"V0": {"CO2_molfrac": float, "CO2_moleflow": float | None, "description": str}, ...}


@runtime_checkable
class Evaluator(Protocol):
    """評価の抽象境界（ARCHITECTURE 5.2）。

    GA はこの境界を通じて評価を呼び出す。topology / ga.py はこの Protocol しか見ない。
    """

    def evaluate_topology(
        self,
        topology: dict[str, Any],
        x_list: list[list[float]],
    ) -> list[Metrics]:
        """同一トポロジーを 1 回構築し、x_list を順に評価する。

        Parameters
        ----------
        topology : 具体トポロジー {vertices, arcs, units}
        x_list   : 連続変数ベクトルのリスト。
                   各 x の並び順は topology.continuous_variables(topology) が返す順に従う。

        Returns
        -------
        x_list と同じ長さの Metrics リスト。
        Aspen 非収束・クラッシュは Metrics.bad() で吸収する。
        """
        ...

    def evaluate_detailed(
        self,
        topology: dict[str, Any],
        x: list[float],
    ) -> DetailedResult:
        """best 解 1 点の詳細抽出。stream_results（各頂点の CO₂ 情報）まで返す。

        ビルド・実行失敗時は DetailedResult(metrics=Metrics.bad()) を返す。
        """
        ...
