"""比較対象（12.6 一括最適化・baseline/）の単体テスト（Aspen 不要）。

対象:
- ss_lee2.json / ss_lee3.json（Lee Fig.2 型 SS・圧力機器なし）の構造整合
- one-hot 有効組合せの解析的生成（81 / 4096）と全数ビルド可能性（設計不変条件）
- ga_onehot（カテゴリカル構造遺伝子の GA）の完走・one-hot 保証・決定論
- BO への実行時注入（patch_bo_for_onehot）での完走と one-hot 保証

実行:
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

_MM = {"tie": False}  # run24 と同じ段別独立膜


class _SmoothMock:
    """決定論的な有限 Metrics を返すモック（test_bo と同型）。"""

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
        # 段別独立膜: 2膜 × (area, p_perm, perm) = 6
        self.assertEqual(len(T.continuous_variables(ss, _MM)), 6)

    def test_lee3_shape(self):
        ss = T.load_ss(LEE3)
        self.assertEqual(len(T.binary_variables(ss)), 24)
        self.assertEqual([len(g) for g in onehot_groups(ss)], [4] * 6)
        self.assertEqual(len(T.continuous_variables(ss, _MM)), 9)

    def test_lee2_series_no_recycle_topology(self):
        """直列（M1→M2→P、残渣→R）の one-hot 割当が正しい2段構造になる。"""
        ss = T.load_ss(LEE2)
        q = {f"q_{k}": 0 for k in range(1, 13)}
        q.update({"q_2": 1, "q_6": 1, "q_9": 1, "q_12": 1})  # V2→V4, V3→R, V5→P, V6→R
        topo = T.active_topology(ss, q)
        self.assertIsNone(T.is_buildable(topo))
        self.assertEqual(set(topo["units"]), {"MEMB1", "MEMB2"})
        self.assertIn(("V2", "V4"), topo["arcs"])
        self.assertIn(("V5", "V7"), topo["arcs"])

    def test_lee2_unfed_m2_prunes_to_single_stage(self):
        """M2 に誰も給餌しない割当では M2 が刈られ、1段構成として成立する。"""
        ss = T.load_ss(LEE2)
        q = {f"q_{k}": 0 for k in range(1, 13)}
        q.update({"q_3": 1, "q_6": 1})  # V2→P, V3→R（M2 系は給餌なし）
        topo = T.active_topology(ss, q)
        self.assertIsNone(T.is_buildable(topo))
        self.assertEqual(set(topo["units"]), {"MEMB1"})


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
        """設計不変条件: one-hot なら全組合せがビルド可能（81 個の全数検証）。"""
        ss = T.load_ss(LEE2)
        assert_all_buildable(ss, build_onehot_fixed_features(ss))

    @unittest.skipUnless(os.environ.get("BASELINE_FULL") == "1",
                         "重い全数検証（~10分）。BASELINE_FULL=1 で実行。"
                         "本番ランナーは起動時に毎回同じ検査を行うため安全性は保たれる")
    def test_lee3_all_combos_buildable(self):
        """同・3段版（4096 個の全数検証。ランナー実行時と同じ検査）。

        2026-07-12 に BASELINE_FULL=1 で全数 PASS 済み。日常スイートでは skip。
        """
        ss = T.load_ss(LEE3)
        assert_all_buildable(ss, build_onehot_fixed_features(ss))

    def test_lee3_sampled_combos_buildable(self):
        """3段版の抜き取り検証（全 4096 から決定論的に 128 個）。日常スイート用。"""
        ss = T.load_ss(LEE3)
        combos = build_onehot_fixed_features(ss)
        assert_all_buildable(ss, combos[::32])  # 128 個


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
            self.assertEqual(sum(best[i] for i in g), 1.0, "best が one-hot でない")
        self.assertEqual(len(gen_log), 3)
        self.assertGreaterEqual(n_evals, 8)
        # 連続部が bounds 内
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
        """evaluator に渡る x が具体トポロジー（pruning 後）の変数次元と一致する。"""
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
    """patch_bo_for_onehot（ランナーと同一コード）で run_bo が完走し one-hot を保つ。"""

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
            bo_mod._build_fixed_features = orig_ff   # 他テストへの影響を残さない
            bo_mod._sobol_initial = orig_sobol
        n_bin = len(T.binary_variables(ss))
        self.assertEqual(len(best), n_bin + len(T.continuous_variables(ss, _MM)))
        for g in onehot_groups(ss):
            self.assertEqual(sum(round(best[i]) for i in g), 1, "best が one-hot でない")
        self.assertEqual(len(gen_log), 2)


if __name__ == "__main__":
    unittest.main()
