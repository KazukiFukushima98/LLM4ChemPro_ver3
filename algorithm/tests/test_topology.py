"""Unit tests for topology.py and unit_registry.py (no Aspen required).

Run with:
    uv run python -m unittest algorithm.tests.test_topology
or
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
    """The smallest valid SS (feed -> MEMB -> product/residue)."""
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
        # Gaps in the numbering (as if V9 and V10 were deleted and V11 comes next)
        vertices = {"V0": {}, "V1": {}, "V5": {}, "V8": {}}
        self.assertEqual(T.next_vertex_id(vertices), "V9")

    def test_next_vertex_id_after_delete_and_add(self):
        # max+1 is the point; count+1 would collide.
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
        """Loading ss_seed.json, saving it and loading it again yields an equivalent SS."""
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
            # In JSON the arcs are a list
            self.assertIsInstance(raw["arcs"], list)
            # from/to are hoisted out of the key
            for arc in raw["arcs"]:
                self.assertIn("from", arc)
                self.assertIn("to", arc)
            # After loading they are a dict keyed by tuples
            ss2 = T.load_ss(out)
            self.assertIsInstance(ss2["arcs"], dict)
            self.assertIn(("V0", "V1"), ss2["arcs"])

    def test_save_orders_arcs_numerically(self):
        # The order comes out as (V0,V1), (V1,V2), (V2,V4) ... i.e. numeric
        ss = make_minimal_ss()
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.json")
            T.save_ss(ss, out)
            with open(out, "r", encoding="utf-8") as f:
                raw = json.load(f)
        keys = [(a["from"], a["to"]) for a in raw["arcs"]]
        # Sorted numerically
        nums = [(int(f[1:]), int(t[1:])) for f, t in keys]
        self.assertEqual(nums, sorted(nums))


# =========================================================
# active_topology
# =========================================================

class TestActiveTopology(unittest.TestCase):

    def setUp(self):
        ss = make_minimal_ss()
        # Add a candidate arc (the V3->V1 recycle)
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_1"}
        self.ss = ss

    def test_candidate_off_is_excluded(self):
        topo = T.active_topology(self.ss, {"q_1": 0})
        self.assertNotIn(("V3", "V1"), topo["arcs"])
        # Fixed arcs remain
        self.assertIn(("V0", "V1"), topo["arcs"])

    def test_candidate_on_is_included_without_label(self):
        topo = T.active_topology(self.ss, {"q_1": 1})
        self.assertIn(("V3", "V1"), topo["arcs"])
        # Once resolved, the candidate key is gone
        self.assertNotIn("candidate", topo["arcs"][("V3", "V1")])
        # Other metadata survives
        self.assertEqual(topo["arcs"][("V3", "V1")]["type"], "recycle")

    def test_default_q_is_zero(self):
        topo = T.active_topology(self.ss)  # q_active omitted
        self.assertNotIn(("V3", "V1"), topo["arcs"])

    def test_units_are_carried_over(self):
        topo = T.active_topology(self.ss, {"q_1": 0})
        self.assertIn("MEMB1", topo["units"])
        # It is a deepcopy, so the original is not polluted
        topo["units"]["MEMB1"]["params"]["area"] = 99999.0
        self.assertNotEqual(self.ss["units"]["MEMB1"]["params"]["area"], 99999.0)


# =========================================================
# dead-unit pruning (cascading removal of unfed units)
# =========================================================

def make_bypass_toggle_ss() -> dict:
    """SS in which the MEMB1 permeate is a toggle pair: "feed MEMB2" vs "bypass to product".

    q_1 ON  : (V2->V6) feeds MEMB2 (second stage present)
    q_2 ON  : (V2->V4) goes straight to product (MEMB2 bypassed = unfed)
    Mutually exclusive (both ON is rejected by is_buildable as V2 out-degree 2).
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
        # Bypass ON (feed OFF) -> MEMB2 and the vertices/arcs it owns disappear
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        for v in ("V6", "V7", "V8"):
            self.assertNotIn(v, topo["vertices"])
        for k in topo["arcs"]:
            self.assertNotIn("V6", k)
            self.assertNotIn("V7", k)
            self.assertNotIn("V8", k)
        # The bypass arc remains (permeate straight to product)
        self.assertIn(("V2", "V4"), topo["arcs"])
        # Upstream MEMB1 and the sinks are untouched
        self.assertIn("MEMB1", topo["units"])
        self.assertIn("V5", topo["vertices"])

    def test_pruned_topology_is_buildable(self):
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 0, "q_2": 1})
        self.assertIsNone(T.is_buildable(topo))

    def test_fed_membrane_is_kept(self):
        # Feed ON (bypass OFF) -> MEMB2 stays; no false positive
        topo = T.active_topology(make_bypass_toggle_ss(), {"q_1": 1, "q_2": 0})
        self.assertIn("MEMB2", topo["units"])
        for v in ("V6", "V7", "V8"):
            self.assertIn(v, topo["vertices"])
        self.assertIn(("V2", "V6"), topo["arcs"])
        self.assertIsNone(T.is_buildable(topo))

    def test_fixed_arc_fed_membrane_never_pruned(self):
        # A membrane in a fixed configuration with no candidates is never pruned, whatever q is
        topo = T.active_topology(make_minimal_ss(), {})
        self.assertIn("MEMB1", topo["units"])
        self.assertIn("V1", topo["vertices"])

    def test_series_membrane_cascade_prune(self):
        # MEMB2 -> MEMB3 in series. Cutting the feed to MEMB2 removes MEMB3 too, via the fixpoint.
        ss = make_bypass_toggle_ss()
        # Connect the MEMB2 permeate V7 to MEMB3 in series (fixed arc)
        ss["vertices"]["V9"]  = {"role": "internal", "label": "MEMB3 inlet"}
        ss["vertices"]["V10"] = {"role": "internal", "label": "MEMB3 permeate"}
        ss["vertices"]["V11"] = {"role": "internal", "label": "MEMB3 retentate"}
        # Replace V7->V4 (product) with V7->V9 (to MEMB3)
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
        # Feed OFF -> MEMB2 dead -> V7 gone -> MEMB3 inlet V9 has in-degree 0 -> MEMB3 dead too
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertNotIn("MEMB3", topo["units"])
        for v in ("V6", "V7", "V8", "V9", "V10", "V11"):
            self.assertNotIn(v, topo["vertices"])

    def test_series_membrane_kept_when_fed(self):
        # With the same series configuration but the feed ON, both membranes stay (no cascade false positive)
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
# is_buildable (guard against splits at non-membrane vertices)
# =========================================================

