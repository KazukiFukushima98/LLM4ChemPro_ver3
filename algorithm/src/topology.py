"""Arc-dictionary topology representation keyed by stable string IDs.

Responsibilities:
- Reading/writing the SS template (superstructure definition including candidates)
- Building a concrete topology once q is fixed: active_topology
- Validity checking: validate (raises TopologyError on violation)
- Deriving auxiliary units: mixer_vertices / splitter_vertices / auto_vps
- Numbering vertex IDs and candidate IDs (max+1, never renumbered)
- Deriving optimisation variable lists: binary_variables / continuous_variables
- Generating the adjacency matrix for the paper: to_matrix

In-memory representation:
    ss = {
        "iteration": int,
        "vertices": {"V0": {"role": ..., "label": ...}, ...},
        "arcs":     {("V0", "V1"): {"type": ..., "unit": ..., "candidate": "q_k"?}, ...},
        "units":    {"MEMB1": {"type": ..., "inlet": ..., "outlets": {...}, "params": {...}}, ...},
        "history":  [...],
    }

The on-disk representation (JSON) cannot have tuple keys, so arcs is a list there.
load_ss / save_ss convert between the two.

Dependencies: Python stdlib only (numpy is imported inside to_matrix only).
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Iterable
from os.path import dirname
from typing import Any

# Add the path so that unit_registry in the same directory can be imported
sys.path.insert(0, dirname(__file__))
from unit_registry import (  # noqa: E402
    get_unit_type,
    is_tie_mode,
    make_ga_variables,
    permeance_bounds_aspen,
)


SinkRoles = frozenset({"product", "residue"})
SourceRoles = frozenset({"feed"})
MembraneArcTypes = frozenset({"membrane_permeate", "membrane_retentate"})


class TopologyError(ValueError):
    """Raised on a structural inconsistency in an SS or a concrete topology."""


# =========================================================
# ID utilities
# =========================================================

def _vid_num(vid: str) -> int:
    """String ID "V12" -> 12."""
    if not vid.startswith("V"):
        raise TopologyError(f"vertex id must start with 'V': {vid!r}")
    try:
        return int(vid[1:])
    except ValueError as e:
        raise TopologyError(f"vertex id has no numeric suffix: {vid!r}") from e


def _qid_num(qid: str) -> int:
    """Candidate ID "q_3" -> 3."""
    if not qid.startswith("q_"):
        raise TopologyError(f"candidate id must start with 'q_': {qid!r}")
    try:
        return int(qid.split("_", 1)[1])
    except ValueError as e:
        raise TopologyError(f"candidate id has no numeric suffix: {qid!r}") from e


def next_vertex_id(vertices: dict[str, Any]) -> str:
    """Pure function returning (max numeric part of existing vertex IDs) + 1
    (used to initialize the counter and as a fallback).

    Returns "V0" if vertices is empty.
    Note: it does not know "the largest ID ever issued", so calling it after
    deleting the highest-numbered vertex would reuse a deleted ID. Use
    allocate_vertex_id to issue new IDs into an SS
    (ARCHITECTURE 3.1: "never renumber or reuse after deletion").
    """
    if not vertices:
        return "V0"
    return f"V{max(_vid_num(v) for v in vertices) + 1}"


def next_candidate_id(arcs: dict[tuple[str, str], dict[str, Any]]) -> str:
    """Pure function returning (max numeric part of existing candidate IDs) + 1
    (used to initialize the counter and as a fallback).

    Returns "q_1" (1-based) if there is no candidate at all.
    Note: for the same reason as next_vertex_id, use allocate_candidate_id to
    issue new IDs into an SS.
    """
    nums: list[int] = []
    for meta in arcs.values():
        cand = meta.get("candidate")
        if cand:
            nums.append(_qid_num(cand))
    return f"q_{max(nums) + 1}" if nums else "q_1"


def _ensure_id_counters(ss: dict[str, Any]) -> dict[str, int]:
    """Ensure and return ss["id_counters"] (the largest numbers ever issued).

    SS files without this key (e.g. ss_seed.json) are initialized from the
    current maxima (backward compatibility). If hand-editing left a counter behind
    the current maximum, it is raised to that maximum as well (defensive).
    """
    counters = ss.get("id_counters") or {}
    v_max = max((_vid_num(v) for v in ss.get("vertices", {})), default=-1)
    q_max = 0
    for meta in ss.get("arcs", {}).values():
        cand = meta.get("candidate")
        if cand:
            q_max = max(q_max, _qid_num(cand))
    counters["vertex"] = max(int(counters.get("vertex", -1)), v_max)
    counters["candidate"] = max(int(counters.get("candidate", 0)), q_max)
    ss["id_counters"] = counters
    return counters


def allocate_vertex_id(ss: dict[str, Any]) -> str:
    """Issue a new vertex ID into the SS (never reusing a deleted ID; ARCHITECTURE 3.1).

    It is "largest ever issued + 1". The counter is persisted in ss["id_counters"]
    (save_ss keeps it in the JSON), so deleting and re-adding the highest-numbered
    vertex can never reassign the same ID to a different physical stream.
    """
    counters = _ensure_id_counters(ss)
    counters["vertex"] += 1
    return f"V{counters['vertex']}"


def allocate_candidate_id(ss: dict[str, Any]) -> str:
    """Issue a new candidate label q_k into the SS (never reusing a deleted label)."""
    counters = _ensure_id_counters(ss)
    counters["candidate"] += 1
    return f"q_{counters['candidate']}"


# =========================================================
# Load / Save
# =========================================================

def load_ss(path: str) -> dict[str, Any]:
    """Load an SS from JSON and convert it to the in-memory form (tuple-keyed arcs)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _from_json(raw)


