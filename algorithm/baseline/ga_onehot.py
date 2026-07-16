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
    targets  = case["optimization_targets"]

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

    # ---- individual = {"genes": [int]*G, "x": [float]*n, "fit": float | None} ----
    def new_individual() -> dict:
        return {
            "genes": [rng.randrange(len(g)) for g in groups],
            "x": [rng.uniform(lo, hi) for lo, hi in cont_bounds],
            "fit": None,
        }

    def clone(ind: dict) -> dict:
        return {"genes": list(ind["genes"]), "x": list(ind["x"]), "fit": ind["fit"]}

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

    def tournament(pop: list[dict], k: int) -> list[dict]:
        # Equivalent to tools.selTournament(tournsize=3) (an invalid fitness counts as inf)
        out = []
        for _ in range(k):
            cands = [pop[rng.randrange(len(pop))] for _ in range(3)]
            out.append(min(cands, key=lambda i: i["fit"] if i["fit"] is not None else float("inf")))
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
            for ind, m in zip(inds, metrics_list):
                obj = _objective(m, ind["x"], topology)
                ind["fit"] = _fitness(obj, m.purity, m.recovery, targets, penalty_w)
            total += len(inds)
        return total

    # ---- Main loop (faithfully mirrors the structure of ga.py) ----
    t0 = time.monotonic()   # "t" in gen_log = seconds elapsed since the start of the optimization
    pop = [new_individual() for _ in range(pop_size)]
    n_evals = evaluate(pop)

    best = min(pop, key=lambda i: i["fit"])
    best = clone(best)
    gen_log: list[dict] = []

    for gen in range(n_gen):
        offspring = [clone(ind) for ind in tournament(pop, len(pop))]

        for c1, c2 in zip(offspring[::2], offspring[1::2]):
            if rng.random() < 0.7:
                crossover(c1, c2)
                clip(c1)
                clip(c2)
                c1["fit"] = None
                c2["fit"] = None

        for mut in offspring:
            if rng.random() < 0.3:
                mutate(mut)
                clip(mut)
                mut["fit"] = None

        invalid = [ind for ind in offspring if ind["fit"] is None]
        _t_eval0 = time.monotonic()
        n_evals += evaluate(invalid)
        t_eval_sec = round(time.monotonic() - _t_eval0, 2)   # wall-clock evaluation time of this generation

        pop = offspring
        gen_best = min(pop, key=lambda i: i["fit"])
        if gen_best["fit"] < best["fit"]:
            best = clone(gen_best)
        # Elitism: replace the worst with a clone of the best (identical to ga.py)
        worst_idx = max(range(len(pop)),
                        key=lambda i: pop[i]["fit"] if pop[i]["fit"] is not None else float("inf"))
        pop[worst_idx] = clone(best)
        best_fit = min(ind["fit"] for ind in pop if ind["fit"] is not None)
        gen_log.append({"gen": gen + 1, "best_fitness": best_fit,
                        "t": round(time.monotonic() - t0, 1),
                        "t_eval": t_eval_sec})
        print(f"  Gen {gen+1:2d}: best={best_fit:.1f}")

    # Return a run_ga compatible full chromosome (0/1 binaries + continuous)
    best_chromosome = genes_to_bits(best["genes"], groups, n_binary) + list(best["x"])
    return best_chromosome, gen_log, n_evals