class TestIsBuildable(unittest.TestCase):

    def test_minimal_concrete_is_buildable(self):
        """A membrane inlet has out-degree 2 = it owns both ports; every other vertex <= 1 -> None."""
        topo = T.active_topology(make_minimal_ss())
        self.assertIsNone(T.is_buildable(topo))

    def test_plain_vertex_split_unbuildable(self):
        """Two plain arcs out of a plain vertex (V2) = out-degree 2 -> not None."""
        topo = T.active_topology(make_minimal_ss())
        # Add an extra outgoing arc from V2 (MEMB1 permeate, plain) to give it out-degree 2
        topo["arcs"][("V2", "V5")] = {"type": "process"}
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("V2", reason)

    def test_membrane_inlet_extra_arc_unbuildable(self):
        """An extra plain outgoing arc on the membrane inlet (V1) gives out-degree 3 -> not None."""
        topo = T.active_topology(make_minimal_ss())
        topo["arcs"][("V1", "V4")] = {"type": "process"}
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("V1", reason)

    def test_non_inlet_vertex_outdegree_two_unbuildable(self):
        """Out-degree 2 on a vertex that is not a membrane inlet (V0=feed) -> not None."""
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
        # Cut V2->V4 (the product becomes isolated)
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
        """Check 8: an internal vertex reachable from the feed that cannot reach a sink is rejected."""
        ss = make_minimal_ss()
        # Branch from V0 (feed) to V6 (internal); V6 has nowhere to go (dead end)
        ss["vertices"]["V6"] = {"role": "internal", "label": "dead end"}
        ss["arcs"][("V0", "V6")] = {"type": "process"}
        # V6 has in_deg=1, out_deg=0 -> it passes the existing isolation check (it is not isolated)
        # but it cannot reach a sink -> the new rule must reject it
        with self.assertRaisesRegex(TopologyError, "cannot reach any sink"):
            T.validate(ss)

    def test_internal_reachable_from_feed_through_chain_to_no_sink_raises(self):
        """Check 8: a dead end two hops away is rejected as well."""
        ss = make_minimal_ss()
        ss["vertices"]["V6"] = {"role": "internal", "label": "chain mid"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "chain end"}
        ss["arcs"][("V0", "V6")] = {"type": "process"}
        ss["arcs"][("V6", "V7")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "cannot reach any sink"):
            T.validate(ss)

    def test_unreachable_dead_end_internal_is_allowed(self):
        """Check 8 does not apply to vertices unreachable from the feed.
        (A subgraph that is not isolated but out of the feed's reach is left to the optimizer's BAD_VALUE.)"""
        ss = make_minimal_ss()
        # Build an "island" unreachable from V0: V6->V7 with no arc into V6
        ss["vertices"]["V6"] = {"role": "internal", "label": "island src"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "island dst"}
        ss["arcs"][("V6", "V7")] = {"type": "process"}
        # V6 / V7 are not isolated: (in_deg=0,out_deg=1) / (in_deg=1,out_deg=0)
        # They are unreachable from the feed, so check 8 does not apply -> validate passes
        T.validate(ss)  # no raise