def save_ss(ss: dict[str, Any], path: str) -> None:
    """Save an SS to JSON. arcs are stably sorted by the numeric parts of from/to.

    The write is atomic (temp file in the same directory -> os.replace).
    ss_current.json is the authoritative live state, and if a forced termination
    mid-write (taskkill collateral from Aspen / Ctrl-C / disk full) left invalid
    JSON the whole outer loop would stall, so the old file is left intact.
    """
    raw = _to_json(ss)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)  # atomic even on Windows within the same volume


def _from_json(raw: dict[str, Any]) -> dict[str, Any]:
    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for arc in raw.get("arcs", []):
        frm = arc["from"]
        to = arc["to"]
        if (frm, to) in arcs:
            raise TopologyError(f"duplicate arc in json: {(frm, to)}")
        meta = {k: v for k, v in arc.items() if k not in ("from", "to")}
        arcs[(frm, to)] = meta
    ss = {
        "iteration": raw.get("iteration", 0),
        "vertices": copy.deepcopy(raw.get("vertices", {})),
        "arcs": arcs,
        "units": copy.deepcopy(raw.get("units", {})),
        "history": list(raw.get("history", [])),
    }
    if "id_counters" in raw:
        ss["id_counters"] = dict(raw["id_counters"])
    # No id_counters: initialize from current maxima (backward
    # compatible; ss_seed.json needs no change)
    _ensure_id_counters(ss)
    return ss


def _to_json(ss: dict[str, Any]) -> dict[str, Any]:
    arc_keys = sorted(ss["arcs"], key=lambda k: (_vid_num(k[0]), _vid_num(k[1])))
    arcs_list = []
    for (frm, to) in arc_keys:
        meta = ss["arcs"][(frm, to)]
        arcs_list.append({"from": frm, "to": to, **meta})
    out = {
        "iteration": ss.get("iteration", 0),
        "vertices": copy.deepcopy(ss.get("vertices", {})),
        "arcs": arcs_list,
        "units": copy.deepcopy(ss.get("units", {})),
        "history": list(ss.get("history", [])),
    }
    if "id_counters" in ss:
        out["id_counters"] = dict(ss["id_counters"])
    return out


def dump_topology(topology: dict[str, Any]) -> dict[str, Any]:
    """Convert a concrete topology (tuple-keyed arcs) into a JSON-serializable dict.

    Serialization for passing a topology across the process boundary
    (subprocess_evaluator -> aspen_worker). Unlike an SS it has no iteration/history
    (a concrete topology is {vertices, arcs, units}).
    arcs are stably sorted by the numeric parts of from/to (same rule as _to_json).
    """
    arc_keys = sorted(topology["arcs"], key=lambda k: (_vid_num(k[0]), _vid_num(k[1])))
    arcs_list = [
        {"from": frm, "to": to, **topology["arcs"][(frm, to)]} for (frm, to) in arc_keys
    ]
    return {
        "vertices": copy.deepcopy(topology["vertices"]),
        "arcs": arcs_list,
        "units": copy.deepcopy(topology.get("units", {})),
    }


