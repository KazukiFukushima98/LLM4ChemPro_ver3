"""GA optimization (mixed-integer GA).

The topology representation is accessed through ss / active_topology.
The Aspen implementation is injected via the Evaluator Protocol (aspen_builder /
simulator are never imported directly).

Ported from: the old run_iteration.py
    _evaluate_population : L80-92
    run_ga               : L99-274 (the evaluation logic of evaluate_group has already
                           been moved into AspenEvaluator)
"""

import random
import sys
import os
import time
from collections import defaultdict
from typing import Any

from deap import base, creator, tools

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import (  # noqa: E402
    BAD_VALUE,
    ECONOMICS_DEFAULTS,
    Evaluator,
    Metrics,
    cost_per_tco2,
    membrane_areas_from_x,
)
from topology import (  # noqa: E402
    active_topology,
    binary_variables,
    continuous_variables,
    is_buildable,
    x_for_topology,
)


def _fitness(obj_value: float, purity: float, recovery: float,
             targets: dict, penalty_w: float) -> float:
    """f = obj + lambda*[max(0, pi_min - pi)^2 + max(0, rho_min - rho)^2]

    obj is the objective value (energy or cost, following the objective switch of 10.2).
    """
    if obj_value >= BAD_VALUE:
        return BAD_VALUE
    penalty = (
        penalty_w * max(0.0, targets["purity_min"]   - purity)   ** 2 +
        penalty_w * max(0.0, targets["recovery_min"] - recovery) ** 2
    )
    return obj_value + penalty


