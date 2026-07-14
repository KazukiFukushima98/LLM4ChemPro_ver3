"""topology.py + unit_registry.py の単体テスト（Aspen 不要）。

実行:
    uv run python -m unittest algorithm.tests.test_topology
または
    uv run python -m unittest discover -s algorithm/tests
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import topology as T  # noqa: E402
from topology import TopologyError  # noqa: E402
import unit_registry as UR  # noqa: E402


SEED_PATH = os.path.normpath(os.path.join(HERE, "..", "ss_seed.json"))


# =========================================================
# Helpers
# =========================================================

def make_minimal_ss() -> dict:
    """最小の妥当な SS（feed→MEMB→product/residue）。"""
    return {
        "iteration": 0,
        "vertices": {
            "V0": {"role": "feed",     "label": "Feed"},
            "V1": {"role": "internal", "label": "MEMB1 inlet"},
            "V2": {"role": "internal", "label": "MEMB1 permeate"},
            "V3": {"role": "internal", "label": "MEMB1 retentate"},
            "V4": {"role": "product",  "label": "Product"},
            "V5": {"role": "residue",  "label": "Residue"},
        },
        "arcs": {
            ("V0", "V1"): {"type": "feed"},
            ("V1", "V2"): {"type": "membrane_permeate",  "unit": "MEMB1"},
            ("V1", "V3"): {"type": "membrane_retentate", "unit": "MEMB1"},
            ("V2", "V4"): {"type": "product"},
            ("V3", "V5"): {"type": "residue"},
        },
        "units": {
            "MEMB1": {
                "type": "MEMB",
                "inlet": "V1",
                "outlets": {"permeate": "V2", "retentate": "V3"},
                "params": {"area": 1000.0, "p_permeate": 0.2},
            },
        },
        "history": [],
    }


# =========================================================
# ID utilities
# =========================================================

class TestIdAllocation(unittest.TestCase):

    def test_next_vertex_id_empty(self):
        self.assertEqual(T.next_vertex_id({}), "V0")

    def test_next_vertex_id_dense(self):
        vertices = {f"V{i}": {} for i in range(9)}
        self.assertEqual(T.next_vertex_id(vertices), "V9")

    def test_next_vertex_id_sparse(self):
        # 飛び番（V9, V10 を削除して V11 が来るような状況）
        vertices = {"V0": {}, "V1": {}, "V5": {}, "V8": {}}
        self.assertEqual(T.next_vertex_id(vertices), "V9")

    def test_next_vertex_id_after_delete_and_add(self):
        # max+1 が肝。個数+1 だと衝突する。
        vertices = {"V0": {}, "V1": {}, "V12": {}}
        self.assertEqual(T.next_vertex_id(vertices), "V13")

    def test_next_candidate_id_empty(self):
        self.assertEqual(T.next_candidate_id({}), "q_1")

    def test_next_candidate_id_max_plus_one(self):
        arcs = {
            ("V0", "V1"): {"type": "feed"},
            ("V1", "V2"): {"type": "recycle", "candidate": "q_3"},
            ("V2", "V3"): {"type": "recycle", "candidate": "q_7"},
        }
        self.assertEqual(T.next_candidate_id(arcs), "q_8")

    def test_invalid_ids(self):
        with self.assertRaises(TopologyError):
            T._vid_num("X3")
        with self.assertRaises(TopologyError):
            T._vid_num("Vfoo")
        with self.assertRaises(TopologyError):
            T._qid_num("q_foo")


# =========================================================
# Load / Save round-trip
# =========================================================

class TestLoadSaveRoundtrip(unittest.TestCase):

    def test_roundtrip_seed(self):
        """ss_seed.json を読んで保存して読み直しても等価。"""
        if not os.path.exists(SEED_PATH):
            self.skipTest(f"seed not found at {SEED_PATH}")
        ss = T.load_ss(SEED_PATH)
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.json")
            T.save_ss(ss, out)
            ss2 = T.load_ss(out)
        self.assertEqual(ss["vertices"], ss2["vertices"])
        self.assertEqual(ss["arcs"], ss2["arcs"])
        self.assertEqual(ss["units"], ss2["units"])
        self.assertEqual(ss["iteration"], ss2["iteration"])

    def test_arc_tuple_keys(self):
        ss = make_minimal_ss()
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.json")
            T.save_ss(ss, out)
            with open(out, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # JSON 上では list 形式
            self.assertIsInstance(raw["arcs"], list)
            # from/to が外出しされている
            for arc in raw["arcs"]:
                self.assertIn("from", arc)
                self.assertIn("to", arc)
            # ロード後はタプルキー辞書
            ss2 = T.load_ss(out)
            self.assertIsInstance(ss2["arcs"], dict)
            self.assertIn(("V0", "V1"), ss2["arcs"])

    def test_save_orders_arcs_numerically(self):
        # 並びが (V0,V1), (V1,V2), (V2,V4) ... と数値順になる
        ss = make_minimal_ss()
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.json")
            T.save_ss(ss, out)
            with open(out, "r", encoding="utf-8") as f:
                raw = json.load(f)
        keys = [(a["from"], a["to"]) for a in raw["arcs"]]
        # 数値ソートされている
        nums = [(int(f[1:]), int(t[1:])) for f, t in keys]
        self.assertEqual(nums, sorted(nums))


# =========================================================
# active_topology
# =========================================================

class TestActiveTopology(unittest.TestCase):

    def setUp(self):
        ss = make_minimal_ss()
        # 候補アーク（V3→V1 のリサイクル）を追加
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_1"}
        self.ss = ss

    def test_candidate_off_is_excluded(self):
        topo = T.active_topology(self.ss, {"q_1": 0})
        self.assertNotIn(("V3", "V1"), topo["arcs"])
        # 固定アークは残る
        self.assertIn(("V0", "V1"), topo["arcs"])

    def test_candidate_on_is_included_without_label(self):
        topo = T.active_topology(self.ss, {"q_1": 1})
        self.assertIn(("V3", "V1"), topo["arcs"])
        # 解決後は candidate キーが消える
        self.assertNotIn("candidate", topo["arcs"][("V3", "V1")])
        # 他のメタは残る
        self.assertEqual(topo["arcs"][("V3", "V1")]["type"], "recycle")

    def test_default_q_is_zero(self):
        topo = T.active_topology(self.ss)  # q_active 省略
        self.assertNotIn(("V3", "V1"), topo["arcs"])

    def test_units_are_carried_over(self):
        topo = T.active_topology(self.ss, {"q_1": 0})
        self.assertIn("MEMB1", topo["units"])
        # deepcopy なので原本を汚さない
        topo["units"]["MEMB1"]["params"]["area"] = 99999.0
        self.assertNotEqual(self.ss["units"]["MEMB1"]["params"]["area"], 99999.0)


# =========================================================
# dead-unit pruning（給餌ゼロ unit の連鎖除去）
# =========================================================

def make_bypass_toggle_ss() -> dict:
    """MEMB1 permeate を「MEMB2 へ ⇄ product へバイパス」のトグル候補ペアにした SS。

    q_1 ON  : (V2→V6) で MEMB2 へ給餌（2段目あり）
    q_2 ON  : (V2→V4) で product へ直行（MEMB2 バイパス＝給餌ゼロ）
    相互排他（両 ON は is_buildable が V2 out-degree 2 で弾く）。
    """
    return {
        "iteration": 0,
        "vertices": {
            "V0": {"role": "feed",     "label": "Feed"},
            "V1": {"role": "internal", "label": "MEMB1 inlet"},
            "V2": {"role": "internal", "label": "MEMB1 permeate"},
            "V3": {"role": "internal", "label": "MEMB1 retentate"},
            "V4": {"role": "product",  "label": "Product"},
            "V5": {"role": "residue",  "label": "Residue"},
            "V6": {"role": "internal", "label": "MEMB2 inlet"},
            "V7": {"role": "internal", "label": "MEMB2 permeate"},
            "V8": {"role": "internal", "label": "MEMB2 retentate"},
        },
        "arcs": {
            ("V0", "V1"): {"type": "feed"},
            ("V1", "V2"): {"type": "membrane_permeate",  "unit": "MEMB1"},
            ("V1", "V3"): {"type": "membrane_retentate", "unit": "MEMB1"},
            ("V3", "V5"): {"type": "residue"},
            ("V2", "V6"): {"type": "process", "candidate": "q_1"},
            ("V2", "V4"): {"type": "product", "candidate": "q_2"},
            ("V6", "V7"): {"type": "membrane_permeate",  "unit": "MEMB2"},
            ("V6", "V8"): {"type": "membrane_retentate", "unit": "MEMB2"},
            ("V7", "V4"): {"type": "product"},
            ("V8", "V5"): {"type": "residue"},
        },
        "units": {
            "MEMB1": {
                "type": "MEMB", "inlet": "V1",
                "outlets": {"permeate": "V2", "retentate": "V3"},
                "params": {"area": 1000.0, "p_permeate": 0.2},
            },
            "MEMB2": {
                "type": "MEMB", "inlet": "V6",
                "outlets": {"permeate": "V7", "retentate": "V8"},
                "params": {"area": 1000.0, "p_permeate": 0.2},
            },
        },
        "history": [],
    }


class TestPruneDeadUnits(unittest.TestCase):

    def test_unfed_membrane_is_pruned(self):
        # バイパス ON（給餌 OFF）→ MEMB2 とその所有頂点・アークが消える
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        for v in ("V6", "V7", "V8"):
            self.assertNotIn(v, topo["vertices"])
        for k in topo["arcs"]:
            self.assertNotIn("V6", k)
            self.assertNotIn("V7", k)
            self.assertNotIn("V8", k)
        # バイパスアークは残る（permeate→product 直行）
        self.assertIn(("V2", "V4"), topo["arcs"])
        # 上流 MEMB1 と sink は無傷
        self.assertIn("MEMB1", topo["units"])
        self.assertIn("V5", topo["vertices"])

    def test_pruned_topology_is_buildable(self):
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 0, "q_2": 1})
        self.assertIsNone(T.is_buildable(topo))

    def test_fed_membrane_is_kept(self):
        # 給餌 ON（バイパス OFF）→ MEMB2 は残る・偽陽性なし
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 1, "q_2": 0})
        self.assertIn("MEMB2", topo["units"])
        for v in ("V6", "V7", "V8"):
            self.assertIn(v, topo["vertices"])
        self.assertIn(("V2", "V6"), topo["arcs"])
        self.assertIsNone(T.is_buildable(topo))

    def test_fixed_arc_fed_membrane_never_pruned(self):
        # 候補を一切持たない固定構成の膜は q に関係なく刈られない
        topo = T.active_topology(make_minimal_ss(), {})
        self.assertIn("MEMB1", topo["units"])
        self.assertIn("V1", topo["vertices"])

    def test_series_membrane_cascade_prune(self):
        # MEMB2 → MEMB3 の直列。MEMB2 を給餌断すると MEMB3 も fixpoint で連鎖除去。
        ss = make_bypass_toggle_ss()
        # MEMB2 permeate V7 を MEMB3 へ直列接続（固定アーク）
        ss["vertices"]["V9"]  = {"role": "internal", "label": "MEMB3 inlet"}
        ss["vertices"]["V10"] = {"role": "internal", "label": "MEMB3 permeate"}
        ss["vertices"]["V11"] = {"role": "internal", "label": "MEMB3 retentate"}
        # V7→V4(product) を V7→V9(MEMB3 へ) に差し替え
        del ss["arcs"][("V7", "V4")]
        ss["arcs"][("V7", "V9")]  = {"type": "process"}
        ss["arcs"][("V9", "V10")] = {"type": "membrane_permeate",  "unit": "MEMB3"}
        ss["arcs"][("V9", "V11")] = {"type": "membrane_retentate", "unit": "MEMB3"}
        ss["arcs"][("V10", "V4")] = {"type": "product"}
        ss["arcs"][("V11", "V5")] = {"type": "residue"}
        ss["units"]["MEMB3"] = {
            "type": "MEMB", "inlet": "V9",
            "outlets": {"permeate": "V10", "retentate": "V11"},
            "params": {"area": 1000.0, "p_permeate": 0.2},
        }
        # 給餌 OFF → MEMB2 dead → V7 消滅 → MEMB3 inlet V9 入次数0 → MEMB3 も dead
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertNotIn("MEMB3", topo["units"])
        for v in ("V6", "V7", "V8", "V9", "V10", "V11"):
            self.assertNotIn(v, topo["vertices"])

    def test_series_membrane_kept_when_fed(self):
        # 同じ直列構成でも給餌 ON なら両膜とも残る（連鎖刈りの偽陽性なし）
        ss = make_bypass_toggle_ss()
        ss["vertices"]["V9"]  = {"role": "internal", "label": "MEMB3 inlet"}
        ss["vertices"]["V10"] = {"role": "internal", "label": "MEMB3 permeate"}
        ss["vertices"]["V11"] = {"role": "internal", "label": "MEMB3 retentate"}
        del ss["arcs"][("V7", "V4")]
        ss["arcs"][("V7", "V9")]  = {"type": "process"}
        ss["arcs"][("V9", "V10")] = {"type": "membrane_permeate",  "unit": "MEMB3"}
        ss["arcs"][("V9", "V11")] = {"type": "membrane_retentate", "unit": "MEMB3"}
        ss["arcs"][("V10", "V4")] = {"type": "product"}
        ss["arcs"][("V11", "V5")] = {"type": "residue"}
        ss["units"]["MEMB3"] = {
            "type": "MEMB", "inlet": "V9",
            "outlets": {"permeate": "V10", "retentate": "V11"},
            "params": {"area": 1000.0, "p_permeate": 0.2},
        }
        topo = T.active_topology(ss, {"q_1": 1, "q_2": 0})
        self.assertIn("MEMB2", topo["units"])
        self.assertIn("MEMB3", topo["units"])


# =========================================================
# is_buildable（非膜の分流ガード）
# =========================================================

class TestIsBuildable(unittest.TestCase):

    def test_minimal_concrete_is_buildable(self):
        """膜 inlet は出次数2＝両ポート所有、他頂点 ≤1 → None。"""
        topo = T.active_topology(make_minimal_ss())
        self.assertIsNone(T.is_buildable(topo))

    def test_plain_vertex_split_unbuildable(self):
        """plain 頂点（V2）から plain アーク2本＝出次数2 → 非 None。"""
        topo = T.active_topology(make_minimal_ss())
        # V2(MEMB1 permeate, plain) から余分な出アークを足して出次数2 にする
        topo["arcs"][("V2", "V5")] = {"type": "process"}
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("V2", reason)

    def test_membrane_inlet_extra_arc_unbuildable(self):
        """膜 inlet（V1）に余分な plain 出アークを足し出次数3 → 非 None。"""
        topo = T.active_topology(make_minimal_ss())
        topo["arcs"][("V1", "V4")] = {"type": "process"}
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("V1", reason)

    def test_non_inlet_vertex_outdegree_two_unbuildable(self):
        """膜 inlet でない頂点（V0=feed）の出次数2 → 非 None。"""
        topo = T.active_topology(make_minimal_ss())
        topo["arcs"][("V0", "V3")] = {"type": "process"}
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("V0", reason)


# =========================================================
# Validate
# =========================================================

class TestValidate(unittest.TestCase):

    def test_minimal_ss_is_valid(self):
        T.validate(make_minimal_ss())  # no raise

    def test_seed_is_valid(self):
        if not os.path.exists(SEED_PATH):
            self.skipTest("seed not found")
        T.validate(T.load_ss(SEED_PATH))

    def test_unknown_endpoint_raises(self):
        ss = make_minimal_ss()
        ss["arcs"][("V1", "V99")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "unknown vertex"):
            T.validate(ss)

    def test_feed_with_incoming_raises(self):
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V0")] = {"type": "recycle"}
        with self.assertRaisesRegex(TopologyError, "source vertex"):
            T.validate(ss)

    def test_product_with_outgoing_raises(self):
        ss = make_minimal_ss()
        ss["arcs"][("V4", "V5")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "sink vertex"):
            T.validate(ss)

    def test_no_path_feed_to_product_raises(self):
        ss = make_minimal_ss()
        # V2→V4 を切る（product が孤立化）
        del ss["arcs"][("V2", "V4")]
        with self.assertRaisesRegex(TopologyError, "feed.*product"):
            T.validate(ss)

    def test_isolated_internal_vertex_raises(self):
        ss = make_minimal_ss()
        ss["vertices"]["V7"] = {"role": "internal", "label": "floating"}
        with self.assertRaisesRegex(TopologyError, "isolated"):
            T.validate(ss)

    def test_unit_inlet_missing_raises(self):
        ss = make_minimal_ss()
        ss["units"]["MEMB1"]["inlet"] = "V99"
        with self.assertRaisesRegex(TopologyError, "inlet"):
            T.validate(ss)

    def test_arc_unit_unknown_raises(self):
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V4")] = {"type": "process", "unit": "GHOST"}
        with self.assertRaisesRegex(TopologyError, "unknown unit"):
            T.validate(ss)

    def test_unknown_role_raises(self):
        ss = make_minimal_ss()
        ss["vertices"]["V0"]["role"] = "source"  # not in known roles
        with self.assertRaisesRegex(TopologyError, "unknown role"):
            T.validate(ss)

    def test_internal_reachable_from_feed_but_no_sink_raises(self):
        """項目8: feed から到達可能な internal が sink に届かないと弾く。"""
        ss = make_minimal_ss()
        # V0(feed) から V6(internal) へ分岐、V6 は行き先なし（dead end）
        ss["vertices"]["V6"] = {"role": "internal", "label": "dead end"}
        ss["arcs"][("V0", "V6")] = {"type": "process"}
        # V6 は in_deg=1, out_deg=0 → 既存の isolated 検査は通る（孤立ではない）
        # しかし sink に到達できない → 新ルールが弾くべき
        with self.assertRaisesRegex(TopologyError, "cannot reach any sink"):
            T.validate(ss)

    def test_internal_reachable_from_feed_through_chain_to_no_sink_raises(self):
        """項目8: 2段先で行き止まりになるケースも弾く。"""
        ss = make_minimal_ss()
        ss["vertices"]["V6"] = {"role": "internal", "label": "chain mid"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "chain end"}
        ss["arcs"][("V0", "V6")] = {"type": "process"}
        ss["arcs"][("V6", "V7")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "cannot reach any sink"):
            T.validate(ss)

    def test_unreachable_dead_end_internal_is_allowed(self):
        """項目8 は feed から到達できない頂点には適用しない。
        （孤立ではないが feed から届かない部分グラフは別途 GA の BAD_VALUE 任せ）。"""
        ss = make_minimal_ss()
        # V0 から到達不能な「島」を作る：V6→V7 で V6 への入アークは無い
        ss["vertices"]["V6"] = {"role": "internal", "label": "island src"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "island dst"}
        ss["arcs"][("V6", "V7")] = {"type": "process"}
        # V6 / V7 は孤立ではなく (in_deg=0,out_deg=1) / (in_deg=1,out_deg=0)
        # feed から到達不能なので項目8 は適用されない → validate は通る
        T.validate(ss)  # no raise


# =========================================================
# Auxiliary derivation
# =========================================================

class TestMixerSplitterVps(unittest.TestCase):

    def test_mixer_at_membrane_inlet(self):
        ss = make_minimal_ss()
        mixers = T.mixer_vertices(ss)
        self.assertIn("V1", mixers)  # 膜入口

    def test_mixer_at_sinks(self):
        ss = make_minimal_ss()
        mixers = T.mixer_vertices(ss)
        self.assertIn("V4", mixers)  # product
        self.assertIn("V5", mixers)  # residue

    def test_mixer_at_high_in_degree(self):
        ss = make_minimal_ss()
        # V1 の入次数を上げる：もう1本入れる
        ss["vertices"]["V6"] = {"role": "internal", "label": "extra"}
        ss["arcs"][("V2", "V6")] = {"type": "process"}
        ss["arcs"][("V6", "V1")] = {"type": "recycle"}
        mixers = T.mixer_vertices(ss)
        self.assertIn("V1", mixers)

    def test_mixer_at_noop_fed_unit_inlet(self):
        """規則(4): 素通しアークで給餌される非膜ユニット入口は Mixer になる。

        pre-mixer 配置（feed → V6 → COMP1 → V7 → 膜入口）で、V6 を Mixer 化
        しないと builder が入口ストリームを生成せず孤立する（2026-07-14 の
        新 seed 全評価 silent BAD の原因）。
        """
        ss = make_minimal_ss()
        ss["vertices"]["V6"] = {"role": "internal", "label": "COMP1 inlet (pre-mixer)"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "COMP1 outlet"}
        del ss["arcs"][("V0", "V1")]
        ss["arcs"][("V0", "V6")] = {"type": "feed"}
        ss["arcs"][("V6", "V7")] = {"type": "compressor", "unit": "COMP1"}
        ss["arcs"][("V7", "V1")] = {"type": "process"}
        ss["units"]["COMP1"] = {
            "type": "COMP", "inlet": "V6", "outlets": {"outlet": "V7"},
            "params": {"outlet_pressure": 2.5},
        }
        mixers = T.mixer_vertices(ss)
        self.assertIn("V6", mixers)   # 規則(4): 素通し（feed）給餌のユニット入口
        self.assertIn("V1", mixers)   # 規則(1): 膜入口は従来どおり

    def test_no_mixer_at_unit_arc_fed_inlet(self):
        """規則(4)の非対象: unit アークで直接給餌される入口は Mixer にしない（既存配線を変えない）。

        例: 膜 permeate 出口 V2 をそのまま COMP2 の入口にする従来パターン
        （ストリーム V2 は膜ブロックが生成するので Mixer 不要）。
        """
        ss = make_minimal_ss()
        ss["vertices"]["V6"] = {"role": "internal", "label": "COMP2 outlet"}
        del ss["arcs"][("V2", "V4")]
        ss["arcs"][("V2", "V6")] = {"type": "compressor", "unit": "COMP2"}
        ss["arcs"][("V6", "V4")] = {"type": "process"}
        ss["units"]["COMP2"] = {
            "type": "COMP", "inlet": "V2", "outlets": {"outlet": "V6"},
            "params": {"outlet_pressure": 2.5},
        }
        mixers = T.mixer_vertices(ss)
        self.assertNotIn("V2", mixers)  # unit アーク給餌 → 従来どおり Mixer なし

    def test_splitter_when_out_degree_two(self):
        ss = make_minimal_ss()
        # V1 から MEMB の 2本（permeate / retentate）が出ているので splitter になる
        splitters = T.splitter_vertices(ss)
        self.assertIn("V1", splitters)

    def test_auto_vps_match_membrane_numbers(self):
        ss = make_minimal_ss()
        # MEMB1 だけ
        vps = T.auto_vps(ss)
        self.assertEqual(vps, {"MEMB1": "VP1"})

    def test_auto_vps_with_gaps(self):
        ss = make_minimal_ss()
        # MEMB2 を削除して MEMB3 だけ残した想定
        ss["units"]["MEMB3"] = {
            "type": "MEMB", "inlet": "V1",
            "outlets": {"permeate": "V2", "retentate": "V3"},
            "params": {"area": 1.0, "p_permeate": 0.5},
        }
        # ↑構造としては不整合（同じ V2/V3 を別 unit が共有）だが、auto_vps の名前生成のテスト目的
        vps = T.auto_vps(ss)
        self.assertEqual(vps, {"MEMB1": "VP1", "MEMB3": "VP3"})


# =========================================================
# Variable derivation
# =========================================================

class TestVariableDerivation(unittest.TestCase):

    def test_binary_variables_sorted_by_q_num(self):
        ss = make_minimal_ss()
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_3"}
        ss["arcs"][("V2", "V0")] = {"type": "recycle", "candidate": "q_1"}
        # ↑ feed への入アークは validate で弾かれるが、binary_variables 単体テストなので問題なし
        ss["arcs"][("V3", "V4")] = {"type": "recycle", "candidate": "q_10"}
        bvs = T.binary_variables(ss)
        self.assertEqual([b["name"] for b in bvs], ["q_1", "q_3", "q_10"])

    def test_binary_variables_empty(self):
        self.assertEqual(T.binary_variables(make_minimal_ss()), [])

    def test_binary_variables_duplicate_raises(self):
        ss = make_minimal_ss()
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_1"}
        ss["arcs"][("V2", "V0")] = {"type": "recycle", "candidate": "q_1"}
        with self.assertRaisesRegex(TopologyError, "duplicate"):
            T.binary_variables(ss)

    def test_continuous_variables_from_units(self):
        ss = make_minimal_ss()
        cvs = T.continuous_variables(ss)
        names = [cv["name"] for cv in cvs]
        # MEMB1 → area, p_perm の順
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm"])

    def test_continuous_variables_order_follows_unit_insertion(self):
        ss = make_minimal_ss()
        # MEMB1 のあとに MEMB2 を追加
        ss["vertices"]["V6"] = {"role": "internal", "label": ""}
        ss["vertices"]["V7"] = {"role": "internal", "label": ""}
        ss["units"]["MEMB2"] = {
            "type": "MEMB", "inlet": "V2",
            "outlets": {"permeate": "V6", "retentate": "V7"},
            "params": {"area": 1.0, "p_permeate": 0.5},
        }
        names = [cv["name"] for cv in T.continuous_variables(ss)]
        # MEMB1 が先、MEMB2 が後
        self.assertEqual(
            names,
            ["MEMB1_area", "MEMB1_p_perm", "MEMB2_area", "MEMB2_p_perm"],
        )

    def test_bounds_override_takes_precedence(self):
        ss = make_minimal_ss()
        # area の境界を上書き
        ss["units"]["MEMB1"]["bounds_override"] = {"area": [50.0, 200.0]}
        cvs = T.continuous_variables(ss)
        cv_area = next(cv for cv in cvs if cv["name"] == "MEMB1_area")
        self.assertEqual(cv_area["bounds"], [50.0, 200.0])
        # 上書きしていない p_permeate は既定が使われる
        cv_pp = next(cv for cv in cvs if cv["name"] == "MEMB1_p_perm")
        self.assertEqual(cv_pp["bounds"], UR.UNIT_BOUNDS["MEMB"]["p_permeate"])


# =========================================================
# x_for_topology（pruning 後トポロジーへの連続 x の整列）
# =========================================================

class TestXForTopology(unittest.TestCase):

    def _cv(self, unit: str, param: str) -> dict:
        return {"name": f"{unit}_{param}", "unit_param": [unit, param], "bounds": [0.0, 1.0]}

    def test_middle_unit_pruned_keeps_alignment(self):
        """途中のユニットが prune されても、後続ユニットの値が位置ずれしない。"""
        cont_vars = [
            self._cv("MEMB1", "area"), self._cv("MEMB1", "p_permeate"),
            self._cv("MEMB2", "area"), self._cv("MEMB2", "p_permeate"),
            self._cv("MEMB3", "area"), self._cv("MEMB3", "p_permeate"),
        ]
        # MEMB2 だけ prune された具体トポロジー
        topology = {"units": {"MEMB1": {}, "MEMB3": {}}}
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        # MEMB2 の (3,4) が落ち、MEMB3 に (5,6) が正しく残る
        self.assertEqual(T.x_for_topology(x, cont_vars, topology), [1.0, 2.0, 5.0, 6.0])

    def test_wildcard_kept_iff_prefix_unit_survives(self):
        """"MEMB*"（tie 共有変数）は膜が1つでも残っていれば保持、全滅なら落ちる。"""
        cont_vars = [
            {"name": "MEMB_perm", "unit_param": ["MEMB*", "permeance_CO2"], "bounds": [1.0, 16.0]},
            self._cv("MEMB1", "area"),
            self._cv("COMP1", "outlet_pressure"),
        ]
        x = [10.0, 20.0, 30.0]
        with_memb = {"units": {"MEMB1": {}, "COMP1": {}}}
        self.assertEqual(T.x_for_topology(x, cont_vars, with_memb), [10.0, 20.0, 30.0])
        no_memb = {"units": {"COMP1": {}}}
        self.assertEqual(T.x_for_topology(x, cont_vars, no_memb), [30.0])

    def test_consistent_with_active_topology_pruning(self):
        """実際の pruning（bypass トグル OFF）後の continuous_variables と 1:1 に揃う。"""
        ss = make_bypass_toggle_ss()
        template_cvs = T.continuous_variables(ss)
        self.assertEqual(len(template_cvs), 4)  # MEMB1/MEMB2 × (area, p_perm)
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1})  # MEMB2 pruned
        x = [11.0, 0.2, 22.0, 0.3]
        filtered = T.x_for_topology(x, template_cvs, topo)
        topo_cvs = T.continuous_variables(topo)
        self.assertEqual(len(filtered), len(topo_cvs))
        self.assertEqual([cv["name"] for cv in topo_cvs], ["MEMB1_area", "MEMB1_p_perm"])
        self.assertEqual(filtered, [11.0, 0.2])


# =========================================================
# unit_registry
# =========================================================

class TestUnitRegistry(unittest.TestCase):

    def test_get_unit_type(self):
        self.assertEqual(UR.get_unit_type("MEMB1"), "MEMB")
        self.assertEqual(UR.get_unit_type("MEMB42"), "MEMB")
        self.assertEqual(UR.get_unit_type("COMP3"), "COMP")
        self.assertEqual(UR.get_unit_type("EXP1"), "EXP")
        self.assertEqual(UR.get_unit_type("HEAT1"), "HEAT")   # 冷却器/加熱器（12.4 検算で追加）
        self.assertIsNone(UR.get_unit_type("PUMP1"))

    def test_expander_has_no_ga_variables(self):
        """膨張機（12.4）は GA 変数を持たない構造部品（membrane_model があっても不変）。"""
        self.assertEqual(UR.make_ga_variables("EXP1"), [])
        self.assertEqual(UR.make_ga_variables("EXP1", membrane_model={"tie": False}), [])
        self.assertEqual(UR.get_outlet_ports("EXP"), ["outlet"])

    def test_make_ga_variables_defaults(self):
        vs = UR.make_ga_variables("MEMB1")
        self.assertEqual([v["name"] for v in vs], ["MEMB1_area", "MEMB1_p_perm"])
        self.assertEqual(vs[0]["unit_param"], ["MEMB1", "area"])
        self.assertEqual(vs[0]["bounds"], UR.UNIT_BOUNDS["MEMB"]["area"])

    def test_make_ga_variables_with_override(self):
        unit_data = {"bounds_override": {"area": [10.0, 20.0]}}
        vs = UR.make_ga_variables("MEMB1", unit_data)
        self.assertEqual(vs[0]["bounds"], [10.0, 20.0])
        # p_permeate は既定
        self.assertEqual(vs[1]["bounds"], UR.UNIT_BOUNDS["MEMB"]["p_permeate"])

    def test_make_ga_variables_unknown_unit(self):
        self.assertEqual(UR.make_ga_variables("WAT1"), [])

    def test_outlet_ports(self):
        self.assertEqual(UR.get_outlet_ports("MEMB"), ["permeate", "retentate"])
        self.assertEqual(UR.get_outlet_ports("COMP"), ["outlet"])
        with self.assertRaises(KeyError):
            UR.get_outlet_ports("HEX")


# =========================================================
# Robeson 膜モデル（ver3 12.1）
# =========================================================

class TestRobesonMembraneModel(unittest.TestCase):

    def test_alpha_satisfies_upper_bound_relation(self):
        """α は Q[GPU]·α^n = k を満たす（上界上の点）。"""
        p = 2.70677  # 1000 GPU
        alpha = UR.robeson_alpha(p, {})
        q_gpu = p / UR.GPU_TO_ASPEN
        self.assertAlmostEqual(q_gpu * alpha ** 2.888, 3.0967e8, delta=3.0967e8 * 1e-9)

    def test_alpha_spot_values_match_lee2018(self):
        """Lee 2018 の代表点: 上界上で 3840 GPU ↔ α≈50、1000 GPU ↔ α≈79.6。"""
        c = UR.GPU_TO_ASPEN
        self.assertAlmostEqual(UR.robeson_alpha(3840.0 * c, {}), 50.0, delta=0.5)
        self.assertAlmostEqual(UR.robeson_alpha(1000.0 * c, {}), 79.6, delta=0.5)
        # 高透過ほど低選択（トレードオフの向き）
        self.assertLess(UR.robeson_alpha(6000.0 * c, {}), UR.robeson_alpha(500.0 * c, {}))

    def test_alpha_rejects_nonpositive_permeance(self):
        with self.assertRaises(ValueError):
            UR.robeson_alpha(0.0, {})

    def test_permeance_bounds_converted_to_aspen_units(self):
        lo, hi = UR.permeance_bounds_aspen({})
        self.assertAlmostEqual(lo, 500.0 * UR.GPU_TO_ASPEN, places=9)
        self.assertAlmostEqual(hi, 6000.0 * UR.GPU_TO_ASPEN, places=9)
        # case.yaml 側の GPU 指定が優先される
        lo2, hi2 = UR.permeance_bounds_aspen({"permeance_bounds_gpu": [1000.0, 2000.0]})
        self.assertAlmostEqual(lo2, 1000.0 * UR.GPU_TO_ASPEN, places=9)
        self.assertAlmostEqual(hi2, 2000.0 * UR.GPU_TO_ASPEN, places=9)

    def test_tie_mode_prepends_shared_variable(self):
        """tie=True: 共有 MEMB_perm が先頭に1本。各 MEMB には perm 変数なし。"""
        ss = make_minimal_ss()
        ss["units"]["MEMB2"] = {
            "type": "MEMB", "inlet": "V2",
            "outlets": {"permeate": "V4", "retentate": "V5"},
            "params": {"area": 1.0, "p_permeate": 0.5},
        }
        cvs = T.continuous_variables(ss, {"tie": True})
        names = [cv["name"] for cv in cvs]
        self.assertEqual(
            names,
            ["MEMB_perm", "MEMB1_area", "MEMB1_p_perm", "MEMB2_area", "MEMB2_p_perm"],
        )
        self.assertEqual(cvs[0]["unit_param"], ["MEMB*", "permeance_CO2"])
        self.assertEqual(cvs[0]["bounds"], UR.permeance_bounds_aspen({}))

    def test_untied_mode_adds_per_unit_variable(self):
        """tie=False: 各 MEMB の変数列末尾に {unit}_perm。共有変数は無し。"""
        ss = make_minimal_ss()
        cvs = T.continuous_variables(ss, {"tie": False})
        names = [cv["name"] for cv in cvs]
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm", "MEMB1_perm"])
        self.assertEqual(cvs[2]["unit_param"], ["MEMB1", "permeance_CO2"])

    def test_none_membrane_model_is_backward_compatible(self):
        """membrane_model=None は従来どおり（permeance 変数なし）。"""
        ss = make_minimal_ss()
        names = [cv["name"] for cv in T.continuous_variables(ss)]
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm"])

    def test_tie_variable_absent_without_membrane_units(self):
        """膜ゼロの SS では tie 共有変数を付けない。"""
        ss = make_minimal_ss()
        ss["units"] = {}
        self.assertEqual(T.continuous_variables(ss, {"tie": True}), [])


# =========================================================
# allocate_*: 採番カウンタ（削除済みIDの再利用禁止・ARCHITECTURE 3.1）
# =========================================================

class TestAllocateIds(unittest.TestCase):

    def test_allocate_vertex_does_not_reuse_deleted_max(self):
        """ARCH 3.1 の明示例: 追加 → 最大番号を削除 → 次の採番は欠番を再利用しない。"""
        ss = make_minimal_ss()  # V0..V5
        v_a = T.allocate_vertex_id(ss)
        self.assertEqual(v_a, "V6")
        ss["vertices"][v_a] = {"role": "internal", "label": "tmp"}
        v_b = T.allocate_vertex_id(ss)
        self.assertEqual(v_b, "V7")
        ss["vertices"][v_b] = {"role": "internal", "label": "tmp"}
        # 最大番号2つを削除しても、次は V8（V6/V7 は永久欠番）
        del ss["vertices"][v_a]
        del ss["vertices"][v_b]
        self.assertEqual(T.allocate_vertex_id(ss), "V8")

    def test_allocate_candidate_does_not_reuse_deleted_max(self):
        ss = make_minimal_ss()
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_3"}
        q_a = T.allocate_candidate_id(ss)
        self.assertEqual(q_a, "q_4")
        ss["arcs"][("V2", "V1")] = {"type": "recycle", "candidate": q_a}
        # 最大番号の候補アークを削除しても q_4 は再利用しない
        del ss["arcs"][("V2", "V1")]
        self.assertEqual(T.allocate_candidate_id(ss), "q_5")

    def test_counters_survive_save_load_roundtrip(self):
        """払い出し履歴（id_counters）が JSON 往復で保持される。"""
        ss = make_minimal_ss()
        self.assertEqual(T.allocate_vertex_id(ss), "V6")   # 払い出しのみ（頂点は追加しない）
        self.assertEqual(T.allocate_candidate_id(ss), "q_1")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ss.json")
            T.save_ss(ss, path)
            ss2 = T.load_ss(path)
        self.assertEqual(T.allocate_vertex_id(ss2), "V7")
        self.assertEqual(T.allocate_candidate_id(ss2), "q_2")

    def test_legacy_ss_without_counters_initialized_from_max(self):
        """id_counters の無い旧形式（ss_seed.json 等）は現存最大から初期化（後方互換）。"""
        if not os.path.exists(SEED_PATH):
            self.skipTest(f"seed not found at {SEED_PATH}")
        ss = T.load_ss(SEED_PATH)  # V0..V10（COMP1 入り seed）、候補なし
        self.assertEqual(T.allocate_vertex_id(ss), "V11")
        self.assertEqual(T.allocate_candidate_id(ss), "q_1")


# =========================================================
# validate: 所有権・自己ループ・候補ラベル（強化分）
# =========================================================

class TestValidateOwnershipAndShape(unittest.TestCase):

    def test_self_loop_rejected(self):
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V2")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "self-loop"):
            T.validate(ss)

    def test_missing_owned_arc_rejected(self):
        """unit の inlet→outlet 所有アークが消えている＝装置構造の破壊を最終ゲートで検出。"""
        ss = make_minimal_ss()
        del ss["arcs"][("V1", "V2")]  # MEMB1 permeate 所有アーク
        with self.assertRaisesRegex(TopologyError, "missing owned arc"):
            T.validate(ss)

    def test_mis_tagged_unit_arc_rejected(self):
        """unit タグ付きアークが inlet→outlet 対でない（誤タグ）を検出。"""
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V4")]["unit"] = "MEMB1"  # 製品アークに MEMB1 タグ
        with self.assertRaisesRegex(TopologyError, "not an inlet"):
            T.validate(ss)

    def test_shared_inlet_rejected(self):
        """2 unit が同じ inlet を共有（is_buildable の辞書が黙って上書きされる形）を検出。"""
        ss = make_minimal_ss()
        ss["units"]["MEMB9"] = {
            "type": "MEMB",
            "inlet": "V1",  # MEMB1 と共有
            "outlets": {"permeate": "V2", "retentate": "V3"},
            "params": {},
        }
        with self.assertRaisesRegex(TopologyError, "share inlet"):
            T.validate(ss)

    def test_owned_arc_as_candidate_rejected(self):
        """所有アーク（装置の内部構造）の候補化は禁止。"""
        ss = make_minimal_ss()
        ss["arcs"][("V1", "V2")]["candidate"] = "q_1"
        with self.assertRaisesRegex(TopologyError, "must not be a candidate"):
            T.validate(ss)

    def test_duplicate_candidate_label_rejected(self):
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V4")]["candidate"] = "q_1"
        ss["arcs"][("V3", "V5")]["candidate"] = "q_1"
        with self.assertRaisesRegex(TopologyError, "duplicate candidate"):
            T.validate(ss)

    def test_serial_units_outlet_as_next_inlet_is_valid(self):
        """A の outlet ＝ B の inlet の直列連結は正常（同ロール間の共有のみ禁止）。"""
        ss = make_minimal_ss()
        # V2（MEMB1 permeate outlet）を inlet とする COMP1 を正規の形で追加
        ss["vertices"]["V6"] = {"role": "internal", "label": "COMP1 outlet"}
        del ss["arcs"][("V2", "V4")]
        ss["arcs"][("V2", "V6")] = {"type": "compressor", "unit": "COMP1"}
        ss["arcs"][("V6", "V4")] = {"type": "product"}
        ss["units"]["COMP1"] = {
            "type": "COMP", "inlet": "V2", "outlets": {"outlet": "V6"}, "params": {},
        }
        T.validate(ss)  # raise しない


# =========================================================
# pruning: feed 到達性・巻き添え切断の検出（強化分）
# =========================================================

class TestPruneReachabilityAndSevered(unittest.TestCase):

    def _ss_with_gated_memb2(self) -> dict:
        """minimal SS に gated MEMB2（給餌 q_1 ⇄ バイパス q_2）を足した SS。"""
        ss = make_minimal_ss()
        # 専用インレット V6、出口 V7(perm)/V8(ret)
        ss["vertices"]["V6"] = {"role": "internal", "label": "MEMB2 inlet (gated)"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "MEMB2 permeate"}
        ss["vertices"]["V8"] = {"role": "internal", "label": "MEMB2 retentate"}
        ss["arcs"][("V6", "V7")] = {"type": "membrane_permeate",  "unit": "MEMB2"}
        ss["arcs"][("V6", "V8")] = {"type": "membrane_retentate", "unit": "MEMB2"}
        ss["arcs"][("V7", "V4")] = {"type": "process"}
        ss["arcs"][("V8", "V5")] = {"type": "process"}
        # トグルペア: 給餌 (V2,V6)=q_1 ⇄ バイパス (V2,V4)=q_2（既存固定を候補化した想定）
        ss["arcs"][("V2", "V6")] = {"type": "process", "candidate": "q_1"}
        ss["arcs"][("V2", "V4")]["candidate"] = "q_2"
        ss["units"]["MEMB2"] = {
            "type": "MEMB", "inlet": "V6",
            "outlets": {"permeate": "V7", "retentate": "V8"}, "params": {},
        }
        return ss

    def test_unreachable_self_recycle_island_is_pruned(self):
        """給餌 OFF ＋ 自己リサイクル ON：入次数は 1 だが feed 非到達 → 島ごと刈る。

        旧実装（入次数 0 のみ発火）では流量ゼロの膜が Aspen に渡っていた。
        """
        ss = self._ss_with_gated_memb2()
        # 自ユニットの retentate 出口から自 inlet へ戻るリサイクル候補
        ss["arcs"][("V8", "V6")] = {"type": "recycle", "candidate": "q_3"}
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1, "q_3": 1})
        self.assertNotIn("MEMB2", topo["units"])
        for vid in ("V6", "V7", "V8"):
            self.assertNotIn(vid, topo["vertices"])
        # 島の内部アークは「生きた流れの切断」ではないので severed 扱いしない
        self.assertNotIn("pruning_severed", topo)
        self.assertIsNone(T.is_buildable(topo))

    def test_severed_live_arc_marks_topology_unbuildable(self):
        """dead unit の outlet へ向かう第三者アークの巻き添え切断を検出して BAD 経路に落とす。

        遺伝子型は「リサイクル ON」なのに実構造からリサイクルが消える
        （静かな遺伝子型⇄表現型のすり替え）ことを防ぐ。
        """
        ss = self._ss_with_gated_memb2()
        # MEMB1 retentate V3 から MEMB2 permeate 出口 V7 へのリサイクル（残渣とのトグルペア）
        ss["arcs"][("V3", "V7")] = {"type": "recycle", "candidate": "q_3"}
        ss["arcs"][("V3", "V5")]["candidate"] = "q_4"  # 既存残渣行きを候補化
        # 給餌 OFF（MEMB2 dead）・リサイクル ON・残渣 OFF
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1, "q_3": 1, "q_4": 0})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertEqual(topo.get("pruning_severed"), [("V3", "V7")])
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("severed", reason)

    def test_normal_gate_off_has_no_severed(self):
        """通常の gated OFF（バイパスのみ ON）は severed なし・buildable。"""
        ss = self._ss_with_gated_memb2()
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertNotIn("pruning_severed", topo)
        self.assertIsNone(T.is_buildable(topo))

    def test_gate_on_keeps_unit(self):
        """給餌 ON（バイパス OFF）ではユニットは残る。"""
        ss = self._ss_with_gated_memb2()
        topo = T.active_topology(ss, {"q_1": 1, "q_2": 0})
        self.assertIn("MEMB2", topo["units"])
        self.assertIsNone(T.is_buildable(topo))


# =========================================================
# to_matrix
# =========================================================

class TestToMatrix(unittest.TestCase):

    def test_matrix_shape_and_values(self):
        ss = make_minimal_ss()
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_1"}
        m, order = T.to_matrix(ss)
        n = len(order)
        self.assertEqual(m.shape, (n, n))
        # 固定アーク
        i0, i1 = order.index("V0"), order.index("V1")
        self.assertEqual(m[i0, i1], 1)
        # 候補アーク
        i3 = order.index("V3")
        self.assertEqual(m[i3, i1], "q_1")
        # 非接続
        i4 = order.index("V4")
        self.assertEqual(m[i0, i4], 0)


if __name__ == "__main__":
    unittest.main()