def load_topology(raw: dict[str, Any]) -> dict[str, Any]:
    """Inverse of dump_topology. Turns list-form arcs back into a tuple-keyed dict."""
    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for arc in raw.get("arcs", []):
        frm = arc["from"]
        to = arc["to"]
        meta = {k: v for k, v in arc.items() if k not in ("from", "to")}
        arcs[(frm, to)] = meta
    return {
        "vertices": copy.deepcopy(raw.get("vertices", {})),
        "arcs": arcs,
        "units": copy.deepcopy(raw.get("units", {})),
    }


# =========================================================
# Active topology (q resolution)
# =========================================================

def active_topology(
    ss: dict[str, Any],
    q_active: dict[str, int] | None = None,
) -> dict[str, Any]:
    """SS template + values of q -> concrete topology (three dicts).

    Parameters
    ----------
    ss : SS template (may contain candidate arcs).
    q_active : dict mapping candidate label -> 0/1. Unspecified candidates count as 0.

    Returns
    -------
    {"vertices": {...}, "arcs": {...}, "units": {...}}
        The concrete topology. The `candidate` key is stripped from arcs (resolved).
        Units left without feed because a candidate arc is OFF are pruned by
        `_prune_dead_units`, so the returned topology matches "what actually gets
        built under this q".
    """
    q_active = q_active or {}
    vertices = copy.deepcopy(ss["vertices"])
    units = copy.deepcopy(ss.get("units", {}))

    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for key, meta in ss["arcs"].items():
        cand = meta.get("candidate")
        if cand is None:
            arcs[key] = {k: v for k, v in meta.items()}
            continue
        if q_active.get(cand, 0) == 1:
            arcs[key] = {k: v for k, v in meta.items() if k != "candidate"}

    severed = _prune_dead_units(vertices, arcs, units)
    topo: dict[str, Any] = {"vertices": vertices, "arcs": arcs, "units": units}
    if severed:
        # Pruning removed the destination of a "live" stream as collateral (e.g. a
        # recycle candidate into a dead unit's outlet vertex is ON). For this q the
        # genotype and the actual structure disagree, so mark it and let is_buildable
        # reject it with a reason (the optimizer then takes the BAD_VALUE path).
        topo["pruning_severed"] = severed
    return topo


