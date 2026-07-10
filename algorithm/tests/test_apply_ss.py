"""apply_ss.py の単体テスト（Aspen 不要）。

実行:
    uv run python -m unittest algorithm.tests.test_apply_ss
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
import apply_ss as A  # noqa: E402
from apply_ss import ApplyError  # noqa: E402


SEED_PATH = os.path.normpath(os.path.join(HERE, "..", "ss_seed.json"))


def load_seed() -> dict:
    """毎テストで新鮮な seed を読み直す。"""
    return T.load_ss(SEED_PATH)


# =========================================================
# add_unit
# =========================================================

class TestAddUnit(unittest.TestCase):

    def test_add_memb_on_process_arc_replaces_direct_arc(self):
        ss = load_seed()
        change = {
            "reason": "insert MEMB3 between V2 and V4",
            "operations": [
                {
                    "op": "add_unit",
                    "unit_type": "MEMB",
                    "unit": "MEMB3",
                    "inlet": "V2",
                    "permeate_to": "V4",
                    "retentate_to": "V8",
                    "params": {
                        "permeance_CO2": 2.70677,
                        "permeance_N2": 0.0541354,
                        "area": 10000.0,
                        "p_permeate": 0.15,
                    },
                }
            ],
        }
        new_ss = A.apply_change(ss, change)
        # 既存頂点の max=8 → 新規は V9, V10（permeate→retentate のポート順）
        self.assertIn("V9", new_ss["vertices"])
        self.assertIn("V10", new_ss["vertices"])
        # 旧直結アークは削除されている
        self.assertNotIn(("V2", "V4"), new_ss["arcs"])
        # 新しいアーク4本
        self.assertEqual(new_ss["arcs"][("V2", "V9")],
                         {"type": "membrane_permeate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V9", "V4")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V2", "V10")],
                         {"type": "membrane_retentate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V10", "V8")], {"type": "process"})
        # units 登録
        self.assertEqual(new_ss["units"]["MEMB3"]["inlet"], "V2")
        self.assertEqual(new_ss["units"]["MEMB3"]["outlets"],
                         {"permeate": "V9", "retentate": "V10"})
        self.assertEqual(new_ss["units"]["MEMB3"]["type"], "MEMB")
        self.assertEqual(new_ss["units"]["MEMB3"]["params"]["area"], 10000.0)
        # validate 通る
        T.validate(new_ss)

    def test_add_unit_unknown_type_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "FOO", "unit": "FOO1",
             "inlet": "V2", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "unknown unit_type"):
            A.apply_change(ss, change)

    def test_add_unit_existing_name_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB1",
             "inlet": "V2", "permeate_to": "V4", "retentate_to": "V8",
             "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "already exists"):
            A.apply_change(ss, change)

    def test_add_unit_missing_port_field_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V2", "permeate_to": "V4", "params": {}}  # retentate_to 抜け
        ]}
        with self.assertRaisesRegex(ApplyError, "retentate_to"):
            A.apply_change(ss, change)

    def test_missing_operations_key_raises(self):
        """operations が無い ss_change（例: 誤って "changes" キー）は空適用せず明示エラー。

        run24 iter_001 の実事故: エージェントが {"changes": [...]} で書き、旧実装が
        0操作の空適用を黙って成功させた（スキーマガードの回帰テスト）。
        """
        ss = load_seed()
        with self.assertRaisesRegex(ApplyError, "operations"):
            A.apply_change(ss, {"reason": "x", "changes": [
                {"op": "delete_unit", "unit": "MEMB2"}]})

    def test_empty_operations_raises(self):
        ss = load_seed()
        with self.assertRaisesRegex(ApplyError, "operations"):
            A.apply_change(ss, {"reason": "x", "operations": []})

    def test_add_unit_inlet_not_in_vertices_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V99", "permeate_to": "V4", "retentate_to": "V8",
             "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "inlet 'V99'"):
            A.apply_change(ss, change)

    def test_add_comp_two_arcs_per_port(self):
        """COMP も MEMB と同じ2本構成（内部 compressor + 後段 process）。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP1",
             "inlet": "V2", "outlet_to": "V4",
             "params": {"outlet_pressure": 5.0}}
        ]}
        new_ss = A.apply_change(ss, change)
        # 旧直結 (V2,V4) 削除、新頂点 V9 (outlet) 追加
        self.assertNotIn(("V2", "V4"), new_ss["arcs"])
        self.assertIn("V9", new_ss["vertices"])
        self.assertEqual(new_ss["arcs"][("V2", "V9")],
                         {"type": "compressor", "unit": "COMP1"})
        self.assertEqual(new_ss["arcs"][("V9", "V4")], {"type": "process"})
        self.assertEqual(new_ss["units"]["COMP1"]["outlets"], {"outlet": "V9"})
        T.validate(new_ss)

    def test_add_expander_two_arcs_per_port(self):
        """EXP（膨張機・ver3 12.4）も COMP と同型（内部 expander + 後段 process）。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "EXP", "unit": "EXP1",
             "inlet": "V3", "outlet_to": "V8",
             "params": {"outlet_pressure": 1.0}}
        ]}
        new_ss = A.apply_change(ss, change)
        # 旧直結 (V3,V8) residue 削除、新頂点 V9 (outlet) 追加
        self.assertNotIn(("V3", "V8"), new_ss["arcs"])
        self.assertIn("V9", new_ss["vertices"])
        self.assertEqual(new_ss["arcs"][("V3", "V9")],
                         {"type": "expander", "unit": "EXP1"})
        self.assertEqual(new_ss["arcs"][("V9", "V8")], {"type": "process"})
        self.assertEqual(new_ss["units"]["EXP1"]["outlets"], {"outlet": "V9"})
        # GA 変数は増えない（膨張機は変数なしの構造部品）
        names = [cv["name"] for cv in T.continuous_variables(new_ss)]
        self.assertNotIn("EXP1_pout", names)
        T.validate(new_ss)

    def test_add_heater_two_arcs_per_port(self):
        """HEAT（冷却器/加熱器）も COMP と同型（内部 heater + 後段 process）・GA 変数なし。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "HEAT", "unit": "HEAT1",
             "inlet": "V2", "outlet_to": "V4",
             "params": {"temperature": 35.0, "pressure": 0.0}}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertNotIn(("V2", "V4"), new_ss["arcs"])
        self.assertEqual(new_ss["arcs"][("V2", "V9")],
                         {"type": "heater", "unit": "HEAT1"})
        self.assertEqual(new_ss["arcs"][("V9", "V4")], {"type": "process"})
        names = [cv["name"] for cv in T.continuous_variables(new_ss)]
        self.assertEqual(len(names), 4)  # 膜2基×2 のみ（HEAT は変数を持たない）
        T.validate(new_ss)


