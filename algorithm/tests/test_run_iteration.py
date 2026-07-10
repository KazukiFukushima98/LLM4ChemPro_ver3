"""get_next_iter_num の採番ロジックの単体テスト（Aspen 不要・決定的）。

iterations/ を tempfile で組み、iter_NNN の数値部分の max + 1 が返ることを検証する。
削除後の非上書き（count+1 ではなく max+1）・欠番・異物 skip を重点的に確認する。

import run_iteration は top-level で simulator(→pythoncom)・ga(→deap) を連鎖
import するが、Aspen 自体は起動しないのでテストは Aspen 不要のまま。

実行:
    uv run python -m unittest algorithm.tests.test_run_iteration
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import run_iteration  # noqa: E402
from evaluator import DetailedResult, Metrics  # noqa: E402

try:
    from test_topology import make_bypass_toggle_ss
except ImportError:  # pragma: no cover
    from tests.test_topology import make_bypass_toggle_ss


def _mkdirs(base: str, names: list[str]) -> None:
    """base/iterations/ を作り、names の各エントリをディレクトリとして作る。"""
    iter_dir = os.path.join(base, "iterations")
    os.makedirs(iter_dir, exist_ok=True)
    for n in names:
        os.makedirs(os.path.join(iter_dir, n), exist_ok=True)


class TestBuildResultsGhostParams(unittest.TestCase):
    """optimal_params から pruned ユニットの自由次元を除外する（幽霊シグナル防止）。

    x 整列修正により pruned 膜の変数は評価に影響しない自由次元となり、optimizer が
    置いた任意の値（境界値になりやすい）をそのまま記録すると extract_bounds_hit が
    「存在しない膜の張り付き」を報告して SST を誤誘導する。best トポロジーに存在する
    変数だけが記録されることを検証する。
    """

    _METRICS = Metrics(specific_energy=300.0, purity=0.96, recovery=0.92,
                       energy_breakdown={"VP1": 100.0})

    def _build(self, q1: float, q2: float) -> dict:
        ss = make_bypass_toggle_ss()
        # 染色体: [q_1, q_2 | MEMB1_area, MEMB1_p_perm, MEMB2_area, MEMB2_p_perm]
        best = [q1, q2, 111000.0, 0.5, 222000.0, 0.4]
        detailed = DetailedResult(metrics=self._METRICS)
        return run_iteration.build_results_dict(1, best, ss, detailed, [], 0)

    def test_pruned_membrane_params_excluded(self) -> None:
        """バイパス ON（MEMB2 pruned）→ MEMB2 の変数は記録されない。"""
        results = self._build(q1=0.0, q2=1.0)
        params = results["optimal_params"]
        self.assertEqual(params["q_1"], 0)
        self.assertEqual(params["q_2"], 1)
        self.assertAlmostEqual(params["MEMB1_area"], 111000.0)
        self.assertAlmostEqual(params["MEMB1_p_perm"], 0.5)
        self.assertNotIn("MEMB2_area", params)
        self.assertNotIn("MEMB2_p_perm", params)

    def test_active_membrane_params_kept(self) -> None:
        """給餌 ON（MEMB2 あり）→ 全変数が記録される。"""
        results = self._build(q1=1.0, q2=0.0)
        params = results["optimal_params"]
        self.assertAlmostEqual(params["MEMB2_area"], 222000.0)
        self.assertAlmostEqual(params["MEMB2_p_perm"], 0.4)


class TestGetNextIterNum(unittest.TestCase):
    def test_iterations_dir_absent(self) -> None:
        """iterations/ が無ければ 1 を返し、ディレクトリを作成する。"""
        with tempfile.TemporaryDirectory() as base:
            self.assertFalse(os.path.isdir(os.path.join(base, "iterations")))
            self.assertEqual(run_iteration.get_next_iter_num(base), 1)
            self.assertTrue(os.path.isdir(os.path.join(base, "iterations")))

    def test_empty(self) -> None:
        """iterations/ が空なら 1。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, [])
            self.assertEqual(run_iteration.get_next_iter_num(base), 1)

    def test_consecutive(self) -> None:
        """iter_001, iter_002 → 3。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_002"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)

    def test_single(self) -> None:
        """iter_001 のみ → 2。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 2)

    def test_no_overwrite_after_delete(self) -> None:
        """iter_002 のみ（iter_001 削除後）→ 3（count+1 の 2 ではない）。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_002"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)

    def test_gap(self) -> None:
        """iter_001, iter_003（欠番）→ 4。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_003"])
            self.assertEqual(run_iteration.get_next_iter_num(base), 4)

    def test_ignore_non_numeric_and_non_dir(self) -> None:
        """異物（非数字・非ディレクトリ）は skip。iter_001, iter_002 が効いて 3。"""
        with tempfile.TemporaryDirectory() as base:
            _mkdirs(base, ["iter_001", "iter_002", "iter_foo", "notiter", "iter_"])
            # iter_005_log.txt はファイル（非ディレクトリ異物）として作る
            with open(os.path.join(base, "iterations", "iter_005_log.txt"), "w") as f:
                f.write("log\n")
            self.assertEqual(run_iteration.get_next_iter_num(base), 3)


