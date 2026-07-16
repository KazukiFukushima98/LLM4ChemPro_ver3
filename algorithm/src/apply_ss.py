"""Read ss_change.json (a unit-level diff) and update ss_current.json.

Operations (ARCHITECTURE 6.2):
    add_unit          : add a unit (always engaged; outlet vertices are numbered
                        deterministically from STRUCTURE_TEMPLATES)
    add_gated_unit    : add a unit as a GA toggle (the feed becomes a bypass
                        candidate, i.e. engagement becomes a binary variable.
                        When OFF, active_topology prunes the unit away. 6.2)
    delete_unit       : delete a unit (inflows are rerouted to residue, outflows
                        are removed. 6.3)
    add_arc           : add an arc (candidate:true auto-numbers q_k)
    delete_arc        : delete a single arc (unit-owned arcs are rejected; use
                        delete_unit instead. 6.2)
    promote_candidate : promote a candidate to a fixed arc (drop the candidate key)
    set_bounds        : store continuous-variable bounds in units[name].bounds_override

CLI:
    uv run python algorithm/src/apply_ss.py --base-dir runs/runN --iter N
    -> applies <base>/iterations/iter_NNN/ss_change.json to <base>/ss_current.json

Backup and validation (ARCHITECTURE 6.2.1 / section 7):
    - ss_current.json is always backed up to iter_NNN/ss_before_change.json before applying
    - topology.validate runs after applying; on violation it raises and ss_current.json
      is left untouched
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import topology as T  # noqa: E402
from topology import allocate_candidate_id, allocate_vertex_id  # noqa: E402
from unit_registry import STRUCTURE_TEMPLATES, UNIT_BOUNDS, get_outlet_ports  # noqa: E402


# "Inner arc" type per unit type (the arc from the inlet to an outlet vertex).
# Defaults to "process". MEMB and COMP follow the vocabulary in 3.6.
INNER_ARC_TYPE: dict[str, dict[str, str]] = {
    "MEMB": {"permeate": "membrane_permeate", "retentate": "membrane_retentate"},
    "COMP": {"outlet": "compressor"},
    "EXP":  {"outlet": "expander"},   # expander (ver3 12.4)
    "HEAT": {"outlet": "heater"},     # cooler/heater (added during the ver3 12.4 cross-check)
}


class ApplyError(ValueError):
    """An inconsistency detected while applying an ss_change."""


def _enforce_fixed_params(unit_type: str, unit: str, params: dict[str, Any]) -> None:
    """Enforce fixed parameters (lo==hi in UNIT_BOUNDS) (2026-07-15).

    Parameters with lo==hi never become GA/BO variables, so the value in params is
    what actually reaches the simulation; a proposal writing a different value would
    slip past the campaign definition (e.g. blower fixed at 1.1 bar). Fill in the
    fixed value when unspecified, and reject any differing value explicitly.
    """
    for param, (lo, hi) in UNIT_BOUNDS.get(unit_type, {}).items():
        if lo != hi:
            continue
        given = params.get(param)
        if given is None:
            params[param] = lo
        elif float(given) != float(lo):
            raise ApplyError(
                f"{unit}: {param}={given} cannot differ from the fixed value {lo} "
                f"(UNIT_BOUNDS has lo==hi, i.e. the parameter is fixed by the campaign)"
            )


# =========================================================
# Operation handlers
# =========================================================

def op_add_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit_type = op["unit_type"]
    unit = op["unit"]
    inlet = op["inlet"]
    params = op.get("params", {})

    if unit_type not in STRUCTURE_TEMPLATES:
        raise ApplyError(f"unknown unit_type {unit_type!r}")
    if unit in ss["units"]:
        raise ApplyError(f"unit {unit!r} already exists")
    if inlet not in ss["vertices"]:
        raise ApplyError(f"inlet {inlet!r} not in vertices")
    _enforce_fixed_params(unit_type, unit, params)

    ports = get_outlet_ports(unit_type)
    inner_map = INNER_ARC_TYPE.get(unit_type, {})

    # Get the destination ({port}_to) of each outlet port
    port_to: dict[str, str] = {}
    for port in ports:
        key = f"{port}_to"
        if key not in op:
            raise ApplyError(f"add_unit for {unit_type} requires field {key!r}")
        target = op[key]
        if target not in ss["vertices"]:
            raise ApplyError(f"{key}={target!r} not in vertices")
        port_to[port] = target

    # Number the outlet vertices (deterministic, in STRUCTURE_TEMPLATES port order)
    outlets: dict[str, str] = {}
    for port in ports:
        new_v = allocate_vertex_id(ss)
        ss["vertices"][new_v] = {
            "role": "internal",
            "label": f"{unit} {port} outlet",
        }
        outlets[port] = new_v

    # Add arcs (inner arc + downstream arc). Remove an existing direct (inlet, port_to)
    # arc if present. However, silently breaking a unit-owned arc (another unit's
    # internal structure) or a candidate (a binary variable) is not allowed: an
    # explicit delete_unit / delete_arc is required.
    for port in ports:
        outlet_v = outlets[port]
        downstream = port_to[port]
        inner_type = inner_map.get(port, "process")

        direct = (inlet, downstream)
        existing_direct = ss["arcs"].get(direct)
        if existing_direct is not None:
            owner = existing_direct.get("unit")
            if owner is not None:
                raise ApplyError(
                    f"direct arc {direct} is owned by unit {owner!r}; "
                    f"add_unit cannot silently replace it — "
                    f"use delete_unit first or choose a different inlet/{port}_to"
                )
            cand = existing_direct.get("candidate")
            if cand:
                raise ApplyError(
                    f"direct arc {direct} is candidate {cand!r}; "
                    f"delete_arc it explicitly first "
                    f"(a binary variable must not silently vanish)"
                )
            del ss["arcs"][direct]

        if (inlet, outlet_v) in ss["arcs"]:
            raise ApplyError(f"arc {(inlet, outlet_v)} already exists")
        ss["arcs"][(inlet, outlet_v)] = {"type": inner_type, "unit": unit}

        if (outlet_v, downstream) in ss["arcs"]:
            raise ApplyError(f"arc {(outlet_v, downstream)} already exists")
        ss["arcs"][(outlet_v, downstream)] = {"type": "process"}

    ss["units"][unit] = {
        "type": unit_type,
        "inlet": inlet,
        "outlets": outlets,
        "params": copy.deepcopy(params),
    }


def op_add_gated_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    """Add a unit as a toggle whose on/off state is decided by the GA (structure as a GA variable).

    Whereas `add_unit` adds a unit permanently (always engaged), this operation makes
    engagement a binary candidate (q_k). Concretely, it creates a dedicated new inlet
    vertex and turns the feed into that vertex into a candidate arc. Feed candidate OFF
    -> inlet in-degree 0 -> the dead-unit pruning in `active_topology` removes the unit,
    giving a clean sub-structure without the unit.

    Expansion (turns the feed_from stream into a "to the unit <-> bypass" toggle pair):
        - Allocate a new inlet vertex Vin (role=internal)
        - Allocate outlet vertices in STRUCTURE_TEMPLATES port order
        - Inner arcs Vin->outlet (fixed, unit-owned) plus downstream arcs outlet->{port}_to
        - Feed candidate (feed_from->Vin) (candidate q_a)
        - Bypass candidate (feed_from->bypass_to) (candidate q_b). If a fixed arc already
          exists, its type is inherited and it becomes a candidate (mutually exclusive).
          A unit-owned arc is rejected.

    Required fields: unit_type / unit / feed_from / bypass_to / each {port}_to.
    Note: feed_from must be the source of the single stream being intercepted. If any
    other fixed outgoing arc remains at feed_from, out-degree > 1 when the candidate is
    ON and is_buildable rejects it (the same discipline as recycle toggle pairs; see the
    playbook). This consumes two binaries (check max_binary_variables yourself).
    """
    unit_type = op["unit_type"]
    unit = op["unit"]
    feed_from = op["feed_from"]
    bypass_to = op["bypass_to"]
    params = op.get("params", {})

    if unit_type not in STRUCTURE_TEMPLATES:
        raise ApplyError(f"unknown unit_type {unit_type!r}")
    if unit in ss["units"]:
        raise ApplyError(f"unit {unit!r} already exists")
    if feed_from not in ss["vertices"]:
        raise ApplyError(f"feed_from {feed_from!r} not in vertices")
    if bypass_to not in ss["vertices"]:
        raise ApplyError(f"bypass_to {bypass_to!r} not in vertices")
    _enforce_fixed_params(unit_type, unit, params)

    ports = get_outlet_ports(unit_type)
    inner_map = INNER_ARC_TYPE.get(unit_type, {})

    # Get the destination ({port}_to) of each outlet port
    port_to: dict[str, str] = {}
    for port in ports:
        key = f"{port}_to"
        if key not in op:
            raise ApplyError(f"add_gated_unit for {unit_type} requires field {key!r}")
        target = op[key]
        if target not in ss["vertices"]:
            raise ApplyError(f"{key}={target!r} not in vertices")
        port_to[port] = target

    # Pre-checks on the bypass candidate target (feed_from->bypass_to)
    bypass_key = (feed_from, bypass_to)
    existing_bypass = ss["arcs"].get(bypass_key)
    if existing_bypass is not None and existing_bypass.get("unit") is not None:
        raise ApplyError(
            f"bypass arc {bypass_key} is owned by unit {existing_bypass['unit']!r}; "
            f"choose a different feed_from/bypass_to"
        )
    if existing_bypass is not None and existing_bypass.get("candidate"):
        # Overlaying an already-candidate arc would silently drop the existing q label,
        # breaking history tracking and promote_candidate references. Out of spec
        # (6.2 only covers turning a fixed arc into a candidate).
        raise ApplyError(
            f"bypass arc {bypass_key} is already candidate "
            f"{existing_bypass['candidate']!r}; resolve the existing toggle first "
            f"(promote_candidate or delete_arc) before gating this stream"
        )

    # 1. New inlet vertex
    inlet = allocate_vertex_id(ss)
    ss["vertices"][inlet] = {"role": "internal", "label": f"{unit} inlet (gated)"}

    # 2. Number the outlet vertices
    outlets: dict[str, str] = {}
    for port in ports:
        new_v = allocate_vertex_id(ss)
        ss["vertices"][new_v] = {"role": "internal", "label": f"{unit} {port} outlet"}
        outlets[port] = new_v

    # 3. Inner arcs (fixed, unit-owned) plus downstream arcs
    for port in ports:
        outlet_v = outlets[port]
        downstream = port_to[port]
        inner_type = inner_map.get(port, "process")
        ss["arcs"][(inlet, outlet_v)] = {"type": inner_type, "unit": unit}
        if (outlet_v, downstream) in ss["arcs"]:
            raise ApplyError(f"arc {(outlet_v, downstream)} already exists")
        ss["arcs"][(outlet_v, downstream)] = {"type": "process"}

    # 4. Toggle pair: number the feed candidate q_a first, then the bypass candidate q_b (distinct)
    feed_key = (feed_from, inlet)
    if feed_key in ss["arcs"]:
        raise ApplyError(f"arc {feed_key} already exists")
    ss["arcs"][feed_key] = {"type": "process", "candidate": allocate_candidate_id(ss)}

    bypass_type = existing_bypass["type"] if existing_bypass else "process"
    ss["arcs"][bypass_key] = {
        "type": bypass_type,
        "candidate": allocate_candidate_id(ss),
    }

    # 5. Register the unit
    ss["units"][unit] = {
        "type": unit_type,
        "inlet": inlet,
        "outlets": outlets,
        "params": copy.deepcopy(params),
    }


def op_delete_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit = op["unit"]
    reroute_to_opt = op.get("reroute_to")

    if unit not in ss["units"]:
        raise ApplyError(f"unit {unit!r} not found")
    u = ss["units"][unit]
    inlet_v = u["inlet"]
    outlet_vs = list(u["outlets"].values())

    # 1. Outflow side: delete the outlet vertices and every arc touching them
    for ov in outlet_vs:
        keys = [k for k in ss["arcs"] if ov in k]
        for k in keys:
            del ss["arcs"][k]
        if ov in ss["vertices"]:
            del ss["vertices"][ov]

    # Remove from the units dict (done first so later reference checks exclude it)
    del ss["units"][unit]

    # 2. Is inlet_v referenced by another unit?
    inlet_in_other_units = any(
        inlet_v == ud.get("inlet") or inlet_v in (ud.get("outlets") or {}).values()
        for ud in ss["units"].values()
    )
    if inlet_in_other_units:
        # Do not break a live path (Q3)
        return

    # 3. If inlet_v still has outgoing arcs, leave it alone (live path)
    outgoing = [k for k in ss["arcs"] if k[0] == inlet_v]
    if outgoing:
        return

    # 4. Reroute inflows to residue
    incoming = [k for k in ss["arcs"] if k[1] == inlet_v]
    if incoming:
        sink = _resolve_reroute_sink(ss, reroute_to_opt)
        for (frm, to) in incoming:
            meta = ss["arcs"][(frm, to)]
            del ss["arcs"][(frm, to)]
            new_meta = {k: v for k, v in meta.items() if k != "unit"}
            new_meta["type"] = "residue"
            if (frm, sink) in ss["arcs"]:
                raise ApplyError(
                    f"cannot reroute {(frm, to)} -> {(frm, sink)}: target arc already exists"
                )
            ss["arcs"][(frm, sink)] = new_meta

    # 5. Delete inlet_v if it is now isolated (Q2)
    has_remaining = any(inlet_v in k for k in ss["arcs"])
    if not has_remaining:
        del ss["vertices"][inlet_v]


def _resolve_reroute_sink(ss: dict[str, Any], reroute_to_opt: str | None) -> str:
    """Pick the reroute target for delete_unit inflows: reroute_to if given, else the first role=residue vertex."""
    if reroute_to_opt is not None:
        if reroute_to_opt not in ss["vertices"]:
            raise ApplyError(f"reroute_to={reroute_to_opt!r} not in vertices")
        return reroute_to_opt
    sinks = [v for v, d in ss["vertices"].items() if d.get("role") == "residue"]
    if not sinks:
        raise ApplyError("no residue sink found and reroute_to not specified")
    return sinks[0]


def op_add_arc(ss: dict[str, Any], op: dict[str, Any]) -> None:
    frm = op["from"]
    to = op["to"]
    arc_type = op["type"]
    candidate = bool(op.get("candidate", False))

    # A unit field is not accepted: owned arcs (a unit's internal structure) are created
    # only by add_unit / add_gated_unit. A mistagged owned arc would also be caught by
    # validate's ownership check, but rejecting it at the entry point makes fixing the
    # proposal faster.
    if op.get("unit") is not None:
        raise ApplyError(
            f"add_arc must not carry a unit field (got unit={op['unit']!r}); "
            f"owned arcs are created only by add_unit / add_gated_unit"
        )

    if frm not in ss["vertices"]:
        raise ApplyError(f"from={frm!r} not in vertices")
    if to not in ss["vertices"]:
        raise ApplyError(f"to={to!r} not in vertices")
    if frm == to:
        raise ApplyError(f"self-loop arc {(frm, to)} is not allowed")
    if (frm, to) in ss["arcs"]:
        raise ApplyError(f"arc {(frm, to)} already exists")

    meta: dict[str, Any] = {"type": arc_type}
    if candidate:
        meta["candidate"] = allocate_candidate_id(ss)
    ss["arcs"][(frm, to)] = meta


def op_delete_arc(ss: dict[str, Any], op: dict[str, Any]) -> None:
    """Delete the single arc (from, to) (the mirror image of op_add_arc).

    The only guard is whether meta carries a unit: unit-owned arcs (a membrane's
    permeate/retentate, a COMP's inner arc) would break the unit if deleted on their
    own, so they are rejected and delete_unit must be used instead.
    Cases where the deletion isolates a vertex or creates a dead end are caught by
    validate (check item 8) after the change is applied.
    """
    frm = op["from"]
    to = op["to"]
    key = (frm, to)
    if key not in ss["arcs"]:
        raise ApplyError(f"arc {key} not found")
    owner = ss["arcs"][key].get("unit")
    if owner is not None:
        raise ApplyError(
            f"arc {key} is owned by unit {owner!r}; use delete_unit instead"
        )
    # Zombie-unit guard: reject the deletion if `to` is some unit's inlet and removing
    # this arc would leave no feed arc to that inlet anywhere in the SS. A unit that has
    # lost its feed path is pruned by active_topology for every q, so it can never be
    # built while its continuous variables linger in the GA chromosome — a state that
    # validate (outside the scope of items 7/8) cannot detect either.
    # To remove the unit itself, use delete_unit (the same division of roles as the
    # owned-arc rejection).
    for uname, udef in ss["units"].items():
        if udef.get("inlet") != to:
            continue
        remaining_in = [k for k in ss["arcs"] if k[1] == to and k != key]
        if not remaining_in:
            raise ApplyError(
                f"deleting arc {key} would leave unit {uname!r} with no feed arc "
                f"(it could never be built again = zombie unit); "
                f"use delete_unit {uname!r} instead"
            )
    del ss["arcs"][key]


def op_promote_candidate(ss: dict[str, Any], op: dict[str, Any]) -> None:
    cand = op["candidate"]
    found = 0
    for meta in ss["arcs"].values():
        if meta.get("candidate") == cand:
            del meta["candidate"]
            found += 1
    if found == 0:
        raise ApplyError(f"candidate {cand!r} not found in arcs")


def op_set_bounds(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit = op["unit"]
    param = op["param"]
    bounds = op["bounds"]

    if unit not in ss["units"]:
        raise ApplyError(f"unit {unit!r} not found")
    if not (isinstance(bounds, list) and len(bounds) == 2):
        raise ApplyError(f"bounds must be a [lo, hi] list: {bounds!r}")
    ss["units"][unit].setdefault("bounds_override", {})[param] = list(bounds)


OP_HANDLERS = {
    "add_unit":          op_add_unit,
    "add_gated_unit":    op_add_gated_unit,
    "delete_unit":       op_delete_unit,
    "add_arc":           op_add_arc,
    "delete_arc":        op_delete_arc,
    "promote_candidate": op_promote_candidate,
    "set_bounds":        op_set_bounds,
}


# =========================================================
# Core application logic
# =========================================================

def apply_change(ss: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """Apply change to ss and return the new SS (not in-place).

    Appending to history and incrementing iteration also happen here.
    validate is not called (main / apply_from_files do that).
    """
    # Schema guard (against the real incident in run24 iter_001): if the agent writes a
    # wrong top-level key such as "changes", the old implementation silently succeeded
    # with an empty, zero-operation application, and the next iteration (hours long) then
    # ran on the same SS unnoticed. Fail explicitly when operations is missing, empty,
    # or not a list.
    ops = change.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ApplyError(
            "ss_change has no operations (a non-empty list). "
            f"Top-level keys: {sorted(change.keys())}. "
            "The correct schema is {\"reason\": ..., \"operations\": [...]}"
            " (ARCHITECTURE 6.2)"
        )

    new_ss = copy.deepcopy(ss)
    new_ss["iteration"] = ss.get("iteration", 0) + 1

    for i, op in enumerate(ops):
        name = op.get("op")
        handler = OP_HANDLERS.get(name)
        if handler is None:
            raise ApplyError(f"operations[{i}]: unknown op {name!r}")
        try:
            handler(new_ss, op)
        except ApplyError as e:
            raise ApplyError(f"operations[{i}] ({name}): {e}") from e

    new_ss.setdefault("history", []).append({
        "iter": new_ss["iteration"],
        "change": change.get("reason", ""),
    })
    return new_ss


def apply_from_files(base_dir: str, iter_num: int, force: bool = False) -> dict[str, Any]:
    """Read ss_change.json and update ss_current.json (the CLI core).

    - ss_current.json is always backed up to ss_before_change.json first (ARCH section 7).
    - If the backup already exists, this is treated as a re-run of an already-applied
      change and rejected (only force=True permits overwriting). Unconditional
      overwriting would let a single retry turn the rollback origin (the pre-change SS)
      into the post-change state, losing it forever.
    - Order: apply -> topology.validate -> save. If validate fails it raises and
      ss_current.json is left unchanged (ARCH 6.2.1).
    """
    iter_dir = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}")
    change_file = os.path.join(iter_dir, "ss_change.json")
    ss_path = os.path.join(base_dir, "ss_current.json")
    backup_path = os.path.join(iter_dir, "ss_before_change.json")

    if not os.path.exists(change_file):
        raise FileNotFoundError(f"ss_change.json not found: {change_file}")
    if not os.path.exists(ss_path):
        raise FileNotFoundError(f"ss_current.json not found: {ss_path}")
    if os.path.exists(backup_path) and not force:
        raise ApplyError(
            f"{backup_path} already exists — iter {iter_num} was likely applied already. "
            f"If you really intend to re-apply (e.g. after restoring from the backup), "
            f"run again with --force (this overwrites the rollback backup)."
        )

    with open(change_file, "r", encoding="utf-8") as f:
        change = json.load(f)
    ss = T.load_ss(ss_path)

    shutil.copy(ss_path, backup_path)

    new_ss = apply_change(ss, change)
    T.validate(new_ss)
    T.save_ss(new_ss, ss_path)
    return new_ss


# =========================================================
# CLI
# =========================================================

def get_latest_iter_num(base_dir: str) -> int | None:
    """Return the highest iter number under iterations/ that has an ss_change.json."""
    iter_base = os.path.join(base_dir, "iterations")
    if not os.path.exists(iter_base):
        return None
    nums: list[int] = []
    for d in os.listdir(iter_base):
        if not d.startswith("iter_"):
            continue
        change_path = os.path.join(iter_base, d, "ss_change.json")
        if not os.path.exists(change_path):
            continue
        try:
            nums.append(int(d.split("_", 1)[1]))
        except ValueError:
            continue
    return max(nums) if nums else None


def _load_membrane_model() -> dict[str, Any] | None:
    """Read membrane_model from case.yaml for display (None if absent; never raises).

    apply_ss does not depend on the case logically; this is referenced only so that the
    number and names of the continuous variables in the summary match those of GA/BO
    (which derive them including membrane_model).
    """
    case_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "case.yaml")
    )
    try:
        import yaml
        with open(case_path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("membrane_model")
    except Exception:
        return None


def print_ss_summary(ss: dict[str, Any]) -> None:
    bvars = T.binary_variables(ss)
    cvars = T.continuous_variables(ss, _load_membrane_model())
    print()
    print("Updated SS:")
    print(f"  Iteration: {ss['iteration']}")
    print(f"  Vertices:  {len(ss['vertices'])}  {list(ss['vertices'])}")
    print(f"  Arcs:      {len(ss['arcs'])}")
    print(f"  Units:     {list(ss['units'])}")
    print(f"  Binary variables     ({len(bvars)}): {[b['name'] for b in bvars]}")
    print(f"  Continuous variables ({len(cvars)}): {[c['name'] for c in cvars]}")


def main() -> None:
    # The default Windows console is cp932, so printing non-cp932 characters (arrows,
    # symbols, etc.) contained in an ss_change reason raises UnicodeEncodeError. The
    # autonomous loop generates reasons in arbitrary Unicode, so pin the standard
    # streams to UTF-8 for robustness.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".",
                        help="run directory containing ss_current.json and iterations/")
    parser.add_argument("--iter", type=int, default=None,
                        help="iteration number to apply (auto-detected if omitted)")
    parser.add_argument("--force", action="store_true",
                        help="re-apply even if ss_before_change.json already exists "
                             "(overwrites the rollback backup — use only intentionally)")
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    iter_num = args.iter if args.iter is not None else get_latest_iter_num(base_dir)
    if iter_num is None:
        print("Error: no iteration with ss_change.json found", file=sys.stderr)
        sys.exit(1)

    change_path = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}/ss_change.json")
    if not os.path.exists(change_path):
        print(f"Error: {change_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(change_path, "r", encoding="utf-8") as f:
        change_preview = json.load(f)

    print("=" * 60)
    print(f"Applying SS change: iter {iter_num} -> {iter_num + 1}")
    print(f"Reason: {change_preview.get('reason', '(not stated)')}")
    print("=" * 60)

    new_ss = apply_from_files(base_dir, iter_num, force=args.force)
    print_ss_summary(new_ss)
    print(f"\n{os.path.join(base_dir, 'ss_current.json')} updated")


if __name__ == "__main__":
    main()