# =========================================================
# Auxiliary derivation
# =========================================================

class TestMixerSplitterVps(unittest.TestCase):

    def test_mixer_at_membrane_inlet(self):
        ss = make_minimal_ss()
        mixers = T.mixer_vertices(ss)
        self.assertIn("V1", mixers)  # membrane inlet

    def test_mixer_at_sinks(self):
        ss = make_minimal_ss()
        mixers = T.mixer_vertices(ss)
        self.assertIn("V4", mixers)  # product
        self.assertIn("V5", mixers)  # residue

    def test_mixer_at_high_in_degree(self):
        ss = make_minimal_ss()
        # Raise the in-degree of V1: add one more incoming arc
        ss["vertices"]["V6"] = {"role": "internal", "label": "extra"}
        ss["arcs"][("V2", "V6")] = {"type": "process"}
        ss["arcs"][("V6", "V1")] = {"type": "recycle"}
        mixers = T.mixer_vertices(ss)
        self.assertIn("V1", mixers)

    def test_mixer_at_noop_fed_unit_inlet(self):
        """Rule (4): the inlet of a non-membrane unit fed by a pass-through arc becomes a Mixer.

        In a pre-mixer layout (feed -> V6 -> COMP1 -> V7 -> membrane inlet), unless V6 is made
        a Mixer the builder never creates the inlet stream and it ends up isolated (the cause of
        a silent BAD on every evaluation).
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
        self.assertIn("V6", mixers)   # rule (4): unit inlet fed by a pass-through (feed) arc
        self.assertIn("V1", mixers)   # rule (1): membrane inlet, as before

    def test_no_mixer_at_unit_arc_fed_inlet(self):
        """Outside rule (4): an inlet fed directly by a unit arc is not made a Mixer (existing wiring unchanged).

        Example: the conventional pattern of using the membrane permeate outlet V2 directly as the
        COMP2 inlet (the membrane block creates stream V2, so no Mixer is needed).
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
        self.assertNotIn("V2", mixers)  # fed by a unit arc -> no Mixer, as before

    def test_splitter_when_out_degree_two(self):
        ss = make_minimal_ss()
        # The two membrane arcs (permeate / retentate) leave V1, so it becomes a splitter
        splitters = T.splitter_vertices(ss)
        self.assertIn("V1", splitters)

    def test_auto_vps_match_membrane_numbers(self):
        ss = make_minimal_ss()
        # MEMB1 only
        vps = T.auto_vps(ss)
        self.assertEqual(vps, {"MEMB1": "VP1"})

    def test_auto_vps_with_gaps(self):
        ss = make_minimal_ss()
        # As if MEMB2 had been deleted and only MEMB3 were left
        ss["units"]["MEMB3"] = {
            "type": "MEMB", "inlet": "V1",
            "outlets": {"permeate": "V2", "retentate": "V3"},
            "params": {"area": 1.0, "p_permeate": 0.5},
        }
        # The structure above is inconsistent (two units share the same V2/V3), but this test
        # only targets the name generation of auto_vps
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
        # An arc into the feed would be rejected by validate, but this is a unit test of
        # binary_variables alone, so it does not matter
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
        # MEMB1 -> area, then p_perm
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm"])

    def test_continuous_variables_order_follows_unit_insertion(self):
        ss = make_minimal_ss()
        # Add MEMB2 after MEMB1
        ss["vertices"]["V6"] = {"role": "internal", "label": ""}
        ss["vertices"]["V7"] = {"role": "internal", "label": ""}
        ss["units"]["MEMB2"] = {
            "type": "MEMB", "inlet": "V2",
            "outlets": {"permeate": "V6", "retentate": "V7"},
            "params": {"area": 1.0, "p_permeate": 0.5},
        }
        names = [cv["name"] for cv in T.continuous_variables(ss)]
        # MEMB1 first, MEMB2 second
        self.assertEqual(
            names,
            ["MEMB1_area", "MEMB1_p_perm", "MEMB2_area", "MEMB2_p_perm"],
        )

    def test_bounds_override_takes_precedence(self):
        ss = make_minimal_ss()
        # Override the bounds on area
        ss["units"]["MEMB1"]["bounds_override"] = {"area": [50.0, 200.0]}
        cvs = T.continuous_variables(ss)
        cv_area = next(cv for cv in cvs if cv["name"] == "MEMB1_area")
        self.assertEqual(cv_area["bounds"], [50.0, 200.0])
        # p_permeate is not overridden, so the default applies
        cv_pp = next(cv for cv in cvs if cv["name"] == "MEMB1_p_perm")
        self.assertEqual(cv_pp["bounds"], UR.UNIT_BOUNDS["MEMB"]["p_permeate"])