# =========================================================
# add_gated_unit（GA トグルとしてのユニット追加）
# =========================================================

def _gate_memb3_on_v5() -> dict:
    """seed の MEMB2 permeate(V5)→product(V7) に MEMB3 をトグル追加する change。"""
    return {
        "reason": "gate MEMB3 on MEMB2 permeate (V5), bypass to product V7",
        "operations": [
            {
                "op": "add_gated_unit",
                "unit_type": "MEMB",
                "unit": "MEMB3",
                "feed_from": "V5",
                "bypass_to": "V7",
                "permeate_to": "V7",
                "retentate_to": "V8",
                "params": {
                    "permeance_CO2": 2.70677,
                    "permeance_N2": 0.0541354,
                    "area": 10000.0,
                    "p_permeate": 0.15,
                },
            }
        ],
    }


class TestAddGatedUnit(unittest.TestCase):

    def test_expands_to_toggle_pair(self):
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        # 新インレット V9・出力 V10/V11（max=8 → V9,V10,V11）
        self.assertIn("V9", new_ss["vertices"])
        self.assertIn("V10", new_ss["vertices"])
        self.assertIn("V11", new_ss["vertices"])
        # 膜の内部アーク（固定・unit 所有）
        self.assertEqual(new_ss["arcs"][("V9", "V10")],
                         {"type": "membrane_permeate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V9", "V11")],
                         {"type": "membrane_retentate", "unit": "MEMB3"})
        # 後段アーク
        self.assertEqual(new_ss["arcs"][("V10", "V7")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V11", "V8")], {"type": "process"})
        # 給餌候補（V5→V9）とバイパス候補（V5→V7）がともに candidate
        self.assertIn("candidate", new_ss["arcs"][("V5", "V9")])
        self.assertIn("candidate", new_ss["arcs"][("V5", "V7")])
        # 2つの候補は別ラベル（バイナリ2本）
        self.assertNotEqual(new_ss["arcs"][("V5", "V9")]["candidate"],
                            new_ss["arcs"][("V5", "V7")]["candidate"])
        # バイパスは元の product 型を引き継ぐ
        self.assertEqual(new_ss["arcs"][("V5", "V7")]["type"], "product")
        # ユニット登録
        self.assertEqual(new_ss["units"]["MEMB3"]["inlet"], "V9")
        self.assertEqual(new_ss["units"]["MEMB3"]["outlets"],
                         {"permeate": "V10", "retentate": "V11"})

    def test_result_validates(self):
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        T.validate(new_ss)  # 例外が出なければ OK

    def test_adds_exactly_two_binaries(self):
        ss = load_seed()
        n_before = len(T.binary_variables(ss))
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        self.assertEqual(len(T.binary_variables(new_ss)), n_before + 2)

    def test_gate_off_prunes_membrane_clean(self):
        # 給餌候補 OFF（バイパス ON）→ active_topology の pruning が MEMB3 を刈り取る
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        q_feed = new_ss["arcs"][("V5", "V9")]["candidate"]
        q_byp = new_ss["arcs"][("V5", "V7")]["candidate"]
        topo = T.active_topology(new_ss, {q_feed: 0, q_byp: 1})
        self.assertNotIn("MEMB3", topo["units"])
        for v in ("V9", "V10", "V11"):
            self.assertNotIn(v, topo["vertices"])
        self.assertIn(("V5", "V7"), topo["arcs"])  # バイパス経路は残る
        self.assertIsNone(T.is_buildable(topo))    # クリーン2段

    def test_gate_on_keeps_membrane_buildable(self):
        # 給餌候補 ON（バイパス OFF）→ MEMB3 が建つ・is_buildable OK
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        q_feed = new_ss["arcs"][("V5", "V9")]["candidate"]
        q_byp = new_ss["arcs"][("V5", "V7")]["candidate"]
        topo = T.active_topology(new_ss, {q_feed: 1, q_byp: 0})
        self.assertIn("MEMB3", topo["units"])
        self.assertIn(("V5", "V9"), topo["arcs"])
        self.assertNotIn(("V5", "V7"), topo["arcs"])
        self.assertIsNone(T.is_buildable(topo))

    def test_rejects_unit_owned_bypass(self):
        # bypass_to が unit 所有アークの先（膜内部アーク）の場合は拒否
        ss = load_seed()
        change = {
            "operations": [{
                "op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMBX",
                "feed_from": "V4", "bypass_to": "V5",  # (V4,V5) は MEMB2 所有
                "permeate_to": "V7", "retentate_to": "V8", "params": {},
            }],
        }
        with self.assertRaises(ApplyError):
            A.apply_change(ss, change)

    def test_rejects_duplicate_unit_name(self):
        ss = load_seed()
        change = {
            "operations": [{
                "op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMB2",
                "feed_from": "V5", "bypass_to": "V7",
                "permeate_to": "V7", "retentate_to": "V8", "params": {},
            }],
        }
        with self.assertRaises(ApplyError):
            A.apply_change(ss, change)


# =========================================================
# delete_unit
# =========================================================

class TestDeleteUnit(unittest.TestCase):

    def test_delete_memb2_reroutes_inlet_incoming_to_residue(self):
        ss = load_seed()
        change = {"operations": [{"op": "delete_unit", "unit": "MEMB2"}]}
        new_ss = A.apply_change(ss, change)
        # outlets (V5, V6) は削除
        self.assertNotIn("V5", new_ss["vertices"])
        self.assertNotIn("V6", new_ss["vertices"])
        # 関連アーク全消し
        for k in [("V4", "V5"), ("V4", "V6"), ("V5", "V7"), ("V6", "V8")]:
            self.assertNotIn(k, new_ss["arcs"])
        # inlet V4 は孤立して削除されている
        self.assertNotIn("V4", new_ss["vertices"])
        # 旧 (V2, V4) は (V2, V8) へ振替（type=residue、unit メタは消える）
        self.assertNotIn(("V2", "V4"), new_ss["arcs"])
        self.assertEqual(new_ss["arcs"][("V2", "V8")], {"type": "residue"})
        # units からも消える
        self.assertNotIn("MEMB2", new_ss["units"])
        # この時点では feed→product 経路は失われる → validate は失敗するはず
        with self.assertRaises(TopologyError):
            T.validate(new_ss)

    def test_delete_unit_not_found_raises(self):
        ss = load_seed()
        change = {"operations": [{"op": "delete_unit", "unit": "MEMB99"}]}
        with self.assertRaisesRegex(ApplyError, "MEMB99"):
            A.apply_change(ss, change)

    def test_delete_unit_bounds_override_disappears_with_unit(self):
        ss = load_seed()
        ss["units"]["MEMB2"]["bounds_override"] = {"area": [200.0, 100000.0]}
        change = {"operations": [{"op": "delete_unit", "unit": "MEMB2"}]}
        new_ss = A.apply_change(ss, change)
        self.assertNotIn("MEMB2", new_ss["units"])

    def test_delete_unit_reroute_to_overrides_residue(self):
        ss = load_seed()
        ss["vertices"]["V20"] = {"role": "residue", "label": "alt residue"}
        change = {"operations": [
            {"op": "delete_unit", "unit": "MEMB2", "reroute_to": "V20"}
        ]}
        new_ss = A.apply_change(ss, change)
        # 旧 (V2,V4) は V20 に振替されているはず（既存の V8 ではなく）
        self.assertIn(("V2", "V20"), new_ss["arcs"])
        self.assertNotIn(("V2", "V8"), new_ss["arcs"])

    def test_delete_then_add_as_set(self):
        """delete + add のセット適用後、validate が通る具体例。"""
        ss = load_seed()
        change = {
            "reason": "swap MEMB2 with a direct MEMB3 from V2 to product",
            "operations": [
                {"op": "delete_unit", "unit": "MEMB2"},
                {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
                 "inlet": "V2", "permeate_to": "V7", "retentate_to": "V8",
                 "params": {"permeance_CO2": 2.70677, "permeance_N2": 0.0541354,
                            "area": 20000.0, "p_permeate": 0.1}}
            ],
        }
        new_ss = A.apply_change(ss, change)
        # delete 後の (V2,V8) は add_unit の直結削除で消える
        self.assertNotIn(("V2", "V8"), new_ss["arcs"])
        # MEMB3 が登録され、permeate→V7, retentate→V8 の後段が張られる
        self.assertIn("MEMB3", new_ss["units"])
        # max=8 → V9, V10
        self.assertEqual(new_ss["arcs"][("V9", "V7")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V10", "V8")], {"type": "process"})
        # validate 通る
        T.validate(new_ss)


# =========================================================
# Q3: delete_unit が「生きた経路」を壊さない分岐
# =========================================================

class TestDeleteUnitInletSharing(unittest.TestCase):

    def test_skips_reroute_when_inlet_shared_with_another_unit(self):
        """Q3(2): inlet が他ユニットの inlet にもなっていたら、流入振替・inlet削除を行わない。"""
        # V1 を MEMB1 と MEMB_GHOST の両方の inlet にする（構造としては不自然だが
        # 所有権モデル上ありうる。Q3 ロジックの検証目的）。
        ss = {
            "iteration": 0,
            "vertices": {
                "V0":  {"role": "feed",     "label": "Feed"},
                "V1":  {"role": "internal", "label": "shared inlet"},
                "V2":  {"role": "internal", "label": "MEMB1 perm"},
                "V3":  {"role": "internal", "label": "MEMB1 ret"},
                "V20": {"role": "internal", "label": "GHOST perm"},
                "V21": {"role": "internal", "label": "GHOST ret"},
                "V7":  {"role": "product",  "label": "Product"},
                "V8":  {"role": "residue",  "label": "Residue"},
            },
            "arcs": {
                ("V0", "V1"):   {"type": "feed"},
                ("V1", "V2"):   {"type": "membrane_permeate",  "unit": "MEMB1"},
                ("V1", "V3"):   {"type": "membrane_retentate", "unit": "MEMB1"},
                ("V2", "V7"):   {"type": "product"},
                ("V3", "V8"):   {"type": "residue"},
                ("V1", "V20"):  {"type": "membrane_permeate",  "unit": "MEMB_GHOST"},
                ("V1", "V21"):  {"type": "membrane_retentate", "unit": "MEMB_GHOST"},
                ("V20", "V8"):  {"type": "residue"},
                ("V21", "V8"):  {"type": "residue"},
            },
            "units": {
                "MEMB1": {
                    "type": "MEMB", "inlet": "V1",
                    "outlets": {"permeate": "V2", "retentate": "V3"},
                    "params": {"area": 1000.0, "p_permeate": 0.2},
                },
                "MEMB_GHOST": {
                    "type": "MEMB", "inlet": "V1",
                    "outlets": {"permeate": "V20", "retentate": "V21"},
                    "params": {"area": 1000.0, "p_permeate": 0.2},
                },
            },
            "history": [],
        }
        change = {"operations": [{"op": "delete_unit", "unit": "MEMB1"}]}
        new_ss = A.apply_change(ss, change)

        # MEMB1 の流出側は削除
        self.assertNotIn("V2", new_ss["vertices"])
        self.assertNotIn("V3", new_ss["vertices"])
        self.assertNotIn(("V1", "V2"), new_ss["arcs"])
        self.assertNotIn(("V1", "V3"), new_ss["arcs"])
        self.assertNotIn("MEMB1", new_ss["units"])

        # V1 は MEMB_GHOST の inlet のまま → 残存
        self.assertIn("V1", new_ss["vertices"])
        self.assertIn("MEMB_GHOST", new_ss["units"])
        self.assertEqual(new_ss["units"]["MEMB_GHOST"]["inlet"], "V1")
        # 流入 (V0, V1) はそのまま（feed 型のまま）、residue へ振替されていない
        self.assertEqual(new_ss["arcs"][("V0", "V1")], {"type": "feed"})
        self.assertNotIn(("V0", "V8"), new_ss["arcs"])

    def test_skips_reroute_when_inlet_has_other_outgoing_arc(self):
        """Q3(3): inlet にユニット所有外の outgoing アークがまだ残っていたら触らない。"""
        ss = load_seed()
        # V1 (MEMB1 inlet) から所有外の outgoing を追加。行き先は sink。
        ss["vertices"]["V20"] = {"role": "internal", "label": "bypass"}
        ss["arcs"][("V1", "V20")] = {"type": "process"}
        ss["arcs"][("V20", "V8")] = {"type": "residue"}

        change = {"operations": [{"op": "delete_unit", "unit": "MEMB1"}]}
        new_ss = A.apply_change(ss, change)

        # MEMB1 の流出側は削除
        self.assertNotIn("V2", new_ss["vertices"])
        self.assertNotIn("V3", new_ss["vertices"])
        self.assertNotIn("MEMB1", new_ss["units"])

        # V1 は所有外 outgoing が残っているので残存
        self.assertIn("V1", new_ss["vertices"])
        # 流入 (V0, V1) もそのまま、residue 振替なし
        self.assertEqual(new_ss["arcs"][("V0", "V1")], {"type": "feed"})
        self.assertNotIn(("V0", "V8"), new_ss["arcs"])
        # 所有外 outgoing は維持
        self.assertEqual(new_ss["arcs"][("V1", "V20")], {"type": "process"})


# =========================================================
# add_arc / promote_candidate
# =========================================================

class TestAddArc(unittest.TestCase):

    def test_add_candidate_assigns_q1(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(new_ss["arcs"][("V6", "V4")],
                         {"type": "recycle", "candidate": "q_1"})
        bvars = T.binary_variables(new_ss)
        self.assertEqual([b["name"] for b in bvars], ["q_1"])
        T.validate(new_ss)

    def test_add_second_candidate_uses_max_plus_one(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True},
            {"op": "add_arc", "from": "V3", "to": "V1",
             "type": "recycle", "candidate": True},
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(new_ss["arcs"][("V6", "V4")]["candidate"], "q_1")
        self.assertEqual(new_ss["arcs"][("V3", "V1")]["candidate"], "q_2")

    def test_add_arc_duplicate_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V0", "to": "V1", "type": "feed"}
        ]}
        with self.assertRaisesRegex(ApplyError, "already exists"):
            A.apply_change(ss, change)

    def test_add_arc_unknown_endpoint_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V99", "to": "V1", "type": "process"}
        ]}
        with self.assertRaisesRegex(ApplyError, "V99"):
            A.apply_change(ss, change)


class TestDeleteArc(unittest.TestCase):

    def test_delete_candidate_recycle_arc_drops_binary_variable(self):
        """候補リサイクルアーク（unit=None, candidate=q_1）削除 → アーク消滅・binary が1つ減る。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True},
        ]}
        ss_with_cand = A.apply_change(ss, change)
        self.assertEqual([b["name"] for b in T.binary_variables(ss_with_cand)], ["q_1"])

        change2 = {"operations": [
            {"op": "delete_arc", "from": "V6", "to": "V4"},
        ]}
        new_ss = A.apply_change(ss_with_cand, change2)
        self.assertNotIn(("V6", "V4"), new_ss["arcs"])
        self.assertEqual(T.binary_variables(new_ss), [])
        T.validate(new_ss)

    def test_delete_fixed_non_unit_arc(self):
        """固定の非 unit アーク（candidate なし・unit なし）を削除 → 消える。"""
        ss = load_seed()
        # seed に無い固定アークを1本足してから消す（消すと seed 構造に戻り validate も通る）
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4", "type": "recycle"},
        ]}
        ss_with_arc = A.apply_change(ss, change)
        self.assertEqual(ss_with_arc["arcs"][("V6", "V4")], {"type": "recycle"})

        change2 = {"operations": [
            {"op": "delete_arc", "from": "V6", "to": "V4"},
        ]}
        new_ss = A.apply_change(ss_with_arc, change2)
        self.assertNotIn(("V6", "V4"), new_ss["arcs"])
        T.validate(new_ss)

    def test_delete_unit_owned_arc_rejected(self):
        """unit 所有アーク（膜の permeate, unit あり）を delete_arc → 拒否、アークは残る。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V1", "to": "V2"},  # MEMB1 permeate
        ]}
        with self.assertRaisesRegex(ApplyError, "owned by unit 'MEMB1'"):
            A.apply_change(ss, change)
        # 元の ss は不変（apply_change は deepcopy なので seed 側でアーク残存を確認）
        self.assertIn(("V1", "V2"), ss["arcs"])

    def test_delete_nonexistent_arc_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V0", "to": "V99"},
        ]}
        with self.assertRaisesRegex(ApplyError, "not found"):
            A.apply_change(ss, change)

    def test_delete_arc_creating_dead_end_rejected(self):
        """削除で下流ユニットの給餌が消えるケース → delete_arc 自体がゾンビガードで拒否。

        (V2,V4) は MEMB2 inlet V4 への唯一の給餌。旧実装は delete_arc を通して
        validate（検査項目8）が後段で弾いていたが、現在は op レベルで
        「zombie unit 化するので delete_unit を使え」と即座に拒否する（検出の前倒し）。
        """
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V2", "to": "V4"},
        ]}
        with self.assertRaisesRegex(ApplyError, "zombie unit"):
            A.apply_change(ss, change)
        # 元の ss は不変
        self.assertIn(("V2", "V4"), ss["arcs"])


