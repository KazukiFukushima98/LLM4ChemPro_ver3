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


if __name__ == "__main__":
    unittest.main()