# =========================================================
# x_for_topology (aligning the continuous x with a pruned topology)
# =========================================================

class TestXForTopology(unittest.TestCase):

    def _cv(self, unit: str, param: str) -> dict:
        return {"name": f"{unit}_{param}", "unit_param": [unit, param], "bounds": [0.0, 1.0]}

    def test_middle_unit_pruned_keeps_alignment(self):
        """Pruning a unit in the middle must not shift the values of the units that follow."""
        cont_vars = [
            self._cv("MEMB1", "area"), self._cv("MEMB1", "p_permeate"),
            self._cv("MEMB2", "area"), self._cv("MEMB2", "p_permeate"),
            self._cv("MEMB3", "area"), self._cv("MEMB3", "p_permeate"),
        ]
        # A concrete topology in which only MEMB2 was pruned
        topology = {"units": {"MEMB1": {}, "MEMB3": {}}}
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        # MEMB2's (3,4) drop out and MEMB3 correctly keeps (5,6)
        self.assertEqual(T.x_for_topology(x, cont_vars, topology), [1.0, 2.0, 5.0, 6.0])

    def test_wildcard_kept_iff_prefix_unit_survives(self):
        """"MEMB*" (the tied shared variable) is kept while at least one membrane survives, and dropped when none do."""
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
        """Lines up 1:1 with continuous_variables after real pruning (bypass toggle OFF)."""
        ss = make_bypass_toggle_ss()
        template_cvs = T.continuous_variables(ss)
        self.assertEqual(len(template_cvs), 4)  # MEMB1/MEMB2 x (area, p_perm)
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
        self.assertEqual(UR.get_unit_type("HEAT1"), "HEAT")   # cooler/heater (10.4)
        self.assertIsNone(UR.get_unit_type("PUMP1"))

    def test_expander_has_no_ga_variables(self):
        """The expander (10.4) is a structural component with no optimisation variables (unchanged even with membrane_model)."""
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
        # p_permeate keeps the default
        self.assertEqual(vs[1]["bounds"], UR.UNIT_BOUNDS["MEMB"]["p_permeate"])

    def test_make_ga_variables_unknown_unit(self):
        self.assertEqual(UR.make_ga_variables("WAT1"), [])

    def test_outlet_ports(self):
        self.assertEqual(UR.get_outlet_ports("MEMB"), ["permeate", "retentate"])
        self.assertEqual(UR.get_outlet_ports("COMP"), ["outlet"])
        with self.assertRaises(KeyError):
            UR.get_outlet_ports("HEX")


# =========================================================
# Robeson membrane model (10.1)
# =========================================================

