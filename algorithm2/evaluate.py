"""Black-box flowsheet evaluator for the direct-design agent (algorithm2).

The agent describes a complete membrane flowsheet in a plain JSON proposal
(see PROBLEM.md for the schema) and this CLI returns its simulated performance
and cost. Internals (the process simulator and its data structures) are
intentionally opaque to the agent.

usage (from algorithm2/):
    uv run python evaluate.py --run-dir runs/direct01 proposal.json
    uv run python evaluate.py --dry-validate proposal.json      # no simulation, schema/wiring check only

Exit codes: 0 = evaluated (see report), 1 = invalid proposal, 2 = budget exhausted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
_ALGO_SRC = os.path.normpath(os.path.join(HERE, "..", "algorithm", "src"))
if _ALGO_SRC not in sys.path:
    sys.path.insert(0, _ALGO_SRC)

GPU1 = 2.70677e-3            # 1 GPU in internal permeance units
BOUNDS = {
    "area_m2":        (1.0e5, 1.5e6),
    "p_permeate_bar": (0.1, 0.99),
    "permeance_gpu":  (500.0, 6000.0),
}
MAX_MEMBRANES = 8
BUDGET_SEC = 6.5 * 3600.0


# ---------------------------------------------------------------- validation

def validate(proposal: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    membranes = proposal.get("membranes")
    streams = proposal.get("streams")
    if not isinstance(membranes, dict) or not membranes:
        return ["'membranes' must be a non-empty object"]
    if not isinstance(streams, dict):
        return ["'streams' must be an object"]
    if len(membranes) > MAX_MEMBRANES:
        errors.append(f"at most {MAX_MEMBRANES} membranes are allowed (got {len(membranes)})")
    for name, m in membranes.items():
        if not name.isalnum():
            errors.append(f"membrane name '{name}' must be alphanumeric")
        for key, (lo, hi) in BOUNDS.items():
            v = m.get(key)
            if not isinstance(v, (int, float)):
                errors.append(f"{name}.{key} is missing or not a number")
            elif not (lo <= float(v) <= hi):
                errors.append(f"{name}.{key}={v} is outside the allowed range [{lo}, {hi}]")
    feed_to = streams.get("feed")
    if feed_to not in membranes:
        errors.append(f"'feed' must be routed to one of the membranes (got {feed_to!r})")
    sinks = {"product", "residue"}
    expected = {f"{n}.permeate" for n in membranes} | {f"{n}.retentate" for n in membranes}
    for key in expected:
        dst = streams.get(key)
        if dst is None:
            errors.append(f"stream '{key}' has no destination")
        elif dst not in sinks and dst not in membranes:
            errors.append(f"stream '{key}' destination {dst!r} is not a membrane, 'product' or 'residue'")
    for key in streams:
        if key != "feed" and key not in expected:
            errors.append(f"unknown stream '{key}' (expected 'feed', '<membrane>.permeate' or '<membrane>.retentate')")
    dests = [streams.get(k) for k in expected]
    if "product" not in dests:
        errors.append("no stream is routed to 'product' (the plant must deliver a CO2 product)")
    if "residue" not in dests:
        errors.append("no stream is routed to 'residue' (the plant must have a waste outlet)")
    # Reachability: every membrane must receive flow traceable back to the feed
    if not errors:
        reached = set()
        frontier = [streams["feed"]]
        while frontier:
            cur = frontier.pop()
            if cur in reached or cur not in membranes:
                continue
            reached.add(cur)
            for port in ("permeate", "retentate"):
                frontier.append(streams[f"{cur}.{port}"])
        unreached = sorted(set(membranes) - reached)
        if unreached:
            errors.append(f"membrane(s) {', '.join(unreached)} never receive any flow from the feed "
                          f"— route a stream into them or remove them")
    return errors


# ---------------------------------------------------------------- conversion

def to_internal(proposal: dict[str, Any]):
    """Proposal -> (ss dict, x vector, name mapping). Assumes validate() passed."""
    membranes = proposal["membranes"]
    streams = proposal["streams"]
    order = sorted(membranes)                     # deterministic MEMB numbering
    m_of = {name: f"MEMB{i+1}" for i, name in enumerate(order)}
    c_of = {name: f"COMP{i+1}" for i, name in enumerate(order)}

    vid = [0]
    def new_v(label: str, role: str = "internal") -> str:
        v = f"V{vid[0]}"; vid[0] += 1
        vertices[v] = {"role": role, "label": label}
        return v

    vertices: dict[str, dict] = {}
    v_feed = new_v("Feed (CO2 13%, post-combustion)", "feed")
    v_prod = new_v("Product collector", "product")
    v_res = new_v("Residue collector", "residue")
    premix, compout, inlet, perm, ret = {}, {}, {}, {}, {}
    for name in order:
        premix[name] = new_v(f"{name} pre-mixer / blower inlet")
        compout[name] = new_v(f"{name} blower outlet")
        inlet[name] = new_v(f"{name} inlet (1.1 bar)")
        perm[name] = new_v(f"{name} permeate")
        ret[name] = new_v(f"{name} retentate")

    arcs: list[dict] = [{"from": v_feed, "to": premix[streams["feed"]], "type": "feed"}]
    for name in order:
        arcs.append({"from": premix[name], "to": compout[name], "type": "compressor", "unit": c_of[name]})
        arcs.append({"from": compout[name], "to": inlet[name], "type": "process"})
        arcs.append({"from": inlet[name], "to": perm[name], "type": "membrane_permeate", "unit": m_of[name]})
        arcs.append({"from": inlet[name], "to": ret[name], "type": "membrane_retentate", "unit": m_of[name]})
    for name in order:
        for port, src in (("permeate", perm[name]), ("retentate", ret[name])):
            dst = streams[f"{name}.{port}"]
            if dst == "product":
                arcs.append({"from": src, "to": v_prod, "type": "product"})
            elif dst == "residue":
                arcs.append({"from": src, "to": v_res, "type": "residue"})
            else:
                arcs.append({"from": src, "to": premix[dst], "type": "recycle"})

    units: dict[str, dict] = {}
    for name in order:
        units[c_of[name]] = {
            "type": "COMP", "inlet": premix[name], "outlets": {"outlet": compout[name]},
            "params": {"outlet_pressure": 1.1},
            "bounds_override": {"outlet_pressure": [1.1, 1.1]},
        }
        m = membranes[name]
        units[m_of[name]] = {
            "type": "MEMB", "inlet": inlet[name],
            "outlets": {"permeate": perm[name], "retentate": ret[name]},
            "params": {"area": float(m["area_m2"]), "p_permeate": float(m["p_permeate_bar"]),
                       "permeance_CO2": float(m["permeance_gpu"]) * GPU1,
                       "permeance_N2": float(m["permeance_gpu"]) * GPU1 / 50.0},
        }
    # in-memory arc form: dict keyed by (from, to) tuples
    arc_map = {(a["from"], a["to"]): {k: v for k, v in a.items() if k not in ("from", "to")}
               for a in arcs}
    ss = {"iteration": 0, "vertices": vertices, "arcs": arc_map, "units": units, "history": []}
    return ss, m_of


def x_for(ss: dict, membranes: dict, m_of: dict, membrane_model) -> list[float]:
    import topology as T
    inv = {v: k for k, v in m_of.items()}
    x = []
    for cv in T.continuous_variables(ss, membrane_model):
        unit, suffix = cv["name"].split("_", 1)
        m = membranes[inv[unit]]
        if suffix == "area":
            x.append(float(m["area_m2"]))
        elif suffix == "p_perm":
            x.append(float(m["p_permeate_bar"]))
        elif suffix == "perm":
            x.append(float(m["permeance_gpu"]) * GPU1)
        else:
            raise ValueError(f"unexpected variable {cv['name']}")
    return x


# ---------------------------------------------------------------- reporting

def friendly_buildable(reason: str | None) -> str | None:
    if reason is None:
        return None
    return (f"the wiring is not a valid plant ({reason}) — check for membranes with no "
            f"inflow, or streams that would need a splitter (each outlet feeds exactly one destination)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("proposal", help="path to the proposal JSON")
    ap.add_argument("--run-dir", default=None, help="run directory (state + history); required unless --dry-validate")
    ap.add_argument("--dry-validate", action="store_true", help="schema/wiring check only, no simulation")
    args = ap.parse_args()

    with open(args.proposal, encoding="utf-8") as f:
        proposal = json.load(f)

    errors = validate(proposal)
    if errors:
        print("INVALID PROPOSAL:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    import topology as T
    ss, m_of = to_internal(proposal)
    try:
        T.validate(ss)
        reason = T.is_buildable(T.active_topology(ss, {}))
    except T.TopologyError as e:
        reason = str(e)
    if reason is not None:
        print("INVALID PROPOSAL:")
        print(f"  - {friendly_buildable(reason)}")
        sys.exit(1)

    if args.dry_validate:
        print("OK: proposal is valid and buildable (no simulation performed)")
        sys.exit(0)

    if not args.run_dir:
        print("ERROR: --run-dir is required for evaluation")
        sys.exit(1)
    os.makedirs(args.run_dir, exist_ok=True)
    state_path = os.path.join(args.run_dir, "state.json")
    now = time.time()
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {"t0": now, "n_evals": 0}
    elapsed = now - state["t0"]
    if elapsed >= BUDGET_SEC:
        print(f"BUDGET EXHAUSTED: {elapsed/3600:.2f} h of the {BUDGET_SEC/3600:.1f} h wall-clock "
              f"budget used ({state['n_evals']} evaluations). No further evaluations are accepted.")
        sys.exit(2)

    import run_iteration as RI
    from subprocess_evaluator import SubprocessEvaluator
    from evaluator import cost_per_tco2
    case = RI.load_case()
    evaluator = SubprocessEvaluator(case, RI.ASPEN_FILE, RI.DMP_DIR)
    topology = T.active_topology(ss, {})
    x = x_for(ss, proposal["membranes"], m_of, case.get("membrane_model"))
    detailed = RI.evaluate_detailed_with_retry(evaluator, topology, x)
    m = detailed.metrics

    targets = case["optimization_targets"]
    bad = m.purity <= 0.0 and m.recovery <= 0.0
    if bad:
        report: dict[str, Any] = {"status": "simulation failed",
                                  "note": "the simulator did not converge for this design; treat it as a bad design point"}
        cost = None
    else:
        areas = {m_of[n]: float(mm["area_m2"]) for n, mm in proposal["membranes"].items()}
        cost = cost_per_tco2(m, areas, case)
        shortfall = (max(0.0, targets["purity_min"] - m.purity)
                     + max(0.0, targets["recovery_min"] - m.recovery))
        inv = {v: k for k, v in m_of.items()}
        report = {
            "status": "ok",
            "CO2_purity": round(m.purity, 4),
            "CO2_recovery": round(m.recovery, 4),
            "feasible": bool(m.purity >= targets["purity_min"] and m.recovery >= targets["recovery_min"]),
            "constraint_shortfall": round(shortfall, 4),
            "cost_usd_per_tCO2": round(cost, 2),
            "specific_energy_kWh_per_tCO2": round(m.specific_energy, 1),
            "energy_breakdown_kW": {k: round(v, 1) for k, v in (m.energy_breakdown or {}).items()},
            "streams": detailed.stream_results,
        }

    state["n_evals"] += 1
    state.setdefault("t0", now)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    with open(os.path.join(args.run_dir, "evals.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "n": state["n_evals"],
                            "elapsed_h": round(elapsed / 3600, 3),
                            "proposal": proposal,
                            "purity": None if bad else round(m.purity, 6),
                            "recovery": None if bad else round(m.recovery, 6),
                            "cost": None if bad or cost is None else round(cost, 4)}) + "\n")

    remaining = max(0.0, BUDGET_SEC - (time.time() - state["t0"]))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n[budget] evaluation #{state['n_evals']}, {remaining/3600:.2f} h of wall-clock budget remaining")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
