"""signals.py の単体テスト（Aspen 不要）。

実行:
    uv run python -m unittest algorithm.tests.test_signals
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import topology as T  # noqa: E402
import signals as S  # noqa: E402


SEED_PATH = os.path.normpath(os.path.join(HERE, "..", "ss_seed.json"))


def load_seed() -> dict:
    return T.load_ss(SEED_PATH)


def make_case(purity_min: float = 0.9, recovery_min: float = 0.7) -> dict:
    return {
        "optimization_targets": {
            "purity_min":   purity_min,
            "recovery_min": recovery_min,
        }
    }


def make_results(
    iteration: int = 1,
    purity: float = 0.514,
    recovery: float = 0.267,
    spec_e: float = 290.3,
    optimal_params: dict | None = None,
    energy_breakdown: dict | None = None,
    stream_results: dict | None = None,
    active_candidates: dict | None = None,
) -> dict:
    return {
        "iteration": iteration,
        "performance": {
            "CO2_purity":               purity,
            "CO2_recovery":             recovery,
            "specific_energy_kWh_tCO2": spec_e,
            "total_compressor_kW":      sum((energy_breakdown or {}).values()),
        },
        "optimal_params":    optimal_params    or {},
        "active_candidates": active_candidates or {},
        "stream_results":    stream_results    or {},
        "energy_breakdown":  energy_breakdown  or {},
        "gen_log":           [],
        "n_evaluations":     0,
    }


# =========================================================
# bounds_hit
# =========================================================

class TestBoundsHit(unittest.TestCase):

    def test_middle_value_no_hit(self):
        ss = load_seed()
        # area bounds = [100, 500000]; 中央付近 → 張り付かない
        # p_perm bounds = [0.01, 0.99]; 中央 0.5 → 張り付かない
        results = make_results(optimal_params={
            "MEMB1_area": 200000.0, "MEMB1_p_perm": 0.5,
            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.5,
        })
        hits = S.extract_bounds_hit(results, ss)
        self.assertEqual(hits, [])

    def test_lower_hit_detected(self):
        ss = load_seed()
        # p_perm の下限 0.01 に張り付き
        results = make_results(optimal_params={
            "MEMB1_area": 200000.0, "MEMB1_p_perm": 0.5,
            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.01,
        })
        hits = S.extract_bounds_hit(results, ss)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h.name, "MEMB2_p_perm")
        self.assertEqual(h.side, "lower")
        self.assertAlmostEqual(h.limit, 0.01)
        self.assertLess(h.slack_ratio, S.BOUNDS_HIT_SLACK)

    def test_upper_hit_detected(self):
        ss = load_seed()
        results = make_results(optimal_params={
            "MEMB1_area": 499000.0, "MEMB1_p_perm": 0.5,  # 上限 500000 に張り付き
            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.5,
        })
        hits = S.extract_bounds_hit(results, ss)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].name, "MEMB1_area")
        self.assertEqual(hits[0].side, "upper")
        self.assertAlmostEqual(hits[0].limit, 500000.0)

    def test_bounds_override_reflected(self):
        ss = load_seed()
        # MEMB1.area の bounds を [200, 100000] に上書き
        ss["units"]["MEMB1"]["bounds_override"] = {"area": [200.0, 100000.0]}
        # 元の bounds=[100,500000] では中央扱いだった 99500 が、
        # 上書き後 [200, 100000] では上限張り付きになる
        results = make_results(optimal_params={
            "MEMB1_area": 99500.0, "MEMB1_p_perm": 0.5,
            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.5,
        })
        hits = S.extract_bounds_hit(results, ss)
        names = [h.name for h in hits]
        self.assertIn("MEMB1_area", names)
        h = next(h for h in hits if h.name == "MEMB1_area")
        self.assertEqual(h.side, "upper")
        self.assertAlmostEqual(h.limit, 100000.0)

    def test_missing_param_skipped(self):
        ss = load_seed()
        # MEMB1_area が optimal_params に無い → skip（KeyError にならない）
        results = make_results(optimal_params={
            "MEMB1_p_perm": 0.5,
            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.5,
        })
        hits = S.extract_bounds_hit(results, ss)
        self.assertEqual(hits, [])  # MEMB1_area が無いだけで他は中央


# =========================================================
# energy_blocks
# =========================================================

class TestEnergyBlocks(unittest.TestCase):

    def test_sorted_desc_by_share(self):
        results = make_results(energy_breakdown={"VP1": 146.0, "VP2": 364.0, "VP3": 50.0})
        blocks = S.extract_energy_blocks(results)
        names = [b.block for b in blocks]
        self.assertEqual(names, ["VP2", "VP1", "VP3"])

    def test_dominant_flag_set_when_share_above_threshold(self):
        results = make_results(energy_breakdown={"VP1": 80.0, "VP2": 20.0})
        blocks = S.extract_energy_blocks(results)
        d = next(b for b in blocks if b.block == "VP1")
        nd = next(b for b in blocks if b.block == "VP2")
        self.assertTrue(d.dominant)
        self.assertFalse(nd.dominant)
        self.assertAlmostEqual(d.share, 0.8)
        self.assertAlmostEqual(nd.share, 0.2)

    def test_no_dominant_when_balanced(self):
        results = make_results(energy_breakdown={"VP1": 100.0, "VP2": 100.0})
        blocks = S.extract_energy_blocks(results)
        for b in blocks:
            self.assertFalse(b.dominant)

    def test_empty_breakdown_returns_empty(self):
        results = make_results(energy_breakdown={})
        self.assertEqual(S.extract_energy_blocks(results), [])

    def test_zero_total_returns_empty(self):
        results = make_results(energy_breakdown={"VP1": 0.0, "VP2": 0.0})
        self.assertEqual(S.extract_energy_blocks(results), [])


# =========================================================
# residue_losses
# =========================================================

class TestResidueLosses(unittest.TestCase):

    def test_single_residue(self):
        ss = load_seed()  # V8 が residue
        results = make_results(stream_results={
            "V0": {"CO2_molfrac": 0.15, "CO2_moleflow": 1.0, "description": "Feed"},
            "V8": {"CO2_molfrac": 0.05, "CO2_moleflow": 0.30, "description": "Residue"},
        })
        losses = S.extract_residue_losses(results, ss)
        self.assertEqual(len(losses), 1)
        loss = losses[0]
        self.assertEqual(loss.vid, "V8")
        self.assertAlmostEqual(loss.co2_moleflow, 0.30)
        self.assertAlmostEqual(loss.share_of_feed_co2, 0.30)

    def test_multiple_residues(self):
        ss = load_seed()
        ss["vertices"]["V20"] = {"role": "residue", "label": "Extra residue"}
        results = make_results(stream_results={
            "V0":  {"CO2_molfrac": 0.15, "CO2_moleflow": 1.0,  "description": "Feed"},
            "V8":  {"CO2_molfrac": 0.05, "CO2_moleflow": 0.20, "description": "Residue"},
            "V20": {"CO2_molfrac": 0.05, "CO2_moleflow": 0.10, "description": "Extra residue"},
        })
        losses = S.extract_residue_losses(results, ss)
        vids = sorted(l.vid for l in losses)
        self.assertEqual(vids, ["V20", "V8"])
        # share = mf / feed
        total_share = sum(l.share_of_feed_co2 for l in losses)
        self.assertAlmostEqual(total_share, 0.30)

    def test_missing_stream_skipped(self):
        ss = load_seed()
        results = make_results(stream_results={
            "V0": {"CO2_molfrac": 0.15, "CO2_moleflow": 1.0, "description": "Feed"},
            # V8 が無い
        })
        losses = S.extract_residue_losses(results, ss)
        self.assertEqual(losses, [])

    def test_no_feed_co2_yields_zero_share(self):
        ss = load_seed()
        results = make_results(stream_results={
            "V0": {"CO2_molfrac": 0.0, "CO2_moleflow": 0.0, "description": "Feed"},
            "V8": {"CO2_molfrac": 0.0, "CO2_moleflow": 0.0, "description": "Residue"},
        })
        losses = S.extract_residue_losses(results, ss)
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0].share_of_feed_co2, 0.0)


# =========================================================
# constraint_violation
# =========================================================

class TestConstraintViolation(unittest.TestCase):

    def test_both_violated(self):
        case = make_case(purity_min=0.9, recovery_min=0.7)
        results = make_results(purity=0.514, recovery=0.267)
        cv = S.extract_constraint_violation(results, case)
        self.assertAlmostEqual(cv.purity_short, 0.386, places=3)
        self.assertAlmostEqual(cv.recovery_short, 0.433, places=3)
        self.assertTrue(cv.is_violating)

    def test_both_met(self):
        case = make_case(purity_min=0.9, recovery_min=0.7)
        results = make_results(purity=0.95, recovery=0.80)
        cv = S.extract_constraint_violation(results, case)
        self.assertEqual(cv.purity_short, 0.0)
        self.assertEqual(cv.recovery_short, 0.0)
        self.assertFalse(cv.is_violating)

    def test_only_purity_violated(self):
        case = make_case(purity_min=0.9, recovery_min=0.7)
        results = make_results(purity=0.85, recovery=0.75)
        cv = S.extract_constraint_violation(results, case)
        self.assertAlmostEqual(cv.purity_short, 0.05)
        self.assertEqual(cv.recovery_short, 0.0)
        self.assertTrue(cv.is_violating)


# =========================================================
# extract end-to-end
# =========================================================

class TestExtractAggregator(unittest.TestCase):

    def test_extract_returns_populated_signals(self):
        ss = load_seed()
        case = make_case()
        results = make_results(
            iteration=3,
            purity=0.514, recovery=0.267, spec_e=290.3,
            optimal_params={
                "MEMB1_area": 200000.0, "MEMB1_p_perm": 0.5,
                "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.01,  # 下限張り付き
            },
            energy_breakdown={"VP1": 146.0, "VP2": 364.0},      # VP2 dominant
            stream_results={
                "V0": {"CO2_molfrac": 0.15, "CO2_moleflow": 1.0, "description": "Feed"},
                "V8": {"CO2_molfrac": 0.05, "CO2_moleflow": 0.30, "description": "Residue"},
            },
            active_candidates={"q_1": 1, "q_2": 0},
        )
        sig = S.extract(results, ss, case)
        self.assertEqual(sig.iteration, 3)
        self.assertAlmostEqual(sig.specific_energy, 290.3)
        self.assertEqual([h.name for h in sig.bounds_hit], ["MEMB2_p_perm"])
        self.assertEqual([b.block for b in sig.energy_blocks], ["VP2", "VP1"])
        self.assertTrue(sig.energy_blocks[0].dominant)
        self.assertEqual(len(sig.residue_losses), 1)
        self.assertTrue(sig.constraint_violation.is_violating)
        self.assertEqual(sig.candidate_status, {"q_1": 1, "q_2": 0})

    def test_summarize_returns_nonempty_string(self):
        ss = load_seed()
        case = make_case()
        results = make_results(
            optimal_params={"MEMB1_area": 200000.0, "MEMB1_p_perm": 0.5,
                            "MEMB2_area": 200000.0, "MEMB2_p_perm": 0.5},
            energy_breakdown={"VP1": 100.0, "VP2": 100.0},
        )
        sig = S.extract(results, ss, case)
        text = S.summarize(sig)
        # 主要セクションが出力されているか
        for marker in ["Signals", "Performance:", "Energy blocks", "Bounds-hit", "Residue CO2 loss", "Candidate status"]:
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