class TestPromoteCandidate(unittest.TestCase):

    def test_promote_removes_candidate_key(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True},
            {"op": "promote_candidate", "candidate": "q_1"},
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertNotIn("candidate", new_ss["arcs"][("V6", "V4")])
        self.assertEqual(T.binary_variables(new_ss), [])

    def test_promote_unknown_candidate_raises(self):
        ss = load_seed()
        change = {"operations": [{"op": "promote_candidate", "candidate": "q_99"}]}
        with self.assertRaisesRegex(ApplyError, "q_99"):
            A.apply_change(ss, change)


# =========================================================
# set_bounds
# =========================================================

class TestSetBounds(unittest.TestCase):

    def test_set_bounds_stores_override(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "set_bounds", "unit": "MEMB1",
             "param": "area", "bounds": [200.0, 100000.0]}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(new_ss["units"]["MEMB1"]["bounds_override"],
                         {"area": [200.0, 100000.0]})

    def test_set_bounds_reflected_in_ga_variables(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "set_bounds", "unit": "MEMB1",
             "param": "area", "bounds": [200.0, 100000.0]}
        ]}
        new_ss = A.apply_change(ss, change)
        gv = UR.make_ga_variables("MEMB1", new_ss["units"]["MEMB1"])
        area_var = next(v for v in gv if v["name"] == "MEMB1_area")
        self.assertEqual(area_var["bounds"], [200.0, 100000.0])

    def test_set_bounds_unknown_unit_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "set_bounds", "unit": "GHOST",
             "param": "area", "bounds": [1.0, 2.0]}
        ]}
        with self.assertRaisesRegex(ApplyError, "GHOST"):
            A.apply_change(ss, change)

    def test_set_bounds_invalid_bounds_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "set_bounds", "unit": "MEMB1",
             "param": "area", "bounds": [100.0]}
        ]}
        with self.assertRaisesRegex(ApplyError, "lo, hi"):
            A.apply_change(ss, change)