def _prune_dead_units(
    vertices: dict[str, Any],
    arcs: dict[tuple[str, str], dict[str, Any]],
    units: dict[str, Any],
) -> list[tuple[str, str]]:
    """Cascade-remove units that lost their feed, to a fixpoint (prunes the concrete
    topology in place).

    Turning a candidate arc OFF (e.g. a bypass toggle) removes the feed to a membrane
    inlet. Such a membrane is not actually built, so it is removed from the concrete
    topology to give a clean "membrane-off" sub-structure. This lets the optimizer compare
    "with membrane" vs "without membrane" fairly (it prevents a zero-feed membrane
    from always landing in the penalty region).

    Trigger: the unit's inlet is unreachable from feed (this subsumes in-degree 0).
        Looking at in-degree alone would leave behind a self-circulating island cut
        off from feed ("feed OFF + recycle ON from downstream of the unit itself":
        a zero-flow membrane), which would then be passed to Aspen and waste an
        evaluation. Using reachability prunes those islands too.
        For topologies without a feed vertex (e.g. in tests) it falls back to the
        old in-degree-0 test.
    What is removed:
        - the unit itself from units (auto_vps then stops generating its VP: automatic
          coupling)
        - each outlet vertex, together with every arc touching it (same shape as the
          outflow handling in delete_unit; the membrane's owned permeate/retentate arcs
          disappear here too)
        - any owned arcs still starting at the inlet (redundant but defensive)
        - isolated vertices with role=internal and in==out==0 (same definition as
          validate item 7)

    Removing outlet vertices can drive a downstream unit's inlet to in-degree 0, so the
    loop repeats until nothing changes (with membranes in series MEMB2->MEMB3, removing
    MEMB2 makes MEMB3 dead on the next pass).

    No residue rerouting: the trigger is "no feed", so there is nothing to reroute (this
    is different from delete_unit, which removes a live unit that has inflow).
    feed/product/residue vertices are never removed.

    Returns
    -------
    list[tuple[str, str]]
        The list of arcs "severed as collateral": third-party arcs (not owned by the
        unit) that pointed into a dead unit's outlet vertex and whose source vertex
        survives in the final topology.
        A non-empty list means "the genotype has that connection ON but it does not
        exist in the actual structure", and the caller (active_topology ->
        is_buildable) sends it down the BAD path.
    """
    severed: list[tuple[str, str]] = []
    feed_vids = [v for v, d in vertices.items() if d.get("role") == "feed"]

    changed = True
    while changed:
        changed = False

        in_deg = {v: 0 for v in vertices}
        for (_frm, to) in arcs:
            if to in in_deg:
                in_deg[to] += 1
        reachable = _reachable_from(feed_vids, arcs, vertices) if feed_vids else None

        for name, u in list(units.items()):
            inlet = u.get("inlet")
            if reachable is not None:
                dead = inlet not in reachable
            else:
                dead = in_deg.get(inlet, 0) == 0
            if not dead:
                continue
            # Prune the dead unit
            del units[name]
            # Outflow side: outlet vertices and every arc touching them (same as delete_unit)
            for ov in (u.get("outlets") or {}).values():
                for k in [k for k in arcs if ov in k]:
                    # Inflow from a third party (an incoming arc not owned by this unit)
                    # is recorded as a candidate "severed live stream"
                    if k[1] == ov and arcs[k].get("unit") != name:
                        severed.append(k)
                    del arcs[k]
                vertices.pop(ov, None)
            # Owned arcs still starting at the inlet (redundant but defensive)
            for k in [k for k in arcs if k[0] == inlet]:
                del arcs[k]
            changed = True

        # Clean up isolated internal vertices (in==out==0; feed/sink are excluded)
        in_deg2 = {v: 0 for v in vertices}
        out_deg2 = {v: 0 for v in vertices}
        for (frm, to) in arcs:
            if frm in out_deg2:
                out_deg2[frm] += 1
            if to in in_deg2:
                in_deg2[to] += 1
        for v in [
            v
            for v, vdef in vertices.items()
            if vdef.get("role") == "internal" and in_deg2[v] == 0 and out_deg2[v] == 0
        ]:
            del vertices[v]
            changed = True

    # Severed arcs whose source vertex also disappeared (internal arcs of an island,
    # etc.) were not "live streams", so drop them
    return [k for k in severed if k[0] in vertices]


def is_buildable(topology: dict[str, Any]) -> str | None:
    """Pure function deciding whether a concrete topology (after q resolution) can be
    built by aspen_builder.

    Returns a reason string if it is unbuildable, None if it is buildable
    (no Aspen needed, no side effects).

    Only **non-membrane splits** are checked:
        The builder cannot create a non-membrane split (splitter; FSplit is not
        implemented), so out-degree >1 on a non-membrane vertex is rejected. Multiple
        outputs are allowed only at the inlet of a unit with multiple outlets
        (currently membranes), and there the out-degree must equal the unit's number of
        outlets (len(outlets)) and every outgoing arc must be owned by that unit
        (meta["unit"] == unit name).

    Not checked:
        - Dead ends: the builder handles them as terminals (no stream generated) or
          unmeasured outputs, so they are buildable and are not rejected here.
        - Merging in-degree: the builder auto-generates a Mixer, so it is buildable.

    Not added to `validate`: an SS template legitimately has out-degree 2 from a
    mutually exclusive candidate pair, and this function applies only to the concrete
    topology after q resolution (a different layer).
    """
    # q combinations where pruning severed a live stream as collateral (marked by
    # active_topology). The genotype (e.g. recycle ON) and the actual structure
    # disagree, so reject as BAD without evaluating.
    severed = topology.get("pruning_severed")
    if severed:
        return f"pruning severed live arc(s) into removed unit outlet: {severed}"

    arcs = topology["arcs"]
    units = topology["units"]
    # unit inlet -> (unit name, number of outlets)
    inlet_of = {u["inlet"]: (name, len(u["outlets"])) for name, u in units.items()}

    for v in topology["vertices"]:
        out = [meta for (frm, _to), meta in arcs.items() if frm == v]
        if v in inlet_of:
            uname, n_out = inlet_of[v]
            if len(out) != n_out:
                return f"unit {uname} inlet {v}: out-degree {len(out)} != {n_out}"
            if any(meta.get("unit") != uname for meta in out):
                return f"unit {uname} inlet {v}: has non-{uname} outgoing arc"
        elif len(out) > 1:
            return (
                f"vertex {v}: out-degree {len(out)} but not a unit inlet "
                f"(split unbuildable)"
            )
    return None


