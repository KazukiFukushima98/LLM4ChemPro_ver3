"""One-hot GA dedicated to the baseline (12.6 single-shot optimization).

In a Lee (2018) Fig.2-style superstructure, "exactly one destination per membrane
outlet" (one-hot) is a structural constraint. With the bit representation of the
existing ga.py (independent binary flips), the probability that a random genome is
one-hot is only 1/4096 in the 3-stage case, so almost the entire population is
unbuildable: everything is penalized and evolution loses any gradient to follow
(making it a straw-man comparison). Lee's own GA is likewise built around this
exclusivity, so this module uses a categorical representation with **one gene per
outlet (its value = the chosen destination)** and therefore searches only over
structures that are always valid.

The existing SST code is left completely untouched:
- The evaluation boundary (Evaluator), the topology, the cost model and the fitness
  definition are imported from src/ and thus shared
- The probabilities, distributions and elitism of crossover/mutation/selection
  faithfully mirror the constants of ga.py
  (crossover p=0.7 uniform/blend, mutation p=0.3 with p=0.5 per gene, tournament
  size 3, 1 elite)
- The return value is the same (best, gen_log, n_evals) as run_ga. best is the full
  binary+continuous chromosome (in the form run_iteration.run_one_iteration reads
  directly)

Since the signature is identical to run_ga, the runner only has to swap
run_iteration.run_ga for this function and the existing driving, result saving and
signals all carry over.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import defaultdict
from typing import Any

_SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from evaluator import (  # noqa: E402
    BAD_VALUE,
    ECONOMICS_DEFAULTS,
    Evaluator,
    Metrics,
    cost_per_tco2,
    membrane_areas_from_x,
)
from ga import _fitness  # noqa: E402  (share the fitness definition = guarantee an identical comparison)
from topology import (  # noqa: E402
    active_topology,
    binary_variables,
    continuous_variables,
    is_buildable,
    x_for_topology,
)


def onehot_groups(ss: dict[str, Any]) -> list[list[int]]:
    """Group the candidate arcs by "same outlet" (the src vertex of the arc).

    Each group is one unit of the one-hot constraint (exactly one arc ON). The
    return value is a list of lists of indices into binary_variables(ss) (in order
    of appearance, deterministic).
    """
    bin_vars = binary_variables(ss)
    by_src: dict[str, list[int]] = {}
    for idx, bv in enumerate(bin_vars):
        by_src.setdefault(bv["arc"][0], []).append(idx)
    return list(by_src.values())


def genes_to_bits(genes: list[int], groups: list[list[int]], n_binary: int) -> list[float]:
    """Structural genes (the choice index per group) -> a binary 0/1 vector."""
    bits = [0.0] * n_binary
    for g, choice in zip(groups, genes):
        bits[g[int(choice)]] = 1.0
    return bits


def run_ga_onehot(
    ss: dict,
    case: dict,
    evaluator: Evaluator,
    seed: int = 1,
) -> tuple[Any, list[dict], int]:
    """Mixed GA over one-hot structural genes (same signature and return format as run_ga).

    Chromosome (internal representation): [dest_1..dest_G | x_1..x_n]
        dest_g in {0..len(group_g)-1} (the destination chosen for outlet g)
    The returned best is run_ga compatible: [q_1..q_m | x_1..x_n] (0/1 plus continuous).
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, case.get("membrane_model"))
    groups    = onehot_groups(ss)
    n_binary  = len(bin_vars)
    n_cont    = len(cont_vars)
    n_groups  = len(groups)

    ga_cfg   = case["ga"]
    pop_size = int(ga_cfg["pop_size"])
    n_gen    = int(ga_cfg["n_gen"])
    # Wall-clock budget (baseline protocol 2026-07-16): stop at a GENERATION
    # BOUNDARY once the elapsed time exceeds this. 0/absent = no wall limit.
    # The generation in progress when the limit passes always completes, so the
    # population is never half-evaluated.
    max_wall_sec = float(ga_cfg.get("max_wall_sec", 0) or 0)
    # Selection rule (baseline protocol 2026-07-17): "deb" (default) uses Deb's
    # parameter-free feasibility rule — feasible beats infeasible, infeasibles
    # compare by constraint violation, feasibles compare by objective (cost).
    # This mirrors the CBO's feasibility-first selection and removes the
    # penalty-weight dependence that let a cheap infeasible solution win the
    # penalty fitness (observed in baseline_lee3_ga_pop40, 2026-07-17).
    # "penalty" keeps the legacy penalized-fitness tournament.
    selection = str(ga_cfg.get("selection", "deb"))
    # Per-evaluation log (2026-07-17, for the structure-space map / constraint-plane
    # figures): if the runner injects case["eval_log_path"], every evaluation appends
    # one JSONL record {genes, purity, recovery, obj, viol}. BAD values become null.
    eval_log_path = case.get("eval_log_path")
    targets  = case["optimization_targets"]
    purity_min   = float(targets["purity_min"])
    recovery_min = float(targets["recovery_min"])

    # ---- Objective switch (identical to ga.py) ----
    cost_mode = str(targets.get("objective", "")).strip() == "minimize_cost"
    if cost_mode:
        econ = {**ECONOMICS_DEFAULTS, **(case.get("economics") or {})}
        penalty_w = float(econ["penalty_weight"])
    else:
        penalty_w = float(case.get("penalty_weight", 1e5))

    def _objective(m: Metrics, x_cont: list, topology: dict) -> float:
        if not cost_mode or m.specific_energy >= BAD_VALUE:
            return float(m.specific_energy)
        areas = membrane_areas_from_x(x_cont, cont_vars, topology)
        return cost_per_tco2(m, areas, case)

    cont_bounds = [cv["bounds"] for cv in cont_vars]
    sigma_cont  = [(hi - lo) * 0.15 for lo, hi in cont_bounds]  # identical to ga.py

    rng = random.Random(seed)

    # ---- individual = {"genes": [int]*G, "x": [float]*n,
    #                    "fit": float | None,   penalized fitness (logging / legacy selection)
    #                    "viol": float | None,  constraint shortfall (0 = feasible, inf = BAD)
    #                    "obj":  float | None}  raw objective (cost) ----
    def new_individual() -> dict:
        return {
            "genes": [rng.randrange(len(g)) for g in groups],
            "x": [rng.uniform(lo, hi) for lo, hi in cont_bounds],
            "fit": None, "viol": None, "obj": None,
        }

    def clone(ind: dict) -> dict:
        return {"genes": list(ind["genes"]), "x": list(ind["x"]),
                "fit": ind["fit"], "viol": ind["viol"], "obj": ind["obj"]}

    def clip(ind: dict) -> None:
        for k, (lo, hi) in enumerate(cont_bounds):
            ind["x"][k] = max(lo, min(hi, ind["x"][k]))

    def crossover(c1: dict, c2: dict) -> None:
        # Structure: uniform crossover (p=0.5 per gene; matches the binary uniform
        # crossover of ga.py)
        for g in range(n_groups):
            if rng.random() < 0.5:
                c1["genes"][g], c2["genes"][g] = c2["genes"][g], c1["genes"][g]
        # Continuous: blend (the same gamma formula as ga.py)
        for k in range(n_cont):
            gamma = (1.0 + 2.0 * 0.5) * rng.random() - 0.5
            v1, v2 = c1["x"][k], c2["x"][k]
            c1["x"][k] = (1.0 - gamma) * v1 + gamma * v2
            c2["x"][k] = gamma * v1 + (1.0 - gamma) * v2

    def mutate(ind: dict) -> None:
        # Structure: with p=0.5 per gene, switch to a different destination
        # (the categorical counterpart of ga.py's bit flip, which always changes
        # the value)
        for g in range(n_groups):
            if rng.random() < 0.5 and len(groups[g]) > 1:
                cur = ind["genes"][g]
                alts = [c for c in range(len(groups[g])) if c != cur]
                ind["genes"][g] = rng.choice(alts)
        # Continuous: Gaussian mutation (p=0.5 per gene; sigma identical to ga.py)
        for k in range(n_cont):
            if rng.random() < 0.5:
                ind["x"][k] += rng.gauss(0, sigma_cont[k])

    def deb_key(ind: dict) -> tuple:
        """Sort key implementing Deb's feasibility rule (smaller is better).

        feasible (viol == 0): (0, 0, objective)  — compared by cost
        infeasible          : (1, viol, 0)       — compared by violation
        unevaluated / BAD   : viol = inf, so it loses to every real point
        """
        viol = ind["viol"] if ind["viol"] is not None else float("inf")
        if viol <= 0.0:
            return (0, 0.0, ind["obj"])
        return (1, viol, 0.0)

    def penalty_key(ind: dict):
        return ind["fit"] if ind["fit"] is not None else float("inf")

    sel_key = deb_key if selection == "deb" else penalty_key

    def tournament(pop: list[dict], k: int) -> list[dict]:
        # Equivalent to tools.selTournament(tournsize=3); the comparison rule is
        # sel_key (Deb's feasibility rule by default, legacy penalty fitness otherwise)
        out = []
        for _ in range(k):
            cands = [pop[rng.randrange(len(pop))] for _ in range(3)]
            out.append(min(cands, key=sel_key))
        return out

    def evaluate(individuals: list[dict]) -> int:
        """Group by structural key -> one build per topology, evaluated in a batch (the same folding as ga.py)."""
        by_struct: dict[tuple, list[dict]] = defaultdict(list)
        for ind in individuals:
            by_struct[tuple(ind["genes"])].append(ind)
        total = 0
        for genes_key, inds in by_struct.items():
            bits = genes_to_bits(list(genes_key), groups, n_binary)
            q_active = {bv["name"]: int(b) for bv, b in zip(bin_vars, bits)}
            topology = active_topology(ss, q_active)
            reason = is_buildable(topology)
            if reason is not None:  # defensive: unreachable if the genome is one-hot
                metrics_list = [Metrics.bad() for _ in inds]
            else:
                x_list = [x_for_topology(ind["x"], cont_vars, topology) for ind in inds]
                metrics_list = evaluator.evaluate_topology(topology, x_list)
            log_rows = []
            for ind, m in zip(inds, metrics_list):
                obj = _objective(m, ind["x"], topology)
                ind["fit"] = _fitness(obj, m.purity, m.recovery, targets, penalty_w)
                if m.specific_energy >= BAD_VALUE:
                    ind["viol"], ind["obj"] = float("inf"), float("inf")
                else:
                    # Same shortfall as the CBO bootstrap phase (bo.py):
                    # max(0, pi_min - pi) + max(0, rho_min - rho)
                    ind["viol"] = max(0.0, purity_min - m.purity) + max(0.0, recovery_min - m.recovery)
                    ind["obj"] = float(obj)
                if eval_log_path:
                    bad = m.specific_energy >= BAD_VALUE
                    log_rows.append(json.dumps({
                        "genes": list(genes_key),
                        "purity": None if bad else round(float(m.purity), 6),
                        "recovery": None if bad else round(float(m.recovery), 6),
                        "obj": None if bad else round(float(obj), 4),
                        "viol": None if bad else round(ind["viol"], 6),
                    }))
            if eval_log_path and log_rows:
                with open(eval_log_path, "a", encoding="utf-8") as f:
                    f.write("\n".join(log_rows) + "\n")
            total += len(inds)
        return total

    # ---- Main loop (faithfully mirrors the structure of ga.py) ----
    t0 = time.monotonic()   # "t" in gen_log = seconds elapsed since the start of the optimization
    pop = [new_individual() for _ in range(pop_size)]
    n_evals = evaluate(pop)

    best = clone(min(pop, key=sel_key))
    gen_log: list[dict] = []

    for gen in range(n_gen):
        elapsed = time.monotonic() - t0
        if max_wall_sec and elapsed >= max_wall_sec:
            if gen_log:
                gen_log[-1]["early_stop"] = (
                    f"wall_clock {elapsed / 3600:.2f}h >= {max_wall_sec / 3600:.2f}h"
                )
            print(f"  [GA] wall-clock limit reached at generation boundary "
                  f"({elapsed / 3600:.2f}h >= {max_wall_sec / 3600:.2f}h) -> stop")
            break
        offspring = [clone(ind) for ind in tournament(pop, len(pop))]

        for c1, c2 in zip(offspring[::2], offspring[1::2]):
            if rng.random() < 0.7:
                crossover(c1, c2)
                clip(c1)
                clip(c2)
                c1["fit"] = c1["viol"] = c1["obj"] = None
                c2["fit"] = c2["viol"] = c2["obj"] = None

        for mut in offspring:
            if rng.random() < 0.3:
                mutate(mut)
                clip(mut)
                mut["fit"] = mut["viol"] = mut["obj"] = None

        invalid = [ind for ind in offspring if ind["fit"] is None]
        _t_eval0 = time.monotonic()
        n_evals += evaluate(invalid)
        t_eval_sec = round(time.monotonic() - _t_eval0, 2)   # wall-clock evaluation time of this generation

        pop = offspring
        gen_best = min(pop, key=sel_key)
        if sel_key(gen_best) < sel_key(best):
            best = clone(gen_best)
        # Elitism: replace the worst with a clone of the best (identical to ga.py,
        # under the active selection rule)
        worst_idx = max(range(len(pop)), key=lambda i: sel_key(pop[i]))
        pop[worst_idx] = clone(best)
        best_fit = min(ind["fit"] for ind in pop if ind["fit"] is not None)
        entry = {"gen": gen + 1, "best_fitness": best_fit,
                 "t": round(time.monotonic() - t0, 1),
                 "t_eval": t_eval_sec}
        # Feasibility reporting (same axis as the CBO's feasibility-first best):
        # best-so-far constraint shortfall and best-so-far feasible objective
        if best["viol"] is not None and best["viol"] != float("inf"):
            entry["min_shortfall"] = round(best["viol"], 4)
        if best["viol"] == 0.0:
            entry["feasible_obj_min"] = round(best["obj"], 3)
            status = f"feasible_obj_min={best['obj']:.1f}"
        else:
            status = f"no feasible yet, min_shortfall={best['viol']:.3f}" \
                if best["viol"] not in (None, float("inf")) else "no valid eval yet"
        gen_log.append(entry)
        print(f"  Gen {gen+1:2d}: best={best_fit:.1f}  ({status})")

    # Return a run_ga compatible full chromosome (0/1 binaries + continuous)
    best_chromosome = genes_to_bits(best["genes"], groups, n_binary) + list(best["x"])
    return best_chromosome, gen_log, n_evals