# =========================================================
# iteration / history / unknown op
# =========================================================

class TestApplyChangeMeta(unittest.TestCase):

    def test_iteration_increments(self):
        ss = load_seed()
        self.assertEqual(ss["iteration"], 0)
        change = {"reason": "trivial", "operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(new_ss["iteration"], 1)

    def test_history_appended(self):
        ss = load_seed()
        change = {"reason": "test reason here", "operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(len(new_ss["history"]), 1)
        self.assertEqual(new_ss["history"][0],
                         {"iter": 1, "change": "test reason here"})

    def test_original_ss_not_mutated(self):
        ss = load_seed()
        ss_snapshot = copy.deepcopy(ss)
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "recycle", "candidate": True}
        ]}
        A.apply_change(ss, change)
        self.assertEqual(ss, ss_snapshot)

    def test_unknown_op_raises(self):
        ss = load_seed()
        change = {"operations": [{"op": "spaghetti"}]}
        with self.assertRaisesRegex(ApplyError, "unknown op"):
            A.apply_change(ss, change)


# =========================================================
# apply_from_files: 退避 + validate 失敗時のロールバック
# =========================================================

class TestApplyFromFiles(unittest.TestCase):

    def _setup_run(self, td: str, change: dict, iter_num: int = 1) -> tuple[str, str]:
        base = td
        iter_dir = os.path.join(base, f"iterations/iter_{iter_num:03d}")
        os.makedirs(iter_dir, exist_ok=True)

        ss_path = os.path.join(base, "ss_current.json")
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            seed_raw = json.load(f)
        with open(ss_path, "w", encoding="utf-8") as f:
            json.dump(seed_raw, f, indent=2, ensure_ascii=False)

        change_path = os.path.join(iter_dir, "ss_change.json")
        with open(change_path, "w", encoding="utf-8") as f:
            json.dump(change, f, indent=2, ensure_ascii=False)
        return base, ss_path

    def test_happy_path_writes_ss_current_and_backup(self):
        change = {
            "reason": "add q_1 recycle",
            "operations": [
                {"op": "add_arc", "from": "V6", "to": "V4",
                 "type": "recycle", "candidate": True}
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            base, ss_path = self._setup_run(td, change)
            new_ss = A.apply_from_files(base, iter_num=1)
            # ss_current.json は更新される
            saved = T.load_ss(ss_path)
            self.assertEqual(saved["iteration"], 1)
            self.assertEqual(saved["arcs"][("V6", "V4")]["candidate"], "q_1")
            # ss_before_change.json が退避されている
            backup = os.path.join(base, "iterations/iter_001/ss_before_change.json")
            self.assertTrue(os.path.exists(backup))
            backup_ss = T.load_ss(backup)
            self.assertEqual(backup_ss["iteration"], 0)

    def test_validate_failure_does_not_overwrite_ss_current(self):
        """delete_unit MEMB1 単体は feed→product を壊すので validate 失敗。
        ss_current.json は更新されないことを確認（ARCH 6.2.1）。"""
        change = {
            "reason": "destructive delete (should fail)",
            "operations": [{"op": "delete_unit", "unit": "MEMB1"}],
        }
        with tempfile.TemporaryDirectory() as td:
            base, ss_path = self._setup_run(td, change)
            seed_before = T.load_ss(ss_path)

            with self.assertRaises(TopologyError):
                A.apply_from_files(base, iter_num=1)

            # ss_current.json は変わっていない
            after = T.load_ss(ss_path)
            self.assertEqual(after, seed_before)
            # 退避ファイルは作られている（rollback の起点になる）
            backup = os.path.join(base, "iterations/iter_001/ss_before_change.json")
            self.assertTrue(os.path.exists(backup))

    def test_get_latest_iter_num(self):
        with tempfile.TemporaryDirectory() as td:
            # iter_001 と iter_003 に ss_change.json、iter_002 にはなし
            for n in (1, 3):
                d = os.path.join(td, f"iterations/iter_{n:03d}")
                os.makedirs(d)
                with open(os.path.join(d, "ss_change.json"), "w") as f:
                    f.write("{}")
            os.makedirs(os.path.join(td, "iterations/iter_002"))
            self.assertEqual(A.get_latest_iter_num(td), 3)

    def test_get_latest_iter_num_no_iterations(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(A.get_latest_iter_num(td))

    def test_reapply_same_iter_rejected_and_backup_preserved(self):
        """同一 iter への2回目の apply は拒否され、ロールバック起点（退避）は無傷。

        無条件上書きだとリトライ1回で「変更前 SS」が適用後の状態に化けて失われる。
        """
        change = {
            "reason": "widen MEMB1 area bounds",
            "operations": [
                {"op": "set_bounds", "unit": "MEMB1",
                 "param": "area", "bounds": [200.0, 300000.0]}
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            base, ss_path = self._setup_run(td, change)
            backup = os.path.join(base, "iterations/iter_001/ss_before_change.json")

            A.apply_from_files(base, iter_num=1)
            self.assertEqual(T.load_ss(ss_path)["iteration"], 1)
            self.assertEqual(T.load_ss(backup)["iteration"], 0)

            # 2回目（force なし）→ 拒否。退避は iteration=0 のまま無傷
            with self.assertRaisesRegex(ApplyError, "--force"):
                A.apply_from_files(base, iter_num=1)
            self.assertEqual(T.load_ss(backup)["iteration"], 0)
            self.assertEqual(T.load_ss(ss_path)["iteration"], 1)

            # --force なら意図的な再適用を許す（退避は上書きされる）
            A.apply_from_files(base, iter_num=1, force=True)
            self.assertEqual(T.load_ss(ss_path)["iteration"], 2)
            self.assertEqual(T.load_ss(backup)["iteration"], 1)


# =========================================================
# 安全ガード（強化分）: add_unit / add_gated_unit / add_arc
# =========================================================

class TestStructureGuards(unittest.TestCase):

    def test_add_unit_on_owned_direct_arc_rejected(self):
        """直結 (V4,V5) は MEMB2 所有アーク。add_unit が黙って壊すのを拒否する。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP1",
             "inlet": "V4", "outlet_to": "V5", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "owned by unit 'MEMB2'"):
            A.apply_change(ss, change)
        self.assertIn(("V4", "V5"), ss["arcs"])  # 元 SS は不変

    def test_add_unit_on_candidate_direct_arc_rejected(self):
        """直結が候補アークの場合、バイナリ変数が黙って消えるので拒否する。"""
        ss = load_seed()
        ss["arcs"][("V2", "V4")]["candidate"] = "q_1"
        change = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V2", "permeate_to": "V4", "retentate_to": "V8", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "candidate 'q_1'"):
            A.apply_change(ss, change)

    def test_add_gated_unit_on_candidate_bypass_rejected(self):
        """バイパス先が既に候補 → 既存 q ラベルの暗黙消滅になるので拒否する。"""
        ss = load_seed()
        ss["arcs"][("V5", "V7")]["candidate"] = "q_1"
        change = {"operations": [
            {"op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "feed_from": "V5", "bypass_to": "V7",
             "permeate_to": "V7", "retentate_to": "V8", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "already candidate"):
            A.apply_change(ss, change)

    def test_add_arc_with_unit_field_rejected(self):
        """所有アークは add_unit 系だけが張る。誤タグの入口を塞ぐ。"""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V4",
             "type": "process", "unit": "MEMB1"}
        ]}
        with self.assertRaisesRegex(ApplyError, "must not carry a unit field"):
            A.apply_change(ss, change)

    def test_add_arc_self_loop_rejected(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "add_arc", "from": "V6", "to": "V6", "type": "recycle"}
        ]}
        with self.assertRaisesRegex(ApplyError, "self-loop"):
            A.apply_change(ss, change)


# =========================================================
# ID 採番: apply 経由でも削除済みIDを再利用しない（ARCH 3.1）
# =========================================================

class TestIdNoReuseThroughApply(unittest.TestCase):

    def test_delete_max_unit_then_add_gets_fresh_ids(self):
        """MEMB3 追加（V9,V10 払い出し）→ 削除 → MEMB4 追加は V11,V12 を得る。"""
        ss = load_seed()
        add3 = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V2", "permeate_to": "V4", "retentate_to": "V8",
             "params": {}}
        ]}
        ss1 = A.apply_change(ss, add3)
        self.assertEqual(ss1["units"]["MEMB3"]["outlets"],
                         {"permeate": "V9", "retentate": "V10"})

        del3 = {"operations": [{"op": "delete_unit", "unit": "MEMB3"}]}
        ss2 = A.apply_change(ss1, del3)
        self.assertNotIn("V9", ss2["vertices"])
        self.assertNotIn("V10", ss2["vertices"])

        add4 = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB4",
             "inlet": "V2", "permeate_to": "V4", "retentate_to": "V8",
             "params": {}}
        ]}
        ss3 = A.apply_change(ss2, add4)
        # V9/V10 は永久欠番（旧実装はここで V9/V10 を別の流れに再割当していた）
        self.assertEqual(ss3["units"]["MEMB4"]["outlets"],
                         {"permeate": "V11", "retentate": "V12"})


if __name__ == "__main__":
    unittest.main()