# =========================================================
# Validate
# =========================================================

def validate(topology: dict[str, Any]) -> None:
    """Validity check for an SS template or a concrete topology.

    Raises TopologyError on any violation. The argument may be either an SS or a
    concrete topology (the candidate key is ignored and all arcs are treated as the
    maximal graph).

    Checks:
        1. Every arc endpoint exists in vertices; self-loops (from==to) are forbidden
        2. An arc's unit (if any) exists in units
        2b. Candidate labels have the form "q_<int>" and are unique within the SS
        3. Unit ownership:
           - the inlet / outlets[*] vertices exist
           - the owned arc inlet->each outlet exists and its unit tag matches
             (structural integrity of the unit)
           - a unit-tagged arc must be an inlet->outlet pair of that unit (no mistagging)
           - owned arcs cannot be candidates (a unit's internal structure never
             disappears with q)
           - inlet and outlet vertices cannot be shared between units (within the same
             role only; series connection "A's outlet == B's inlet" is legitimate and
             therefore allowed)
        4. In-degree of role=feed == 0
        5. Out-degree of role=product/residue == 0
        6. Reachability feed -> product (at least one path)
        7. An internal vertex with in-degree + out-degree == 0 is forbidden as isolated
        8. Every internal vertex reachable from feed can also reach some sink
           (forbids disconnected subgraphs and dead ends)
        9. No vertex has an unknown role
    """
    vertices = topology["vertices"]
    arcs = topology["arcs"]
    units = topology.get("units", {})

    # 1. endpoints + no self-loops
    for (frm, to) in arcs:
        if frm not in vertices:
            raise TopologyError(f"arc references unknown vertex from={frm!r}")
        if to not in vertices:
            raise TopologyError(f"arc references unknown vertex to={to!r}")
        if frm == to:
            raise TopologyError(f"self-loop arc {frm!r}->{to!r} is not allowed")

    # 2. arc.unit exists in units
    for key, meta in arcs.items():
        u = meta.get("unit")
        if u is not None and u not in units:
            raise TopologyError(f"arc {key} references unknown unit {u!r}")

    # 2b. Candidate label format/uniqueness (rejected at the final gate rather than
    # deferred to binary_variables)
    seen_cands: set[str] = set()
    for key, meta in arcs.items():
        cand = meta.get("candidate")
        if cand is None:
            continue
        _qid_num(cand)  # raises TopologyError if the format is invalid
        if cand in seen_cands:
            raise TopologyError(f"duplicate candidate label {cand!r}")
        seen_cands.add(cand)

    # 3. Unit ownership (vertex existence + owned arcs exist + exclusivity within a role)
    inlet_owner: dict[str, str] = {}
    outlet_owner: dict[str, str] = {}
    for uname, udef in units.items():
        inlet = udef.get("inlet")
        if inlet is None:
            raise TopologyError(f"unit {uname!r} has no inlet")
        if inlet not in vertices:
            raise TopologyError(f"unit {uname!r}.inlet={inlet!r} not in vertices")
        if inlet in inlet_owner:
            raise TopologyError(
                f"units {inlet_owner[inlet]!r} and {uname!r} share inlet {inlet!r}"
            )
        inlet_owner[inlet] = uname
        for port, vid in (udef.get("outlets") or {}).items():
            if vid not in vertices:
                raise TopologyError(
                    f"unit {uname!r}.outlets[{port!r}]={vid!r} not in vertices"
                )
            if vid in outlet_owner:
                raise TopologyError(
                    f"units {outlet_owner[vid]!r} and {uname!r} share outlet vertex {vid!r}"
                )
            outlet_owner[vid] = uname
            owned = arcs.get((inlet, vid))
            if owned is None:
                raise TopologyError(
                    f"unit {uname!r} missing owned arc {(inlet, vid)} "
                    f"(port {port!r}) - unit structure is broken"
                )
            if owned.get("unit") != uname:
                raise TopologyError(
                    f"arc {(inlet, vid)} should be owned by {uname!r} "
                    f"but has unit={owned.get('unit')!r}"
                )
            if owned.get("candidate"):
                raise TopologyError(
                    f"owned arc {(inlet, vid)} of unit {uname!r} must not be a candidate"
                )
    # A unit-tagged arc must be an inlet->outlet pair of that unit
    for key, meta in arcs.items():
        u = meta.get("unit")
        if u is None:
            continue
        udef = units[u]  # existence guaranteed by check 2
        if key[0] != udef.get("inlet") or key[1] not in (udef.get("outlets") or {}).values():
            raise TopologyError(
                f"arc {key} is tagged unit={u!r} but is not an inlet->outlet arc of it"
            )

    # 4-5. Roles and degrees
    in_deg, out_deg = _degree(vertices, arcs)
    known_roles = SourceRoles | SinkRoles | {"internal"}
    for vid, vdef in vertices.items():
        role = vdef.get("role")
        if role not in known_roles:
            raise TopologyError(f"vertex {vid!r} has unknown role {role!r}")
        if role in SourceRoles and in_deg[vid] != 0:
            raise TopologyError(f"source vertex {vid!r} (role={role}) has incoming arc")
        if role in SinkRoles and out_deg[vid] != 0:
            raise TopologyError(f"sink vertex {vid!r} (role={role}) has outgoing arc")

    # 6. feed -> product reachability
    feed_vids = [v for v, d in vertices.items() if d.get("role") == "feed"]
    product_vids = [v for v, d in vertices.items() if d.get("role") == "product"]
    sink_vids = [v for v, d in vertices.items() if d.get("role") in SinkRoles]

    reachable_from_feed: set[str] = set()
    if feed_vids:
        reachable_from_feed = _reachable_from(feed_vids, arcs, vertices)
    if feed_vids and product_vids:
        if not any(p in reachable_from_feed for p in product_vids):
            raise TopologyError("no feed->product path exists")

    # 7. Isolated internal vertices
    for vid, vdef in vertices.items():
        if vdef.get("role") != "internal":
            continue
        if in_deg[vid] == 0 and out_deg[vid] == 0:
            raise TopologyError(f"isolated internal vertex {vid!r} (no in/out arcs)")

    # 8. Every internal vertex reachable from feed can also reach some sink
    if feed_vids and sink_vids:
        reachable_to_sink = _reachable_to(sink_vids, arcs, vertices)
        for vid in reachable_from_feed:
            if vertices[vid].get("role") != "internal":
                continue
            if vid not in reachable_to_sink:
                raise TopologyError(
                    f"internal vertex {vid!r} reachable from feed but cannot reach any sink"
                )


