"""ga.run_ga の単体テスト（Aspen 不要・モック evaluator）。

現状は x 整列（pruning 後トポロジーへのフィルタ）の検証が主。BO が本番最適化器の
ため GA の網羅テストは持たないが、x_for_topology の配線は GA 経路でも独立に確認する。

実行:
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
    """連続 x が具体トポロジー（pruning 後）の変数だけに絞られて evaluator に渡ること。"""

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
                         f"x の次元がトポロジーの変数数と不一致: {mismatches}")
        # pruned 側（2変数）と 2 段側（4変数）の両方を GA が踏んでいること
        # （seed 固定で決定論。踏んでいなければ seed を変えて再現性を確認する）
        self.assertIn(2, seen_dims, f"pruned 側が未評価: dims={seen_dims}")
        self.assertIn(4, seen_dims, f"2段側が未評価: dims={seen_dims}")
        self.assertEqual(len(gen_log), 3)
        # 計測フィールド "t"（経過秒）が単調非減少で付いている
        ts = [g["t"] for g in gen_log]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])), ts)
        # 各世代の評価実時間 t_eval（非負）
        for g in gen_log:
            self.assertGreaterEqual(g["t_eval"], 0.0)


class TestGACostObjective(unittest.TestCase):
    """12.2: objective=minimize_cost で GA の fitness がコスト軸で回り完走する。"""

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
                     "totflow": 80307.0, "co2_frac": 0.15},   # kmol/h（モル解釈）
            "ga": {"pop_size": 6, "n_gen": 2},
        }
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_ga(ss, case, _FeasibleMock(), seed=7)
        self.assertEqual(len(gen_log), 2)
        # コストスケール（数十 $/t）＋ penalty なし → fitness は BAD ではなく小さい値
        self.assertLess(gen_log[-1]["best_fitness"], 1000.0)


if __name__ == "__main__":
    unittest.main()