class TestApplyGaOverrides(unittest.TestCase):
    """CLI 由来の pop/gen 上書きが case.yaml を汚さず in-memory のみで効くこと。"""

    def test_overrides_applied_in_memory_only(self) -> None:
        case = run_iteration.load_case()
        snap = copy.deepcopy(case)
        with open(run_iteration.CASE_PATH, "r", encoding="utf-8") as f:
            disk_before = f.read()

        out = run_iteration.apply_ga_overrides(case, 4, 3)

        # 上書き結果が返る
        self.assertEqual(out["ga"]["pop_size"], 4)
        self.assertEqual(out["ga"]["n_gen"], 3)
        # 入力 dict は不変（純関数・コピーを返す）
        self.assertEqual(case, snap)
        # ディスク上の case.yaml は無変更
        with open(run_iteration.CASE_PATH, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), disk_before)

    def test_partial_override_keeps_other(self) -> None:
        case = {"ga": {"pop_size": 20, "n_gen": 50}}
        out = run_iteration.apply_ga_overrides(case, 4, None)
        self.assertEqual(out["ga"]["pop_size"], 4)
        self.assertEqual(out["ga"]["n_gen"], 50)  # gen は未指定なので据え置き

    def test_none_passthrough_no_change(self) -> None:
        case = {"ga": {"pop_size": 20, "n_gen": 50}}
        out = run_iteration.apply_ga_overrides(case, None, None)
        self.assertEqual(out["ga"], {"pop_size": 20, "n_gen": 50})


class TestDetailedEvalRetry(unittest.TestCase):
    """詳細評価の transient wedge リトライ（run22 で実測した故障モードの回帰テスト）。"""

    class _FlakyEvaluator:
        """最初の fail_n 回は bad、その後は実値を返すモック。"""

        def __init__(self, fail_n: int) -> None:
            from evaluator import DetailedResult, Metrics
            self._fail_n = fail_n
            self.calls = 0
            self._bad = DetailedResult(metrics=Metrics.bad())
            self._good = DetailedResult(
                metrics=Metrics(specific_energy=999.0, purity=0.78, recovery=0.82),
                stream_results={"V0": {"CO2_molfrac": 0.15}},
            )

        def evaluate_detailed(self, topology, x):
            self.calls += 1
            return self._bad if self.calls <= self._fail_n else self._good

    def test_first_success_no_retry(self) -> None:
        ev = self._FlakyEvaluator(fail_n=0)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0])
        self.assertEqual(ev.calls, 1)
        self.assertAlmostEqual(d.metrics.specific_energy, 999.0)

    def test_transient_wedge_recovers_on_retry(self) -> None:
        """run22 の実測パターン：1回目 wedge → リトライで実値回復。"""
        ev = self._FlakyEvaluator(fail_n=1)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0])
        self.assertEqual(ev.calls, 2)
        self.assertAlmostEqual(d.metrics.specific_energy, 999.0)
        self.assertIn("V0", d.stream_results)

    def test_persistent_failure_returns_bad_after_budget(self) -> None:
        """全滅でも例外にせず bad を返す（呼び出し側の既存経路を維持）。"""
        from evaluator import BAD_VALUE
        ev = self._FlakyEvaluator(fail_n=10)
        d = run_iteration.evaluate_detailed_with_retry(ev, {}, [0.0], retries=2)
        self.assertEqual(ev.calls, 3)  # 初回 + リトライ2回で打ち切り
        self.assertGreaterEqual(d.metrics.specific_energy, BAD_VALUE)


class TestCliCommitWiring(unittest.TestCase):
    """--no-commit / --pop / --gen が run_one_iteration へ正しく配線されること（Aspen 不要）。

    run_one_iteration をモックに差し替えて main() のCLI配線だけを検証する。
    モックは GA も auto-commit も呼ばないので git サブプロセスは起動されない。
    """

    def _run_main(self, argv: list[str]) -> dict:
        captured: dict = {}

        def fake_run_one_iteration(base_dir, case, commit=True):
            captured["commit"] = commit
            captured["pop"] = case["ga"]["pop_size"]
            captured["gen"] = case["ga"]["n_gen"]
            return {}

        orig_fn = run_iteration.run_one_iteration
        orig_argv = sys.argv
        run_iteration.run_one_iteration = fake_run_one_iteration
        sys.argv = argv
        try:
            run_iteration.main()
        finally:
            run_iteration.run_one_iteration = orig_fn
            sys.argv = orig_argv
        return captured

    def test_no_commit_and_overrides_thread_through(self) -> None:
        cap = self._run_main(
            ["run_iteration.py", "--base-dir", "X", "--no-commit", "--pop", "4", "--gen", "3"]
        )
        self.assertFalse(cap["commit"])  # --no-commit → commit=False（auto-commit 不発）
        self.assertEqual(cap["pop"], 4)
        self.assertEqual(cap["gen"], 3)

    def test_default_commits(self) -> None:
        cap = self._run_main(["run_iteration.py", "--base-dir", "X"])
        self.assertTrue(cap["commit"])  # 既定は commit=True


if __name__ == "__main__":
    unittest.main()
