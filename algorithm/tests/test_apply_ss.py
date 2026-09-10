"""Unit tests for apply_ss.py (no Aspen required).

Run with:
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
    """Re-read a fresh seed for every test."""
    return T.load_ss(SEED_PATH)


# =========================================================
# add_unit
# =========================================================

class TestAddUnit(unittest.TestCase):

    def test_add_memb_on_process_arc_replaces_direct_arc(self):
        ss = load_seed()
        change = {
            "reason": "insert MEMB3 between V2 and V11 (F2 pre-mixer)",
            "operations": [
                {
                    "op": "add_unit",
                    "unit_type": "MEMB",
                    "unit": "MEMB3",
                    "inlet": "V2",
                    "permeate_to": "V11",
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
        # The existing vertices peak at max=12 (the seed with two blowers) -> the new ones are
        # V13, V14 (in permeate -> retentate port order)
        self.assertIn("V13", new_ss["vertices"])
        self.assertIn("V14", new_ss["vertices"])
        # The old direct arc has been removed
        self.assertNotIn(("V2", "V11"), new_ss["arcs"])
        # The four new arcs
        self.assertEqual(new_ss["arcs"][("V2", "V13")],
                         {"type": "membrane_permeate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V13", "V11")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V2", "V14")],
                         {"type": "membrane_retentate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V14", "V8")], {"type": "process"})
        # Registered in units
        self.assertEqual(new_ss["units"]["MEMB3"]["inlet"], "V2")
        self.assertEqual(new_ss["units"]["MEMB3"]["outlets"],
                         {"permeate": "V13", "retentate": "V14"})
        self.assertEqual(new_ss["units"]["MEMB3"]["type"], "MEMB")
        self.assertEqual(new_ss["units"]["MEMB3"]["params"]["area"], 10000.0)
        # validate passes
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
             "inlet": "V2", "permeate_to": "V4", "params": {}}  # retentate_to missing
        ]}
        with self.assertRaisesRegex(ApplyError, "retentate_to"):
            A.apply_change(ss, change)

    def test_missing_operations_key_raises(self):
        """An ss_change without operations (e.g. a mistaken "changes" key) must raise, not apply nothing.

        If the agent writes {"changes": [...]}, a naive implementation would silently
        succeed with an empty, zero-operation apply (regression test for the schema guard).
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
        """COMP has the same two-arc shape as MEMB (an internal compressor plus a downstream process arc).

        The seed already contains COMP1/COMP2 (the fixed blowers), so the added one is named COMP3.
        """
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP3",
             "inlet": "V2", "outlet_to": "V11",
             "params": {"outlet_pressure": 1.1}}
        ]}
        new_ss = A.apply_change(ss, change)
        # The old direct arc (V2,V11) is removed and the new vertex V13 (outlet) is added (max=12 plus one)
        self.assertNotIn(("V2", "V11"), new_ss["arcs"])
        self.assertIn("V13", new_ss["vertices"])
        self.assertEqual(new_ss["arcs"][("V2", "V13")],
                         {"type": "compressor", "unit": "COMP3"})
        self.assertEqual(new_ss["arcs"][("V13", "V11")], {"type": "process"})
        self.assertEqual(new_ss["units"]["COMP3"]["outlets"], {"outlet": "V13"})
        T.validate(new_ss)

    def test_add_expander_two_arcs_per_port(self):
        """EXP (the expander, 10.4) has the same shape as COMP (an internal expander plus a downstream process arc)."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "EXP", "unit": "EXP1",
             "inlet": "V3", "outlet_to": "V8",
             "params": {"outlet_pressure": 1.0}}
        ]}
        new_ss = A.apply_change(ss, change)
        # The old direct residue arc (V3,V8) is removed and the new vertex V13 (outlet) is added
        self.assertNotIn(("V3", "V8"), new_ss["arcs"])
        self.assertIn("V13", new_ss["vertices"])
        self.assertEqual(new_ss["arcs"][("V3", "V13")],
                         {"type": "expander", "unit": "EXP1"})
        self.assertEqual(new_ss["arcs"][("V13", "V8")], {"type": "process"})
        self.assertEqual(new_ss["units"]["EXP1"]["outlets"], {"outlet": "V13"})
        # No new optimisation variable (the expander is a structural component with no variables)
        names = [cv["name"] for cv in T.continuous_variables(new_ss)]
        self.assertNotIn("EXP1_pout", names)
        T.validate(new_ss)

    def test_add_heater_two_arcs_per_port(self):
        """HEAT (cooler/heater) has the same shape as COMP (an internal heater plus a downstream process arc) and no optimisation variables."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "HEAT", "unit": "HEAT1",
             "inlet": "V2", "outlet_to": "V11",
             "params": {"temperature": 35.0, "pressure": 0.0}}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertNotIn(("V2", "V11"), new_ss["arcs"])
        self.assertEqual(new_ss["arcs"][("V2", "V13")],
                         {"type": "heater", "unit": "HEAT1"})
        self.assertEqual(new_ss["arcs"][("V13", "V11")], {"type": "process"})
        names = [cv["name"] for cv in T.continuous_variables(new_ss)]
        # 2 membranes x 2 (COMP is a fixed blower = no variables, and HEAT has none either)
        self.assertEqual(len(names), 4)
        T.validate(new_ss)


