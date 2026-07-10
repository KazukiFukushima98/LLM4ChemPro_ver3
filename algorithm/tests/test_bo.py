"""bo.run_bo の単体テスト（Aspen 不要・モック evaluator）。

torch/botorch の重い import を伴うので CPU/GPU どちらでも動くよう書く。

実行:
    uv run python -m unittest algorithm.tests.test_bo

軽量化のため n_init=4, n_iter=2, q_batch=2 に抑える（テストの目的は決定論・完走・bad 吸収）。
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

# bypass トグル SS fixture（gated 膜の pruning 検証用）。
# discover（-s tests）では top-level、`-m unittest tests.test_bo` ではパッケージ名になる。
try:
    from test_topology import make_bypass_toggle_ss
except ImportError:  # pragma: no cover
    from tests.test_topology import make_bypass_toggle_ss


def _load_seed() -> dict:
    return T.load_ss(SEED_PATH)


# 軽量 BO 設定（テスト専用。固定値で defaults を上書き）
_TEST_BO = {"n_init": 4, "n_iter": 2, "q_batch": 2}


def _make_case(bo_override: dict | None = None) -> dict:
    case = {
        "optimization_targets": {"purity_min": 0.9, "recovery_min": 0.7},
        "penalty_weight": 1.0e5,
        "bo": {**_TEST_BO, **(bo_override or {})},
    }
    return case


class _SmoothMock:
    """連続変数のノルム的な値を energy にした決定論的 Metrics を返す（GP fit が安定する形）。

    binary は無視（topology の units だけ見れば q が反映済み）。purity/recovery は固定。
    """

    def __init__(self) -> None:
        self.call_count = 0

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        self.call_count += len(x_list)
        out: list[Metrics] = []
        for x in x_list:
            energy = 100.0 + sum((float(v) % 100.0) for v in x)  # 有限・滑らかな疑似 energy
            out.append(Metrics(specific_energy=energy, purity=0.95, recovery=0.85))
        return out

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        m = self.evaluate_topology(topology, [x])[0]
        return DetailedResult(metrics=m)


class _AllBadMock:
    """常に bad を返す（GP fit が破綻しないかの edge ケース）。"""

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        return [Metrics.bad() for _ in x_list]

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        return DetailedResult(metrics=Metrics.bad())


class _ShortfallLandscapeMock:
    """全点 infeasible で shortfall と energy が逆相関する地形（12.5(a) の検証用）。

    第1連続変数 a（MEMB1_area。bounds は UNIT_BOUNDS から動的に取る）に対し
        purity   = 0.5 + 0.399 * t   （t=(a-lo)/(hi-lo)。最大 0.899 < 0.9 → 常に infeasible）
        recovery = 0.85              （≥ 0.7 → shortfall は purity 由来のみ）
        energy   = 100 + 0.01 * a
    → min-shortfall の観測は「a 最大」、penalty-min（penalty_weight=1 なら energy 支配）は
    「a 最小」となり、best 返却の軸を区別できる。評価した a は self.seen に記録する。
    """

    _LO, _HI = T.continuous_variables(_load_seed())[0]["bounds"]

    def __init__(self) -> None:
        self.seen: list[float] = []

    def evaluate_topology(self, topology: dict, x_list: list[list[float]]) -> list[Metrics]:
        out: list[Metrics] = []
        for x in x_list:
            a = float(x[0])
            self.seen.append(a)
            t = (a - self._LO) / (self._HI - self._LO)
            out.append(Metrics(specific_energy=100.0 + 0.01 * a,
                               purity=0.5 + 0.399 * t, recovery=0.85))
        return out

    def evaluate_detailed(self, topology: dict, x: list[float]) -> DetailedResult:
        return DetailedResult(metrics=self.evaluate_topology(topology, [x])[0])


class TestRunBO(unittest.TestCase):

    def setUp(self) -> None:
        # bo.py は torch を import する。importable であることを保証。
        from bo import run_bo  # noqa: F401

    def test_completes_on_seed_with_binary_and_continuous(self) -> None:
        """seed SS（binary + continuous あり）で run_bo が完走する。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _SmoothMock(), seed=1)
        # 戻り値の形状
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertGreaterEqual(n_evals, _TEST_BO["n_init"])
        # best は変数次元（binary + continuous）と同じ長さの素な list
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(best), len(bin_vars) + len(cont_vars))
        for v in best:
            self.assertIsInstance(v, float)
        # best_fitness は単調非増加（BO のベスト追跡）
        bests = [g["best_fitness"] for g in gen_log]
        for prev, curr in zip(bests, bests[1:]):
            self.assertLessEqual(curr, prev + 1e-6)

    def test_deterministic_with_same_seed(self) -> None:
        """同 seed で2回呼んで best と n_evals が一致する（決定論）。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best_a, log_a, n_a = run_bo(ss, case, _SmoothMock(), seed=42)
            best_b, log_b, n_b = run_bo(ss, case, _SmoothMock(), seed=42)
        self.assertEqual(n_a, n_b)
        # gen_log 全体一致
        self.assertEqual(
            [g["best_fitness"] for g in log_a],
            [g["best_fitness"] for g in log_b],
        )
        # best_x も一致（浮動小数）
        for va, vb in zip(best_a, best_b):
            self.assertAlmostEqual(va, vb, places=6)

    def test_handles_all_bad_metrics(self) -> None:
        """全評価が bad でも GP fit が破綻せず最後まで回る。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _AllBadMock(), seed=7)
        # 全部 bad なので best_fitness = BAD_VALUE
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        for g in gen_log:
            self.assertAlmostEqual(g["best_fitness"], BAD_VALUE, delta=1.0)

    def test_bootstrap_phase_when_all_infeasible(self) -> None:
        """feasible ゼロの間は第1相（bootstrap）で回り、完走する。"""
        from bo import run_bo

        class _InfeasibleMock(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                out = super().evaluate_topology(topology, x_list)
                # 常に制約未達（purity 0.5 / recovery 0.5）だが有限＝valid な観測
                return [Metrics(specific_energy=m.specific_energy, purity=0.5, recovery=0.5)
                        for m in out]

        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _InfeasibleMock(), seed=11)
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertTrue(all(g["phase"] == "bootstrap" for g in gen_log),
                        f"全反復 bootstrap のはずが {[g['phase'] for g in gen_log]}")

    def test_cei_phase_when_feasible_exists(self) -> None:
        """初期サンプルから feasible（targets 0.9/0.7 vs mock 0.95/0.85）→ 全反復 CEI。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, n_evals = run_bo(ss, case, _SmoothMock(), seed=12)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log))

    def test_bootstrap_off_restores_legacy_behavior(self) -> None:
        """bootstrap: off なら全 infeasible でも CEI（ロールバック口の確認）。"""
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
        """一時的 wedge（初回 bad→再評価で成功）が retry で回収され、GP を汚染しない。"""
        from bo import run_bo

        class _TransientWedgeMock(_SmoothMock):
            """各 x につき初回は bad、2回目以降は実値（transient wedge の再現）。"""

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
        # 全点が初回 bad → retry で全回収 ⇒ 評価数は名目の2倍、best は有限値
        self.assertEqual(n_evals, 2 * nominal)
        self.assertLess(gen_log[-1]["best_fitness"], BAD_VALUE)

    def test_bootstrap_best_returns_min_shortfall(self) -> None:
        """12.5(a): 全 infeasible 終了時、best は penalty-min ではなく min-shortfall の観測。

        penalty_weight=1 だと旧来の penalty-min は energy 支配（=a 最小）を選ぶが、
        bootstrap 相の探索軸は shortfall なので a 最大（purity 最良）を返すべき。
        """
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["penalty_weight"] = 1.0  # penalty-min と shortfall-min の選択を分離する
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=21)
        self.assertTrue(all(g["phase"] == "bootstrap" for g in gen_log))
        # 観測された a のうち最大（= shortfall 最小）が返る
        self.assertAlmostEqual(best[0], max(mock.seen), places=6)

    def test_bootstrap_off_best_keeps_legacy_penalty_min(self) -> None:
        """12.5(a) ロールバック口: bootstrap: off なら旧来の penalty-min 返却のまま。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"bootstrap": "off"})
        case["penalty_weight"] = 1.0
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=22)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log))
        # penalty_weight=1 では fitness ≈ energy = 100 + 0.01a → a 最小が返る
        self.assertAlmostEqual(best[0], min(mock.seen), places=6)

    def test_bootstrap_yaml_false_treated_as_off(self) -> None:
        """YAML 1.1 は `off` を bool False にパースする。False でも off 扱いになること。"""
        from bo import run_bo
        ss = _load_seed()
        case = _make_case({"bootstrap": False})
        mock = _ShortfallLandscapeMock()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, mock, seed=23)
        self.assertTrue(all(g["phase"] == "cei" for g in gen_log),
                        f"bootstrap=False は off のはずが {[g['phase'] for g in gen_log]}")

    def test_fixed_features_exclude_unbuildable_combos(self) -> None:
        """トグルペア（片方しか ON にできない）でビルド不能な組合せが探索空間から除外される。"""
        import bo as bo_mod
        import topology as T2
        ss = _load_seed()
        # V6 は固定 (V6,V8) residue を持つ。候補を2本足すと ON で出次数>1 → unbuildable
        ss["arcs"][("V6", "V1")] = {"type": "recycle", "candidate": "q_1"}
        ss["arcs"][("V6", "V4")] = {"type": "recycle", "candidate": "q_2"}
        bin_vars = T2.binary_variables(ss)
        combos = bo_mod._build_fixed_features(ss, bin_vars)
        # 生き残るのは (0,0) のみ（どちらか ON で V6 出次数2の非膜分流＝unbuildable）
        self.assertEqual(combos, [{0: 0.0, 1: 0.0}])

    def test_gp_failure_falls_back_to_sobol(self) -> None:
        """GP fit の例外でループが死なず、Sobol フォールバックで全反復を完走する。

        初期観測の縮退（分散ゼロ）や数値不安定で fit_gpytorch_mll が投げても、
        BO 反復（=1回数時間の Aspen 予算）を丸ごと失わないための安定性ガード。
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

        # 全反復がフォールバックで進み、評価数も全反復分ある
        self.assertEqual(len(gen_log), _TEST_BO["n_iter"])
        self.assertEqual(
            n_evals,
            _TEST_BO["n_init"] + _TEST_BO["n_iter"] * _TEST_BO["q_batch"],
        )
        # best は正常な形（変数次元と同じ長さ）
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(best), len(bin_vars) + len(cont_vars))


class TestMembraneModelIntegration(unittest.TestCase):
    """12.1: membrane_model 有効時に permeance 変数が BO の探索次元に入り完走する。"""

    def test_tie_mode_adds_one_shared_dimension(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["membrane_model"] = {"tie": True}
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=51)
        # seed は連続4変数 + 共有 MEMB_perm = 5 次元
        self.assertEqual(len(best), 5)
        lo, hi = T.continuous_variables(ss, {"tie": True})[0]["bounds"]
        self.assertGreaterEqual(best[0], lo - 1e-9)  # 先頭が MEMB_perm
        self.assertLessEqual(best[0], hi + 1e-9)

    def test_untied_mode_adds_per_membrane_dimension(self) -> None:
        from bo import run_bo
        ss = _load_seed()
        case = _make_case()
        case["membrane_model"] = {"tie": False}
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _SmoothMock(), seed=52)
        # 連続4変数 + MEMB1_perm + MEMB2_perm = 6 次元
        self.assertEqual(len(best), 6)


class TestXAlignmentWithPruning(unittest.TestCase):
    """連続 x が具体トポロジー（pruning 後）の変数だけに絞られて evaluator に渡ること。

    修正前は「テンプレート全次元の x」と「pruned topology の continuous_variables」を
    位置 zip していたため、途中ユニットの pruning で後続ユニットに前のユニットの値が
    書かれる整列バグがあった（topology.x_for_topology で修正）。
    """

    def test_evaluate_batch_filters_x_to_topology(self) -> None:
        import numpy as np

        import bo as bo_mod
        ss = make_bypass_toggle_ss()
        bin_vars  = T.binary_variables(ss)
        cont_vars = T.continuous_variables(ss)
        self.assertEqual(len(bin_vars), 2)
        self.assertEqual(len(cont_vars), 4)

        seen: list[tuple[int, int]] = []  # (トポロジーのユニット数, 受け取った x の次元)

        class _DimRecorder(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                for x in x_list:
                    seen.append((len(topology["units"]), len(x)))
                return super().evaluate_topology(topology, x_list)

        # 列: [q_1, q_2 | MEMB1_area, MEMB1_p_perm, MEMB2_area, MEMB2_p_perm]
        x_np = np.array([
            [1.0, 0.0, 200000.0, 0.5, 300000.0, 0.4],  # 2段 → x は 4 変数
            [0.0, 1.0, 200000.0, 0.5, 300000.0, 0.4],  # MEMB2 prune → x は 2 変数
        ])
        e, p, r, v, n = bo_mod._evaluate_batch_multi(
            x_np, ss, bin_vars, cont_vars, 2, _DimRecorder(), retry_bad=0
        )
        self.assertIn((2, 4), seen, f"2段側の次元が不正: {seen}")
        self.assertIn((1, 2), seen, f"pruned 側の次元が不正: {seen}")
        self.assertTrue(all(v))


class TestLogScaleInputs(unittest.TestCase):
    """12.5(b) 対数スケール化（bounds 比 50 倍超の正の連続変数を log 空間で探索）。"""

    def test_mask_on_seed_with_lee_bounds_is_all_false(self) -> None:
        """ver3 の Lee 整合 bounds（area 比15・p_permeate 比9.9）では seed に log 対象なし。

        12.5(b) の実装は bounds 比 50 倍超のケース（bounds_override や将来の広い範囲）
        への保険として残る。正例は下の synthetic テストがカバーする。
        """
        import bo as bo_mod
        cont_vars = T.continuous_variables(_load_seed())
        mask = bo_mod._log_scale_mask(0, cont_vars, enabled=True)
        self.assertFalse(mask.any())

    def test_mask_excludes_narrow_nonpositive_and_binary(self) -> None:
        """比 ≤50・下限 ≤0 の連続変数と binary 次元は対象外。"""
        import bo as bo_mod
        cont_vars = [
            {"name": "narrow", "bounds": [1.0, 10.0]},    # 比10 ≤ 50 → 対象外
            {"name": "nonpos", "bounds": [0.0, 100.0]},   # 下限0 → 対象外
            {"name": "wide",   "bounds": [0.01, 10.0]},   # 比1000 → 対象
        ]
        mask = bo_mod._log_scale_mask(2, cont_vars, enabled=True)  # binary 2本
        self.assertEqual(mask.tolist(), [False, False, False, False, True])

    def test_mask_disabled_is_all_false(self) -> None:
        """log_scale_inputs: off 相当（enabled=False）で全 False＝線形スケール。"""
        import bo as bo_mod
        cont_vars = T.continuous_variables(_load_seed())
        mask = bo_mod._log_scale_mask(0, cont_vars, enabled=False)
        self.assertFalse(mask.any())

    def test_to_eval_space_exps_only_masked_columns(self) -> None:
        """_to_eval_space は mask 列だけ exp し、binary・線形列は不変。"""
        import math

        import bo as bo_mod
        import numpy as np
        mask = np.array([False, True, False])
        x = np.array([[1.0, math.log(20000.0), 0.5]])
        out = bo_mod._to_eval_space(x, mask)
        self.assertAlmostEqual(out[0, 0], 1.0)
        self.assertAlmostEqual(out[0, 1], 20000.0, places=6)
        self.assertAlmostEqual(out[0, 2], 0.5)
        # 元配列は破壊しない
        self.assertAlmostEqual(x[0, 1], math.log(20000.0))

    def test_best_and_evaluated_x_within_original_bounds(self) -> None:
        """log 有効（既定）でも evaluator が受ける x と best は元 bounds の実スケール内。"""
        from bo import run_bo

        cont_vars = T.continuous_variables(_load_seed())
        bounds = [cv["bounds"] for cv in cont_vars]

        class _RangeCheckMock(_SmoothMock):
            def evaluate_topology(self, topology, x_list):
                for x in x_list:
                    for v, (lo, hi) in zip(x, bounds):
                        assert lo - 1e-9 <= float(v) <= hi + 1e-9, \
                            f"eval x={v} が bounds [{lo}, {hi}] 外（log 空間のまま渡った疑い）"
                return super().evaluate_topology(topology, x_list)

        ss = _load_seed()
        case = _make_case()
        with redirect_stdout(io.StringIO()):
            best, gen_log, _ = run_bo(ss, case, _RangeCheckMock(), seed=31)
        for v, (lo, hi) in zip(best, bounds):
            self.assertGreaterEqual(v, lo - 1e-9)
            self.assertLessEqual(v, hi + 1e-9)

    def test_log_scale_off_restores_legacy_and_completes(self) -> None:
        """log_scale_inputs: off（YAML の bool False も含む）で線形スケールのまま完走する。"""
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