class TestRobesonMembraneModel(unittest.TestCase):

    def test_alpha_satisfies_upper_bound_relation(self):
        """alpha satisfies Q[GPU]*alpha^n = k (a point on the upper bound)."""
        p = 2.70677  # 1000 GPU
        alpha = UR.robeson_alpha(p, {})
        q_gpu = p / UR.GPU_TO_ASPEN
        self.assertAlmostEqual(q_gpu * alpha ** 2.888, 3.0967e8, delta=3.0967e8 * 1e-9)

    def test_alpha_spot_values_match_lee2018(self):
        """Reference points from Lee 2018: on the upper bound, 3840 GPU <-> alpha ~= 50, 1000 GPU <-> alpha ~= 79.6."""
        c = UR.GPU_TO_ASPEN
        self.assertAlmostEqual(UR.robeson_alpha(3840.0 * c, {}), 50.0, delta=0.5)
        self.assertAlmostEqual(UR.robeson_alpha(1000.0 * c, {}), 79.6, delta=0.5)
        # Higher permeance means lower selectivity (the direction of the trade-off)
        self.assertLess(UR.robeson_alpha(6000.0 * c, {}), UR.robeson_alpha(500.0 * c, {}))

    def test_alpha_rejects_nonpositive_permeance(self):
        with self.assertRaises(ValueError):
            UR.robeson_alpha(0.0, {})

    def test_permeance_bounds_converted_to_aspen_units(self):
        lo, hi = UR.permeance_bounds_aspen({})
        self.assertAlmostEqual(lo, 500.0 * UR.GPU_TO_ASPEN, places=9)
        self.assertAlmostEqual(hi, 6000.0 * UR.GPU_TO_ASPEN, places=9)
        # A GPU specification in case.yaml takes precedence
        lo2, hi2 = UR.permeance_bounds_aspen({"permeance_bounds_gpu": [1000.0, 2000.0]})
        self.assertAlmostEqual(lo2, 1000.0 * UR.GPU_TO_ASPEN, places=9)
        self.assertAlmostEqual(hi2, 2000.0 * UR.GPU_TO_ASPEN, places=9)

    def test_tie_mode_prepends_shared_variable(self):
        """tie=True: a single shared MEMB_perm at the front, and no perm variable on any MEMB."""
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
        """tie=False: {unit}_perm at the end of each MEMB's variable block, and no shared variable."""
        ss = make_minimal_ss()
        cvs = T.continuous_variables(ss, {"tie": False})
        names = [cv["name"] for cv in cvs]
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm", "MEMB1_perm"])
        self.assertEqual(cvs[2]["unit_param"], ["MEMB1", "permeance_CO2"])

    def test_none_membrane_model_is_backward_compatible(self):
        """membrane_model=None behaves as before (no permeance variable)."""
        ss = make_minimal_ss()
        names = [cv["name"] for cv in T.continuous_variables(ss)]
        self.assertEqual(names, ["MEMB1_area", "MEMB1_p_perm"])

    def test_tie_variable_absent_without_membrane_units(self):
        """No tied shared variable is added for an SS with no membranes."""
        ss = make_minimal_ss()
        ss["units"] = {}
        self.assertEqual(T.continuous_variables(ss, {"tie": True}), [])


# =========================================================
# allocate_*: the ID counters (deleted IDs are never reused; ARCHITECTURE 3.1)
# =========================================================

