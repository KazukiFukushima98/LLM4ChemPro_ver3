"""_extract_metrics の質量収支ガード（COORDINATION Fix A）の単体テスト。

リサイクル tear がヘッドレス COM 上で「収束」と返っても非物理な点（回収率 > 1）に
落ちることがある（run12/iter_002 で実観測：製品 CO2 188.5 > feed 150 ＝回収 125.67%）。
_extract_metrics に入れた検算ガードが、回収率 > recovery_physical_max を BAD で弾くことを検証する。

simulator は pythoncom / aspen_builder（pywin32 依存）を import するため、import 不能環境では
skip する。実 Aspen は不要：_safe をモックに差し替え、Aspen ツリーを読まずに値を注入する。

実行:
    uv run python -m unittest algorithm.tests.test_simulator_metrics
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from evaluator import BAD_VALUE, Metrics  # noqa: E402

try:
    from simulator import AspenEvaluator  # noqa: E402
    _SIM_IMPORT_ERR = None
except Exception as e:  # pragma: no cover - pywin32 不在環境
    AspenEvaluator = None
    _SIM_IMPORT_ERR = e


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


@unittest.skipUnless(
    AspenEvaluator is not None,
    f"simulator import 不可（pywin32 不在等）: {_SIM_IMPORT_ERR}",
)
class TestMassBalanceGuard(unittest.TestCase):
    """_extract_metrics の回収率検算ガードを、_safe モックで実 Aspen なしに検証。"""

    PURITY = 0.9
    V0_MF = 150.0   # feed CO2 流量（固定）
    POWER = 10.0    # 各 energy block の WNET

    def _evaluator(self, prod_mf: float) -> AspenEvaluator:
        """case={} で AspenEvaluator を構築し、_safe を path で値を返すモックに差し替える。

        _extract_metrics が読む 4 種の path を判別する:
          MOLEFRAC           → 純度
          WNET               → 動力
          MOLEFLOW + "\\V0\\" → feed CO2 流量
          MOLEFLOW (その他)   → 製品 CO2 流量
        """
        ev = AspenEvaluator({}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return self.PURITY
            if "WNET" in path:
                return self.POWER
            if "MOLEFLOW" in path:
                return self.V0_MF if "\\V0\\" in path else prod_mf
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        return ev

    def _extract(self, prod_mf: float) -> Metrics:
        ev = self._evaluator(prod_mf)
        return ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=["MEMB1"])

    def test_recovery_above_one_is_bad(self):
        """回収率 1.257（run12 の非物理点）→ BAD で弾く。"""
        m = self._extract(prod_mf=188.5)  # 188.5 / 150 = 1.2567
        self.assertTrue(_is_bad(m), f"非物理(回収>1)が BAD でない: {m}")

    def test_recovery_below_one_is_ok(self):
        """回収率 0.773（正常域）→ 通す。"""
        m = self._extract(prod_mf=115.9)  # 115.9 / 150 = 0.7727
        self.assertFalse(_is_bad(m), f"正常解が誤って BAD: {m}")
        self.assertAlmostEqual(m.recovery, 115.9 / 150.0, places=4)
        self.assertAlmostEqual(m.purity, self.PURITY, places=6)

    def test_recovery_exactly_one_is_ok(self):
        """回収率 1.0（境界・全量回収）→ 既定 max=1.02 以下なので通す。"""
        m = self._extract(prod_mf=150.0)  # 150 / 150 = 1.0
        self.assertFalse(_is_bad(m), f"回収=1.0 が誤って BAD: {m}")
        self.assertAlmostEqual(m.recovery, 1.0, places=6)

    def test_threshold_is_configurable(self):
        """recovery_physical_max を case で下げると、境界が動く。"""
        ev = AspenEvaluator({"recovery_physical_max": 0.9}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return self.PURITY
            if "WNET" in path:
                return self.POWER
            if "MOLEFLOW" in path:
                return self.V0_MF if "\\V0\\" in path else 142.5  # 142.5/150 = 0.95 > 0.9
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        m = ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=["MEMB1"])
        self.assertTrue(_is_bad(m), f"max=0.9 下で 0.95 が BAD でない: {m}")


@unittest.skipUnless(
    AspenEvaluator is not None,
    f"simulator import 不可（pywin32 不在等）: {_SIM_IMPORT_ERR}",
)
class TestEnergyGuard(unittest.TestCase):
    """エネルギー検算ガード：WNET 読み取り全滅（合計 0）の偽ゼロエネルギー解を弾く。"""

    def _extract(self, wnet: float, energy_blocks: list[str]) -> Metrics:
        ev = AspenEvaluator({}, "", "")

        def fake_safe(aspen, path, default=None):
            if "MOLEFRAC" in path:
                return 0.9
            if "WNET" in path:
                return wnet
            if "MOLEFLOW" in path:
                return 150.0 if "\\V0\\" in path else 115.9
            return default

        ev._safe = fake_safe  # type: ignore[method-assign]
        return ev._extract_metrics(aspen=None, product_vid="V7", energy_blocks=energy_blocks)

    def test_zero_wnet_with_blocks_is_bad(self):
        """VP がいるのに WNET 合計 0 → 偽の spec_e=0 として BAD で弾く。"""
        m = self._extract(wnet=0.0, energy_blocks=["VP1", "VP2"])
        self.assertTrue(_is_bad(m), f"偽ゼロエネルギー解が BAD でない: {m}")

    def test_positive_wnet_with_blocks_is_ok(self):
        m = self._extract(wnet=10.0, energy_blocks=["VP1"])
        self.assertFalse(_is_bad(m), f"正常解が誤って BAD: {m}")
        self.assertGreater(m.specific_energy, 0.0)

    def test_no_energy_blocks_zero_energy_allowed(self):
        """energy_blocks が空（VP/COMP 無しの構造）は spec_e=0 を許す（別途ペナルティが効く）。"""
        m = self._extract(wnet=0.0, energy_blocks=[])
        self.assertFalse(_is_bad(m), f"blocks なしの 0 エネルギーが誤って BAD: {m}")
        self.assertEqual(m.specific_energy, 0.0)


if __name__ == "__main__":
    unittest.main()