# =========================================================
# add_gated_unit (adding a unit as an optimizer-decided toggle)
# =========================================================

def _gate_memb3_on_v5() -> dict:
    """A change that toggle-adds MEMB3 on the seed's MEMB2 permeate(V5) -> product(V7) flow."""
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
        # New inlet V13 and outlets V14/V15 (max=12 -> V13, V14, V15)
        self.assertIn("V13", new_ss["vertices"])
        self.assertIn("V14", new_ss["vertices"])
        self.assertIn("V15", new_ss["vertices"])
        # The membrane's internal arcs (fixed, owned by the unit)
        self.assertEqual(new_ss["arcs"][("V13", "V14")],
                         {"type": "membrane_permeate", "unit": "MEMB3"})
        self.assertEqual(new_ss["arcs"][("V13", "V15")],
                         {"type": "membrane_retentate", "unit": "MEMB3"})
        # The downstream arcs
        self.assertEqual(new_ss["arcs"][("V14", "V7")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V15", "V8")], {"type": "process"})
        # The feed candidate (V5->V13) and the bypass candidate (V5->V7) are both candidates
        self.assertIn("candidate", new_ss["arcs"][("V5", "V13")])
        self.assertIn("candidate", new_ss["arcs"][("V5", "V7")])
        # The two candidates carry distinct labels (two binaries)
        self.assertNotEqual(new_ss["arcs"][("V5", "V13")]["candidate"],
                            new_ss["arcs"][("V5", "V7")]["candidate"])
        # The bypass inherits the original product type
        self.assertEqual(new_ss["arcs"][("V5", "V7")]["type"], "product")
        # Unit registration
        self.assertEqual(new_ss["units"]["MEMB3"]["inlet"], "V13")
        self.assertEqual(new_ss["units"]["MEMB3"]["outlets"],
                         {"permeate": "V14", "retentate": "V15"})

    def test_result_validates(self):
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        T.validate(new_ss)  # OK as long as it does not raise

    def test_adds_exactly_two_binaries(self):
        ss = load_seed()
        n_before = len(T.binary_variables(ss))
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        self.assertEqual(len(T.binary_variables(new_ss)), n_before + 2)

    def test_gate_off_prunes_membrane_clean(self):
        # Feed candidate OFF (bypass ON) -> the pruning in active_topology removes MEMB3
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        q_feed = new_ss["arcs"][("V5", "V13")]["candidate"]
        q_byp = new_ss["arcs"][("V5", "V7")]["candidate"]
        topo = T.active_topology(new_ss, {q_feed: 0, q_byp: 1})
        self.assertNotIn("MEMB3", topo["units"])
        for v in ("V13", "V14", "V15"):
            self.assertNotIn(v, topo["vertices"])
        self.assertIn(("V5", "V7"), topo["arcs"])  # the bypass route remains
        self.assertIsNone(T.is_buildable(topo))    # a clean two-stage structure

    def test_gate_on_keeps_membrane_buildable(self):
        # Feed candidate ON (bypass OFF) -> MEMB3 is built and is_buildable passes
        ss = load_seed()
        new_ss = A.apply_change(ss, _gate_memb3_on_v5())
        q_feed = new_ss["arcs"][("V5", "V13")]["candidate"]
        q_byp = new_ss["arcs"][("V5", "V7")]["candidate"]
        topo = T.active_topology(new_ss, {q_feed: 1, q_byp: 0})
        self.assertIn("MEMB3", topo["units"])
        self.assertIn(("V5", "V13"), topo["arcs"])
        self.assertNotIn(("V5", "V7"), topo["arcs"])
        self.assertIsNone(T.is_buildable(topo))

    def test_rejects_unit_owned_bypass(self):
        # Reject when bypass_to points past a unit-owned arc (a membrane's internal arc)
        ss = load_seed()
        change = {
            "operations": [{
                "op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMBX",
                "feed_from": "V4", "bypass_to": "V5",  # (V4,V5) is owned by MEMB2
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
        # The outlets (V5, V6) are deleted
        self.assertNotIn("V5", new_ss["vertices"])
        self.assertNotIn("V6", new_ss["vertices"])
        # Every related arc is gone
        for k in [("V4", "V5"), ("V4", "V6"), ("V5", "V7"), ("V6", "V8")]:
            self.assertNotIn(k, new_ss["arcs"])
        # The inlet V4 is now isolated and has been deleted
        self.assertNotIn("V4", new_ss["vertices"])
        # The old (V12, V4) (downstream of COMP2) is rerouted to (V12, V8) (type=residue, the unit metadata is gone)
        self.assertNotIn(("V12", "V4"), new_ss["arcs"])
        self.assertEqual(new_ss["arcs"][("V12", "V8")], {"type": "residue"})
        # It is gone from units as well
        self.assertNotIn("MEMB2", new_ss["units"])
        # At this point the feed->product route is lost -> validate must fail
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
        # The old (V12,V4) must be rerouted to V20 (not to the existing V8)
        self.assertIn(("V12", "V20"), new_ss["arcs"])
        self.assertNotIn(("V12", "V8"), new_ss["arcs"])

    def test_delete_then_add_as_set(self):
        """A concrete example where validate passes after applying a delete + add set.

        Delete MEMB2 (the COMP2 downstream arc (V12,V4) is rerouted to (V12,V8) residue), then
        intercept that rerouted arc to insert MEMB3 (reattaching a membrane downstream of COMP2).
        """
        ss = load_seed()
        change = {
            "reason": "swap MEMB2 with MEMB3 downstream of COMP2",
            "operations": [
                {"op": "delete_unit", "unit": "MEMB2"},
                {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
                 "inlet": "V12", "permeate_to": "V7", "retentate_to": "V8",
                 "params": {"permeance_CO2": 2.70677, "permeance_N2": 0.0541354,
                            "area": 20000.0, "p_permeate": 0.1}}
            ],
        }
        new_ss = A.apply_change(ss, change)
        # The post-delete reroute (V12,V8) is consumed by add_unit removing the direct arc
        self.assertNotIn(("V12", "V8"), new_ss["arcs"])
        # MEMB3 is registered, with downstream arcs permeate->V7 and retentate->V8
        self.assertIn("MEMB3", new_ss["units"])
        # max=12 -> V13, V14
        self.assertEqual(new_ss["arcs"][("V13", "V7")], {"type": "process"})
        self.assertEqual(new_ss["arcs"][("V14", "V8")], {"type": "process"})
        # validate passes
        T.validate(new_ss)


# =========================================================
# Q3: the branches where delete_unit must not break a live route
# =========================================================

class TestDeleteUnitInletSharing(unittest.TestCase):

    def test_skips_reroute_when_inlet_shared_with_another_unit(self):
        """Q3(2): if the inlet is also the inlet of another unit, do not reroute the inflow or delete the inlet."""
        # Make V1 the inlet of both MEMB1 and MEMB_GHOST (unnatural as a structure, but possible
        # under the ownership model; the point is to exercise the Q3 logic).
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

        # The outlet side of MEMB1 is deleted
        self.assertNotIn("V2", new_ss["vertices"])
        self.assertNotIn("V3", new_ss["vertices"])
        self.assertNotIn(("V1", "V2"), new_ss["arcs"])
        self.assertNotIn(("V1", "V3"), new_ss["arcs"])
        self.assertNotIn("MEMB1", new_ss["units"])

        # V1 is still the inlet of MEMB_GHOST -> it survives
        self.assertIn("V1", new_ss["vertices"])
        self.assertIn("MEMB_GHOST", new_ss["units"])
        self.assertEqual(new_ss["units"]["MEMB_GHOST"]["inlet"], "V1")
        # The inflow (V0, V1) is untouched (still of type feed) and not rerouted to the residue
        self.assertEqual(new_ss["arcs"][("V0", "V1")], {"type": "feed"})
        self.assertNotIn(("V0", "V8"), new_ss["arcs"])

    def test_skips_reroute_when_inlet_has_other_outgoing_arc(self):
        """Q3(3): leave the inlet alone if it still has an outgoing arc not owned by the unit."""
        ss = load_seed()
        # Add an unowned outgoing arc from V1 (the MEMB1 inlet), leading to a sink.
        ss["vertices"]["V20"] = {"role": "internal", "label": "bypass"}
        ss["arcs"][("V1", "V20")] = {"type": "process"}
        ss["arcs"][("V20", "V8")] = {"type": "residue"}

        change = {"operations": [{"op": "delete_unit", "unit": "MEMB1"}]}
        new_ss = A.apply_change(ss, change)

        # The outlet side of MEMB1 is deleted
        self.assertNotIn("V2", new_ss["vertices"])
        self.assertNotIn("V3", new_ss["vertices"])
        self.assertNotIn("MEMB1", new_ss["units"])

        # V1 survives because an unowned outgoing arc remains
        self.assertIn("V1", new_ss["vertices"])
        # The inflow (V10, V1) (the process arc downstream of COMP1) is untouched, with no reroute to the residue
        self.assertEqual(new_ss["arcs"][("V10", "V1")], {"type": "process"})
        self.assertNotIn(("V10", "V8"), new_ss["arcs"])
        # The unowned outgoing arc is kept
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
            {"op": "add_arc", "from": "V0", "to": "V9", "type": "feed"}
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
        """Deleting a candidate recycle arc (unit=None, candidate=q_1) removes the arc and one binary."""
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
        """Deleting a fixed non-unit arc (no candidate, no unit) removes it."""
        ss = load_seed()
        # Add a fixed arc that is not in the seed and then remove it (removal restores the seed
        # structure, so validate passes)
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
        """delete_arc on a unit-owned arc (a membrane permeate, with a unit) is rejected and the arc stays."""
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V1", "to": "V2"},  # the MEMB1 permeate
        ]}
        with self.assertRaisesRegex(ApplyError, "owned by unit 'MEMB1'"):
            A.apply_change(ss, change)
        # The original ss is unchanged (apply_change deepcopies, so check the arc still exists on the seed side)
        self.assertIn(("V1", "V2"), ss["arcs"])

    def test_delete_nonexistent_arc_raises(self):
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V0", "to": "V99"},
        ]}
        with self.assertRaisesRegex(ApplyError, "not found"):
            A.apply_change(ss, change)

    def test_delete_arc_creating_dead_end_rejected(self):
        """A deletion that would cut the feed to a downstream unit is rejected by delete_arc's own zombie guard.

        (V12,V4) is the only feed into the MEMB2 inlet V4. The old implementation let delete_arc
        through and validate (check 8) caught it later; now the op itself rejects it immediately
        with "this would create a zombie unit; use delete_unit" (detection moved earlier).
        """
        ss = load_seed()
        change = {"operations": [
            {"op": "delete_arc", "from": "V12", "to": "V4"},
        ]}
        with self.assertRaisesRegex(ApplyError, "zombie unit"):
            A.apply_change(ss, change)
        # The original ss is unchanged
        self.assertIn(("V12", "V4"), ss["arcs"])


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

    def test_bounds_override_equal_lo_hi_fixes_param(self):
        """A bounds_override with lo==hi is excluded from the optimisation variables, i.e. the parameter is fixed.

        Used for "no feed compression (pout fixed at 1.1 bar)". The value itself lives in params.
        """
        ss = load_seed()
        ss["units"]["COMP1"]["bounds_override"] = {"outlet_pressure": [1.1, 1.1]}
        ss["units"]["COMP1"]["params"]["outlet_pressure"] = 1.1
        gv = UR.make_ga_variables("COMP1", ss["units"]["COMP1"])
        self.assertEqual(gv, [])  # COMP1_pout disappears
        # COMP1_pout does not appear among the variables of the whole SS either (the membrane variables are unchanged)
        names = [cv["name"] for cv in T.continuous_variables(ss)]
        self.assertNotIn("COMP1_pout", names)
        self.assertIn("MEMB1_area", names)

    def test_fixed_param_autofill_and_no_ga_variable(self):
        """For a parameter with lo==hi in the registry (the COMP of the blower campaign), omitting it
        from params autofills the fixed value, and it does not become an optimisation variable either."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP3",
             "inlet": "V2", "outlet_to": "V11", "params": {}}
        ]}
        new_ss = A.apply_change(ss, change)
        self.assertEqual(new_ss["units"]["COMP3"]["params"]["outlet_pressure"], 1.1)
        names = [cv["name"] for cv in T.continuous_variables(new_ss)]
        self.assertNotIn("COMP3_pout", names)

    def test_fixed_param_wrong_value_rejected(self):
        """A proposal that writes a different value into a fixed parameter is rejected (so the campaign definition cannot be circumvented)."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP3",
             "inlet": "V2", "outlet_to": "V11",
             "params": {"outlet_pressure": 3.0}}
        ]}
        with self.assertRaisesRegex(ApplyError, "fixed value"):
            A.apply_change(ss, change)

    def test_fixed_param_gated_unit_also_guarded(self):
        """The fixed-parameter guard applies through add_gated_unit as well."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_gated_unit", "unit_type": "COMP", "unit": "COMP3",
             "feed_from": "V5", "bypass_to": "V7", "outlet_to": "V7",
             "params": {"outlet_pressure": 2.0}}
        ]}
        with self.assertRaisesRegex(ApplyError, "fixed value"):
            A.apply_change(ss, change)

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
# apply_from_files: the saved copy, and rollback when validate fails
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
            # ss_current.json is updated
            saved = T.load_ss(ss_path)
            self.assertEqual(saved["iteration"], 1)
            self.assertEqual(saved["arcs"][("V6", "V4")]["candidate"], "q_1")
            # ss_before_change.json has been saved
            backup = os.path.join(base, "iterations/iter_001/ss_before_change.json")
            self.assertTrue(os.path.exists(backup))
            backup_ss = T.load_ss(backup)
            self.assertEqual(backup_ss["iteration"], 0)

    def test_validate_failure_does_not_overwrite_ss_current(self):
        """delete_unit MEMB1 on its own breaks the feed->product route, so validate fails.
        Confirm that ss_current.json is not updated (ARCH 6.2.1)."""
        change = {
            "reason": "destructive delete (should fail)",
            "operations": [{"op": "delete_unit", "unit": "MEMB1"}],
        }
        with tempfile.TemporaryDirectory() as td:
            base, ss_path = self._setup_run(td, change)
            seed_before = T.load_ss(ss_path)

            with self.assertRaises(TopologyError):
                A.apply_from_files(base, iter_num=1)

            # ss_current.json is unchanged
            after = T.load_ss(ss_path)
            self.assertEqual(after, seed_before)
            # The saved copy is created (it is the starting point for a rollback)
            backup = os.path.join(base, "iterations/iter_001/ss_before_change.json")
            self.assertTrue(os.path.exists(backup))

    def test_get_latest_iter_num(self):
        with tempfile.TemporaryDirectory() as td:
            # ss_change.json in iter_001 and iter_003, none in iter_002
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
        """A second apply to the same iter is rejected, and the rollback point (the saved copy) is left intact.

        With unconditional overwriting, a single retry would turn the "SS before the change" into
        the post-change state and lose it.
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

            # Second time (without force) -> rejected. The saved copy stays intact at iteration=0
            with self.assertRaisesRegex(ApplyError, "--force"):
                A.apply_from_files(base, iter_num=1)
            self.assertEqual(T.load_ss(backup)["iteration"], 0)
            self.assertEqual(T.load_ss(ss_path)["iteration"], 1)

            # With --force a deliberate re-apply is allowed (the saved copy is overwritten)
            A.apply_from_files(base, iter_num=1, force=True)
            self.assertEqual(T.load_ss(ss_path)["iteration"], 2)
            self.assertEqual(T.load_ss(backup)["iteration"], 1)