class TestAllocateIds(unittest.TestCase):

    def test_allocate_vertex_does_not_reuse_deleted_max(self):
        """The explicit example from ARCH 3.1: add -> delete the highest number -> the next allocation does not reuse the gap."""
        ss = make_minimal_ss()  # V0..V5
        v_a = T.allocate_vertex_id(ss)
        self.assertEqual(v_a, "V6")
        ss["vertices"][v_a] = {"role": "internal", "label": "tmp"}
        v_b = T.allocate_vertex_id(ss)
        self.assertEqual(v_b, "V7")
        ss["vertices"][v_b] = {"role": "internal", "label": "tmp"}
        # Even after deleting the two highest numbers, the next one is V8 (V6/V7 are retired for good)
        del ss["vertices"][v_a]
        del ss["vertices"][v_b]
        self.assertEqual(T.allocate_vertex_id(ss), "V8")

    def test_allocate_candidate_does_not_reuse_deleted_max(self):
        ss = make_minimal_ss()
        ss["arcs"][("V3", "V1")] = {"type": "recycle", "candidate": "q_3"}
        q_a = T.allocate_candidate_id(ss)
        self.assertEqual(q_a, "q_4")
        ss["arcs"][("V2", "V1")] = {"type": "recycle", "candidate": q_a}
        # Deleting the highest-numbered candidate arc does not free q_4 for reuse
        del ss["arcs"][("V2", "V1")]
        self.assertEqual(T.allocate_candidate_id(ss), "q_5")

    def test_counters_survive_save_load_roundtrip(self):
        """The allocation history (id_counters) survives a JSON round-trip."""
        ss = make_minimal_ss()
        self.assertEqual(T.allocate_vertex_id(ss), "V6")   # allocate only (the vertex is not added)
        self.assertEqual(T.allocate_candidate_id(ss), "q_1")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ss.json")
            T.save_ss(ss, path)
            ss2 = T.load_ss(path)
        self.assertEqual(T.allocate_vertex_id(ss2), "V7")
        self.assertEqual(T.allocate_candidate_id(ss2), "q_2")

    def test_ss_without_counters_initialized_from_max(self):
        """An SS without id_counters (ss_seed.json etc.) initialises from the current maximum (backward compatible)."""
        if not os.path.exists(SEED_PATH):
            self.skipTest(f"seed not found at {SEED_PATH}")
        ss = T.load_ss(SEED_PATH)  # V0..V12 (the seed with two blowers), no candidates
        self.assertEqual(T.allocate_vertex_id(ss), "V13")
        self.assertEqual(T.allocate_candidate_id(ss), "q_1")


# =========================================================
# validate: ownership, self-loops, candidate labels (hardening)
# =========================================================

class TestValidateOwnershipAndShape(unittest.TestCase):

    def test_self_loop_rejected(self):
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V2")] = {"type": "process"}
        with self.assertRaisesRegex(TopologyError, "self-loop"):
            T.validate(ss)

    def test_missing_owned_arc_rejected(self):
        """A missing inlet->outlet arc owned by a unit means the unit structure is broken; catch it at the final gate."""
        ss = make_minimal_ss()
        del ss["arcs"][("V1", "V2")]  # the arc owned by the MEMB1 permeate
        with self.assertRaisesRegex(TopologyError, "missing owned arc"):
            T.validate(ss)

    def test_mis_tagged_unit_arc_rejected(self):
        """Detect a unit-tagged arc that is not an inlet->outlet pair (a mis-tag)."""
        ss = make_minimal_ss()
        ss["arcs"][("V2", "V4")]["unit"] = "MEMB1"  # a MEMB1 tag on the product arc
        with self.assertRaisesRegex(TopologyError, "not an inlet"):
            T.validate(ss)

    def test_shared_inlet_rejected(self):
        """Detect two units sharing the same inlet (which would silently overwrite the dict inside is_buildable)."""
        ss = make_minimal_ss()
        ss["units"]["MEMB9"] = {
            "type": "MEMB",
            "inlet": "V1",  # shared with MEMB1
            "outlets": {"permeate": "V2", "retentate": "V3"},
            "params": {},
        }
        with self.assertRaisesRegex(TopologyError, "share inlet"):
            T.validate(ss)

    def test_owned_arc_as_candidate_rejected(self):
        """Making an owned arc (a unit's internal structure) a candidate is forbidden."""
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
        """A's outlet = B's inlet is a valid series connection (only sharing within the same role is forbidden)."""
        ss = make_minimal_ss()
        # Add COMP1 in the proper shape, with V2 (the MEMB1 permeate outlet) as its inlet
        ss["vertices"]["V6"] = {"role": "internal", "label": "COMP1 outlet"}
        del ss["arcs"][("V2", "V4")]
        ss["arcs"][("V2", "V6")] = {"type": "compressor", "unit": "COMP1"}
        ss["arcs"][("V6", "V4")] = {"type": "product"}
        ss["units"]["COMP1"] = {
            "type": "COMP", "inlet": "V2", "outlets": {"outlet": "V6"}, "params": {},
        }
        T.validate(ss)  # does not raise


# =========================================================
# pruning: feed reachability and detection of collaterally severed arcs (hardening)
# =========================================================

