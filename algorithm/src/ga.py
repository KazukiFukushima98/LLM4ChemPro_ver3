"""GA 最適化（混合整数 GA）。

トポロジー表現は ss / active_topology 経由。
Aspen 実装は Evaluator Protocol 経由で注入（aspen_builder / simulator を直接 import しない）。

移植元: 旧 run_iteration.py
    _evaluate_population : L80-92
    run_ga               : L99-274（evaluate_group の評価ロジックは AspenEvaluator に移管済み）
"""

import random
import sys
import os
from collections import defaultdict
from typing import Any

from deap import base, creator, tools

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import BAD_VALUE, Evaluator, Metrics  # noqa: E402
from topology import (  # noqa: E402
    active_topology,
    binary_variables,
    continuous_variables,
    is_buildable,
    x_for_topology,
)


def _fitness(m: Metrics, targets: dict, penalty_w: float) -> float:
    """f = E + λ[max(0, π_min−π)² + max(0, ρ_min−ρ)²]"""
    if m.specific_energy >= BAD_VALUE:
        return BAD_VALUE
    penalty = (
        penalty_w * max(0.0, targets["purity_min"]   - m.purity)   ** 2 +
        penalty_w * max(0.0, targets["recovery_min"] - m.recovery) ** 2
    )
    return m.specific_energy + penalty


def run_ga(
    ss: dict,
    case: dict,
    evaluator: Evaluator,
    seed: int = 1,
) -> tuple[Any, list[dict], int]:
    """Mixed GA で ss の連続・バイナリ変数を最適化する。

    Parameters
    ----------
    ss        : SS テンプレート（binary / continuous 変数の導出元）。
    case      : case.yaml の内容（ga / optimization_targets / penalty_weight）。
    evaluator : Evaluator Protocol 実装（AspenEvaluator など）。
    seed      : 乱数シード。

    Returns
    -------
    best     : 最良個体 (DEAP Individual)。
    gen_log  : [{"gen": i, "best_fitness": f}, ...] (長さ n_gen)。
    n_evals  : 総 Aspen 評価回数。
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, case.get("membrane_model"))
    n_binary  = len(bin_vars)
    n_cont    = len(cont_vars)

    ga_cfg    = case["ga"]
    pop_size  = ga_cfg["pop_size"]
    n_gen     = ga_cfg["n_gen"]
    targets   = case["optimization_targets"]
    penalty_w = float(case.get("penalty_weight", 1e5))  # YAML 1.1 は指数符号なし(1.0e5)を str で返すため明示変換

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
        # uniform crossover for binary part (旧 run_iteration.py:127-131)
        for k in range(n_binary):
            if random.random() < 0.5:
                c1[k], c2[k] = c2[k], c1[k]
        # blend crossover for continuous part (旧 :132-138)
        for idx in range(n_cont):
            k = n_binary + idx
            gamma = (1.0 + 2.0 * 0.5) * random.random() - 0.5
            v1, v2 = c1[k], c2[k]
            c1[k] = (1.0 - gamma) * v1 + gamma * v2
            c2[k] = gamma * v1 + (1.0 - gamma) * v2

    def mutate(ind: Any) -> None:
        # flip mutation for binary part (旧 :141-144)
        for k in range(n_binary):
            if random.random() < 0.5:
                ind[k] = 1.0 - float(round(ind[k]))
        # Gaussian mutation for continuous part (旧 :145-149)
        for idx in range(n_cont):
            k = n_binary + idx
            if random.random() < 0.5:
                ind[k] += random.gauss(0, sigma_cont[idx])

    def _evaluate_population(individuals: list) -> int:
        """binary key でグループ化 → 各グループを一括 Aspen 評価。(旧 :80-92)"""
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
                # ビルド不能な組み合わせ（非膜の分流）は Aspen を回さず BAD で落とす
                metrics_list = [Metrics.bad() for _ in inds]
            else:
                # 連続 x はテンプレート全次元 → 具体トポロジー（pruning 後）の変数だけに
                # 絞って渡す（evaluator 側の位置 zip との整列。topology.x_for_topology）
                x_list = [
                    x_for_topology(list(ind[n_binary:]), cont_vars, topology)
                    for ind in inds
                ]
                metrics_list = evaluator.evaluate_topology(topology, x_list)
            for ind, m in zip(inds, metrics_list):
                ind.fitness.values = (_fitness(m, targets, penalty_w),)
            total += len(inds)
        return total

    random.seed(seed)
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
        n_evals += _evaluate_population(invalid)

        pop[:] = offspring
        hof.update(pop)
        # エリート保存: best のクローンで worst を置換（COORDINATION 4b-(1) 対応）。
        # pop[-1] への上書きでは「末尾がたまたま選ばれた個体」を消すだけで best 残存の保証がないため、
        # 明示的に最大 fitness（最小化なので最悪）を選んで置換する。
        # 無効な fitness は inf 扱いで優先的に置換対象にする。
        worst_idx = max(
            range(len(pop)),
            key=lambda i: pop[i].fitness.values[0] if pop[i].fitness.valid else float("inf"),
        )
        pop[worst_idx] = toolbox.clone(hof[0])
        best_fit = min(ind.fitness.values[0] for ind in pop if ind.fitness.valid)
        gen_log.append({"gen": gen + 1, "best_fitness": best_fit})
        print(f"  Gen {gen+1:2d}: best={best_fit:.1f}")

    return hof[0], gen_log, n_evals