# =========================================================
# Safety guards (hardening): add_unit / add_gated_unit / add_arc
# =========================================================

class TestStructureGuards(unittest.TestCase):

    def test_add_unit_on_owned_direct_arc_rejected(self):
        """The direct arc (V4,V5) is owned by MEMB2. Reject add_unit rather than let it silently break it."""
        ss = load_seed()
        change = {"operations": [
            {"op": "add_unit", "unit_type": "COMP", "unit": "COMP9",
             "inlet": "V4", "outlet_to": "V5", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "owned by unit 'MEMB2'"):
            A.apply_change(ss, change)
        self.assertIn(("V4", "V5"), ss["arcs"])  # the original SS is unchanged

    def test_add_unit_on_candidate_direct_arc_rejected(self):
        """When the direct arc is a candidate, reject: a binary variable would silently disappear."""
        ss = load_seed()
        ss["arcs"][("V2", "V11")]["candidate"] = "q_1"
        change = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V2", "permeate_to": "V11", "retentate_to": "V8", "params": {}}
        ]}
        with self.assertRaisesRegex(ApplyError, "candidate 'q_1'"):
            A.apply_change(ss, change)

    def test_add_gated_unit_on_candidate_bypass_rejected(self):
        """The bypass target is already a candidate -> reject, since the existing q label would vanish implicitly."""
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
        """Only the add_unit family may create owned arcs. Close the door on mis-tagging."""
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
# ID allocation: deleted IDs are not reused through apply either (ARCH 3.1)
# =========================================================