def run_ga(
    ss: dict,
    case: dict,
    evaluator: Evaluator,
    seed: int = 1,
) -> tuple[Any, list[dict], int]:
    """Optimize the continuous and binary variables of ss with a mixed GA.

    Parameters
    ----------
    ss        : SS template (the source of the binary / continuous variables).
    case      : contents of case.yaml (ga / optimization_targets / penalty_weight).
    evaluator : an Evaluator Protocol implementation (e.g. AspenEvaluator).
    seed      : random seed.

    Returns
    -------
    best     : the best individual (DEAP Individual).
    gen_log  : [{"gen": i, "best_fitness": f}, ...] (length n_gen).
    n_evals  : total number of Aspen evaluations.
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, case.get("membrane_model"))
    n_binary  = len(bin_vars)
    n_cont    = len(cont_vars)

    ga_cfg    = case["ga"]
    pop_size  = ga_cfg["pop_size"]
    n_gen     = ga_cfg["n_gen"]
    targets   = case["optimization_targets"]

    # ---- objective switch (10.2): energy / cost ($/tCO2) ----
    cost_mode = str(targets.get("objective", "")).strip() == "minimize_cost"
    if cost_mode:
        econ = {**ECONOMICS_DEFAULTS, **(case.get("economics") or {})}
        penalty_w = float(econ["penalty_weight"])   # lambda on the cost scale (Lee's r)
    else:
        penalty_w = float(case.get("penalty_weight", 1e5))  # explicit cast: YAML 1.1 returns an unsigned exponent (1.0e5) as str

    def _objective(m: Metrics, x_cont: list, topology: dict) -> float:
        if not cost_mode or m.specific_energy >= BAD_VALUE:
            return float(m.specific_energy)
        areas = membrane_areas_from_x(x_cont, cont_vars, topology)
        return cost_per_tco2(m, areas, case)

    bounds     = [[0.0, 1.0]] * n_binary + [cv["bounds"] for cv in cont_vars]
    sigma_cont = [(b[1] - b[0]) * 0.15 for b in [cv["bounds"] for cv in cont_vars]]

    if not hasattr(creator, "FitnessMin"):
        creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
    if not hasattr(creator, "Individual"):
        creator.create("Individual", list, fitness=creator.FitnessMin)

    toolbox = base.Toolbox()
    toolbox.register("individual", lambda: creator.Individual(
        [random.uniform(lo, hi) for lo, hi in bounds]
    ))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("select", tools.selTournament, tournsize=3)

    def clip(ind: Any) -> None:
        for k, (lo, hi) in enumerate(bounds):
            ind[k] = max(lo, min(hi, ind[k]))

    def crossover(c1: Any, c2: Any) -> None:
        # uniform crossover for binary part (old run_iteration.py:127-131)
        for k in range(n_binary):
            if random.random() < 0.5:
                c1[k], c2[k] = c2[k], c1[k]
        # blend crossover for continuous part (old :132-138)
        for idx in range(n_cont):
            k = n_binary + idx
            gamma = (1.0 + 2.0 * 0.5) * random.random() - 0.5
            v1, v2 = c1[k], c2[k]
            c1[k] = (1.0 - gamma) * v1 + gamma * v2
            c2[k] = gamma * v1 + (1.0 - gamma) * v2

    def mutate(ind: Any) -> None:
        # flip mutation for binary part (old :141-144)
        for k in range(n_binary):
            if random.random() < 0.5:
                ind[k] = 1.0 - float(round(ind[k]))
        # Gaussian mutation for continuous part (old :145-149)
        for idx in range(n_cont):
            k = n_binary + idx
            if random.random() < 0.5:
                ind[k] += random.gauss(0, sigma_cont[idx])

    def _evaluate_population(individuals: list) -> int:
        """Group by binary key, then evaluate each group in one Aspen batch. (old :80-92)"""
        groups: dict[tuple, list] = defaultdict(list)
        for ind in individuals:
            key = tuple(int(ind[k] > 0.5) for k in range(n_binary))
            groups[key].append(ind)
        total = 0
        for binary_key, inds in groups.items():
            q_active = {bv["name"]: binary_key[i] for i, bv in enumerate(bin_vars)}
            topology = active_topology(ss, q_active)
            reason = is_buildable(topology)
            if reason is not None:
                # Unbuildable combinations (splitting a non-membrane stream) are rejected as
                # BAD without running Aspen
                metrics_list = [Metrics.bad() for _ in inds]
            else:
                # The continuous x spans all template dimensions; narrow it down to the
                # variables of the concrete topology (after pruning) before passing it on,
                # so it lines up with the positional zip on the evaluator side
                # (topology.x_for_topology)
                x_list = [
                    x_for_topology(list(ind[n_binary:]), cont_vars, topology)
                    for ind in inds
                ]
                metrics_list = evaluator.evaluate_topology(topology, x_list)
            for ind, m in zip(inds, metrics_list):
                obj = _objective(m, list(ind[n_binary:]), topology)
                ind.fitness.values = (_fitness(obj, m.purity, m.recovery, targets, penalty_w),)
            total += len(inds)
        return total

    random.seed(seed)
    t0      = time.monotonic()   # "t" in gen_log = seconds elapsed since the start of optimization
    pop     = toolbox.population(n=pop_size)
    n_evals = _evaluate_population(pop)

    hof     = tools.HallOfFame(1)
    gen_log: list[dict] = []
    hof.update(pop)

    for gen in range(n_gen):
        offspring = [toolbox.clone(ind) for ind in toolbox.select(pop, len(pop))]

        for c1, c2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < 0.7:
                crossover(c1, c2)
                clip(c1)
                clip(c2)
                del c1.fitness.values, c2.fitness.values

        for mut in offspring:
            if random.random() < 0.3:
                mutate(mut)
                clip(mut)
                del mut.fitness.values

        invalid = [ind for ind in offspring if not ind.fitness.valid]
        _t_eval0 = time.monotonic()
        n_evals += _evaluate_population(invalid)
        t_eval_sec = round(time.monotonic() - _t_eval0, 2)   # wall-clock evaluation time of this generation

        pop[:] = offspring
        hof.update(pop)
        # Elitism: replace the worst individual with a clone of best .
        # Overwriting pop[-1] would merely erase whichever individual happened to land last and
        # gives no guarantee that best survives, so pick the maximum fitness explicitly
        # (the worst, since this is a minimization) and replace that one.
        # Invalid fitness is treated as inf so such individuals are replaced first.
        worst_idx = max(
            range(len(pop)),
            key=lambda i: pop[i].fitness.values[0] if pop[i].fitness.valid else float("inf"),
        )
        pop[worst_idx] = toolbox.clone(hof[0])
        best_fit = min(ind.fitness.values[0] for ind in pop if ind.fitness.valid)
        gen_log.append({"gen": gen + 1, "best_fitness": best_fit,
                        "t": round(time.monotonic() - t0, 1),
                        "t_eval": t_eval_sec})
        print(f"  Gen {gen+1:2d}: best={best_fit:.1f}")

    return hof[0], gen_log, n_evals