class TestPruneReachabilityAndSevered(unittest.TestCase):

    def _ss_with_gated_memb2(self) -> dict:
        """The minimal SS plus a gated MEMB2 (feed q_1 vs bypass q_2)."""
        ss = make_minimal_ss()
        # A dedicated inlet V6 and outlets V7 (perm) / V8 (ret)
        ss["vertices"]["V6"] = {"role": "internal", "label": "MEMB2 inlet (gated)"}
        ss["vertices"]["V7"] = {"role": "internal", "label": "MEMB2 permeate"}
        ss["vertices"]["V8"] = {"role": "internal", "label": "MEMB2 retentate"}
        ss["arcs"][("V6", "V7")] = {"type": "membrane_permeate",  "unit": "MEMB2"}
        ss["arcs"][("V6", "V8")] = {"type": "membrane_retentate", "unit": "MEMB2"}
        ss["arcs"][("V7", "V4")] = {"type": "process"}
        ss["arcs"][("V8", "V5")] = {"type": "process"}
        # Toggle pair: feed (V2,V6)=q_1 vs bypass (V2,V4)=q_2 (as if an existing fixed arc had been made a candidate)
        ss["arcs"][("V2", "V6")] = {"type": "process", "candidate": "q_1"}
        ss["arcs"][("V2", "V4")]["candidate"] = "q_2"
        ss["units"]["MEMB2"] = {
            "type": "MEMB", "inlet": "V6",
            "outlets": {"permeate": "V7", "retentate": "V8"}, "params": {},
        }
        return ss

    def test_unreachable_self_recycle_island_is_pruned(self):
        """Feed OFF plus self-recycle ON: in-degree is 1 but the feed cannot reach it -> prune the whole island.

        The old implementation (which only fired on in-degree 0) passed a zero-flow membrane to Aspen.
        """
        ss = self._ss_with_gated_memb2()
        # A candidate recycle from the unit's own retentate outlet back to its own inlet
        ss["arcs"][("V8", "V6")] = {"type": "recycle", "candidate": "q_3"}
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1, "q_3": 1})
        self.assertNotIn("MEMB2", topo["units"])
        for vid in ("V6", "V7", "V8"):
            self.assertNotIn(vid, topo["vertices"])
        # The island's internal arcs are not treated as severed, since no live flow was cut
        self.assertNotIn("pruning_severed", topo)
        self.assertIsNone(T.is_buildable(topo))

    def test_severed_live_arc_marks_topology_unbuildable(self):
        """Detect the collateral severing of a third-party arc into a dead unit's outlet and route it to BAD.

        This prevents the recycle from silently vanishing from the real structure while the genotype
        says "recycle ON" (a silent genotype/phenotype swap).
        """
        ss = self._ss_with_gated_memb2()
        # A recycle from the MEMB1 retentate V3 to the MEMB2 permeate outlet V7 (a toggle pair with the residue)
        ss["arcs"][("V3", "V7")] = {"type": "recycle", "candidate": "q_3"}
        ss["arcs"][("V3", "V5")]["candidate"] = "q_4"  # make the existing arc to residue a candidate
        # Feed OFF (MEMB2 dead), recycle ON, residue OFF
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1, "q_3": 1, "q_4": 0})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertEqual(topo.get("pruning_severed"), [("V3", "V7")])
        reason = T.is_buildable(topo)
        self.assertIsNotNone(reason)
        self.assertIn("severed", reason)

    def test_normal_gate_off_has_no_severed(self):
        """A normal gated OFF (only the bypass ON) has nothing severed and is buildable."""
        ss = self._ss_with_gated_memb2()
        topo = T.active_topology(ss, {"q_1": 0, "q_2": 1})
        self.assertNotIn("MEMB2", topo["units"])
        self.assertNotIn("pruning_severed", topo)
        self.assertIsNone(T.is_buildable(topo))

    def test_gate_on_keeps_unit(self):
        """With the feed ON (bypass OFF) the unit stays."""
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
        # Fixed arc
        i0, i1 = order.index("V0"), order.index("V1")
        self.assertEqual(m[i0, i1], 1)
        # Candidate arc
        i3 = order.index("V3")
        self.assertEqual(m[i3, i1], "q_1")
        # Not connected
        i4 = order.index("V4")
        self.assertEqual(m[i0, i4], 0)


if __name__ == "__main__":
    unittest.main()