class TestIdNoReuseThroughApply(unittest.TestCase):

    def test_delete_max_unit_then_add_gets_fresh_ids(self):
        """Add MEMB3 (allocating V13, V14) -> delete it -> adding MEMB4 gets V15, V16."""
        ss = load_seed()
        add3 = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3",
             "inlet": "V2", "permeate_to": "V11", "retentate_to": "V8",
             "params": {}}
        ]}
        ss1 = A.apply_change(ss, add3)
        self.assertEqual(ss1["units"]["MEMB3"]["outlets"],
                         {"permeate": "V13", "retentate": "V14"})

        del3 = {"operations": [{"op": "delete_unit", "unit": "MEMB3"}]}
        ss2 = A.apply_change(ss1, del3)
        self.assertNotIn("V13", ss2["vertices"])
        self.assertNotIn("V14", ss2["vertices"])

        add4 = {"operations": [
            {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB4",
             "inlet": "V2", "permeate_to": "V11", "retentate_to": "V8",
             "params": {}}
        ]}
        ss3 = A.apply_change(ss2, add4)
        # V13/V14 are retired for good (the old implementation reassigned them to a different flow here)
        self.assertEqual(ss3["units"]["MEMB4"]["outlets"],
                         {"permeate": "V15", "retentate": "V16"})


if __name__ == "__main__":
    unittest.main()