def _degree(
    vertices: dict[str, Any],
    arcs: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    in_deg = {v: 0 for v in vertices}
    out_deg = {v: 0 for v in vertices}
    for (frm, to) in arcs:
        if frm in out_deg:
            out_deg[frm] += 1
        if to in in_deg:
            in_deg[to] += 1
    return in_deg, out_deg


def _reachable_from(
    starts: Iterable[str],
    arcs: dict[tuple[str, str], dict[str, Any]],
    vertices: dict[str, Any],
) -> set[str]:
    adj: dict[str, set[str]] = {v: set() for v in vertices}
    for (frm, to) in arcs:
        if frm in adj:
            adj[frm].add(to)
    reachable: set[str] = set()
    stack = list(starts)
    while stack:
        v = stack.pop()
        if v in reachable:
            continue
        reachable.add(v)
        stack.extend(adj.get(v, ()))
    return reachable


def _reachable_to(
    targets: Iterable[str],
    arcs: dict[tuple[str, str], dict[str, Any]],
    vertices: dict[str, Any],
) -> set[str]:
    """Reverse reachability: the set of vertices that can reach any of targets."""
    radj: dict[str, set[str]] = {v: set() for v in vertices}
    for (frm, to) in arcs:
        if to in radj:
            radj[to].add(frm)
    reachable: set[str] = set()
    stack = list(targets)
    while stack:
        v = stack.pop()
        if v in reachable:
            continue
        reachable.add(v)
        stack.extend(radj.get(v, ()))
    return reachable


# =========================================================
# Automatic derivation of auxiliary units (applied to a concrete topology)
# =========================================================

def mixer_vertices(topology: dict[str, Any]) -> set[str]:
    """The set of vertices where a Mixer must be placed.

    Rules:
        (1) The source vertex of a membrane arc (membrane_permeate / membrane_retentate)
        (2) Any vertex with in-degree >= 2
        (3) Sink vertices (role=product/residue) (turned into streams to secure a
            measurement point)
        (4) Unit inlet vertices fed by a pass-through arc (a feed/process/recycle arc
            with no unit). The builder wires a pass-through arc only as an
            "F(IN) registration on the destination Mixer", so without a Mixer nobody
            creates the inlet stream and it is left isolated (this covers the pre-mixer
            placement feed->junction->COMP and non-membrane units from add_gated_unit;
            membrane inlets are already Mixers via (1), and an inlet connected directly
            to a unit outlet is fed by a unit arc and is out of scope, so existing
            wiring is unchanged).
            A single-input Mixer is harmless in Aspen, and it naturally becomes
            multi-input when a recycle is added.
    """
    vertices = topology["vertices"]
    arcs = topology["arcs"]
    result: set[str] = set()

    in_deg = {v: 0 for v in vertices}
    for (frm, to), meta in arcs.items():
        if meta.get("type") in MembraneArcTypes:
            result.add(frm)
        if to in in_deg:
            in_deg[to] += 1

    for v, deg in in_deg.items():
        if deg >= 2:
            result.add(v)

    for vid, vdef in vertices.items():
        if vdef.get("role") in SinkRoles:
            result.add(vid)

    unit_inlets = {udef["inlet"] for udef in topology.get("units", {}).values()}
    for (_, to), meta in arcs.items():
        if not meta.get("unit") and to in unit_inlets:
            result.add(to)

    return result


def splitter_vertices(topology: dict[str, Any]) -> set[str]:
    """The set of vertices where a Splitter must be placed (out-degree >= 2)."""
    out_deg: dict[str, int] = {}
    for (frm, _) in topology["arcs"]:
        out_deg[frm] = out_deg.get(frm, 0) + 1
    return {v for v, d in out_deg.items() if d >= 2}


def auto_vps(topology: dict[str, Any]) -> dict[str, str]:
    """Map from MEMB unit name -> the corresponding VP name.

    The n in VP{n} reuses the numeric part of MEMB{n} as is (gaps are fine).
    """
    result: dict[str, str] = {}
    for uname, udef in topology["units"].items():
        if udef.get("type") != "MEMB":
            continue
        if not uname.startswith("MEMB"):
            raise TopologyError(f"MEMB-typed unit has non-MEMB name: {uname!r}")
        num = uname[len("MEMB"):]
        if not num.isdigit():
            raise TopologyError(f"MEMB unit {uname!r} has no numeric suffix")
        result[uname] = f"VP{num}"
    return result


# =========================================================
# Variable derivation (used to assemble the optimisation variable vector)
# =========================================================

def binary_variables(ss: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the list of binary variables from the candidate arcs of an SS template.

    Each element: {"name": "q_k", "arc": (frm, to), "type": str|None, "unit": str|None}.
    The order is ascending in the numeric part of the q label (deterministic).
    """
    seen: dict[str, dict[str, Any]] = {}
    for (frm, to), meta in ss["arcs"].items():
        q = meta.get("candidate")
        if q is None:
            continue
        if q in seen:
            raise TopologyError(f"duplicate candidate label {q!r}")
        seen[q] = {
            "name": q,
            "arc": (frm, to),
            "type": meta.get("type"),
            "unit": meta.get("unit"),
        }
    return [seen[q] for q in sorted(seen, key=_qid_num)]


def continuous_variables(
    ss: dict[str, Any],
    membrane_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive the list of continuous variables from the units of an SS template.

    Order:
        - Outer: the insertion order of units
        - Within each unit: the declaration order in unit_registry
        - When membrane_model (10.1) is in tie mode, the shared permeance variable
          `MEMB_perm` (unit_param=["MEMB*", "permeance_CO2"]) is placed **first**.
          It is pinned first so that positional alignment with the post-pruning
          topology (x_for_topology) is preserved regardless of which membranes
          survive. In per-stage mode (tie=False), each MEMB's variable list gets a
          `{unit}_perm` instead (unit_registry.make_ga_variables).
    Each unit's bounds_override takes precedence over UNIT_BOUNDS inside
    make_ga_variables.
    membrane_model=None behaves as before (permeance is a fixed value in params).
    """
    result: list[dict[str, Any]] = []
    units = ss.get("units", {})
    if (
        membrane_model is not None
        and is_tie_mode(membrane_model)
        and any(get_unit_type(u) == "MEMB" for u in units)
    ):
        result.append({
            "name":       "MEMB_perm",
            "unit_param": ["MEMB*", "permeance_CO2"],
            "bounds":     permeance_bounds_aspen(membrane_model),
        })
    for uname, udef in units.items():
        result.extend(make_ga_variables(uname, udef, membrane_model=membrane_model))
    return result


def _cv_in_topology(cv: dict[str, Any], units: dict[str, Any]) -> bool:
    """Shared predicate for whether a continuous-variable entry is valid for a concrete
    topology (units).

    An entry whose unit name ends in "*" (e.g. "MEMB*" = the tied shared membrane
    variable) is valid as long as at least one unit with that prefix remains.
    """
    uname = cv["unit_param"][0]
    if uname.endswith("*"):
        prefix = uname[:-1]
        return any(u.startswith(prefix) for u in units)
    return uname in units


def cont_vars_for_topology(
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> list[dict[str, Any]]:
    """Restrict a template-derived continuous-variable list to the variables that exist
    in a concrete topology.

    It filters the cv entries with the same predicate and the same order as
    `x_for_topology` (always use the two together so that values and names stay
    aligned). Used to exclude "free dimensions that do not affect the evaluation" of
    pruned units from optimal_params in results.json (preventing phantom signals).
    """
    units = topology.get("units", {})
    return [cv for cv in cont_vars if _cv_in_topology(cv, units)]


def x_for_topology(
    x_cont: list[float],
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> list[float]:
    """Restrict a template-dimension continuous value vector to the variables that exist
    in a concrete topology.

    The optimizer (ga/bo/run_iteration) carries an x over all continuous variables
    derived from the template ss, but the evaluator **zips positionally** with the
    `continuous_variables` of the concrete topology (after dead-unit pruning). If
    pruning removes a unit "in the middle", the positions shift and a preceding unit's
    value is written into a later unit (an alignment bug that shows up when there are
    two or more gated units and only one of them is pruned). Dropping the values of
    units absent from the topology while preserving the template order lines this up
    1:1 with the evaluator's zip.

    The predicate is `_cv_in_topology` (shared with cont_vars_for_topology).
    """
    units = topology.get("units", {})
    return [
        float(val)
        for val, cv in zip(x_cont, cont_vars)
        if _cv_in_topology(cv, units)
    ]


# =========================================================
# Adjacency matrix (for the paper)
# =========================================================

def to_matrix(topology: dict[str, Any]):
    """Return (adjacency_matrix, vertex_order). For the paper.

    Meaning of the values:
        0    : no arc
        1    : fixed arc
        "q_k": candidate arc (candidate label)

    vertex_order is the insertion order of vertices (string ID -> row/column index).
    """
    import numpy as np  # For the paper only; not loaded on the normal path
    vertex_order = list(topology["vertices"])
    idx = {v: i for i, v in enumerate(vertex_order)}
    n = len(vertex_order)
    matrix = np.zeros((n, n), dtype=object)
    for (frm, to), meta in topology["arcs"].items():
        if frm not in idx or to not in idx:
            continue
        cand = meta.get("candidate")
        matrix[idx[frm], idx[to]] = cand if cand else 1
    return matrix, vertex_order
