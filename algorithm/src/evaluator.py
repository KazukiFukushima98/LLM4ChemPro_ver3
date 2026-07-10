"""評価の抽象境界（Evaluator Protocol）と結果型定義。

ARCHITECTURE 5.2 の Evaluator Protocol と、Metrics / DetailedResult の dataclass を定義する。
実装は simulator.AspenEvaluator が行う。将来サロゲート（FMQA/BOQA 等）に差し替える際は
別実装を注入するだけでよい。テストはモック注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


BAD_VALUE: float = 1.0e6


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
