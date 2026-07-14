"""比較対象（12.6 一括最適化）専用の one-hot GA。

Lee (2018) Fig.2 型の超構造では「各膜出口の行き先はちょうど1つ」（one-hot）が
構造制約になる。既存 ga.py のビット表現（バイナリ独立フリップ）だと、ランダムな
遺伝子が one-hot になる確率は 3段版で 1/4096 しかなく、集団のほぼ全員が建設不能
＝ペナルティ一色で進化の手がかりを失う（藁人形比較になる）。Lee 自身の GA も
排他性を前提に組まれているため、本モジュールは**出口1つにつき遺伝子1個
（値＝行き先の選択）**のカテゴリカル表現を使い、常に有効な構造だけを探索する。

既存 SST コードは一切変更しない：
- 評価境界（Evaluator）・トポロジー・コスト式・fitness 定義は src/ から import して共有
- 交叉/変異/選択の確率・分布・エリート保存は ga.py の定数を忠実にミラー
  （交叉 p=0.7・一様/blend、変異 p=0.3・遺伝子毎 p=0.5、トーナメント3、エリート1）
- 戻り値は run_ga と同一の (best, gen_log, n_evals)。best はバイナリ+連続の
  フル染色体（run_iteration.run_one_iteration がそのまま解釈できる形）

run_ga と同一シグネチャなので、ランナーが run_iteration.run_ga をこの関数に
差し替えるだけで既存の駆動・結果保存・signals がすべて流用できる。
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
from ga import _fitness  # noqa: E402  （fitness 定義を共有＝比較の同一性を保証）
from topology import (  # noqa: E402
    active_topology,
    binary_variables,
    continuous_variables,
    is_buildable,
    x_for_topology,
)


def onehot_groups(ss: dict[str, Any]) -> list[list[int]]:
    """候補アークを「同一出口（アークの src 頂点）」でグループ化して返す。

    各グループが one-hot 制約の単位（ちょうど1本 ON）。返り値は
    binary_variables(ss) のインデックスのリストのリスト（出現順・決定論的）。
    """
    bin_vars = binary_variables(ss)
    by_src: dict[str, list[int]] = {}
    for idx, bv in enumerate(bin_vars):
        by_src.setdefault(bv["arc"][0], []).append(idx)
    return list(by_src.values())


def genes_to_bits(genes: list[int], groups: list[list[int]], n_binary: int) -> list[float]:
    """構造遺伝子（グループ毎の選択番号）→ バイナリ 0/1 ベクトル。"""
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
    """one-hot 構造遺伝子の Mixed GA（run_ga と同一シグネチャ・同一戻り値形式）。

    染色体（内部表現）: [dest_1..dest_G | x_1..x_n]
        dest_g ∈ {0..len(group_g)-1}（出口 g の行き先の選択）
    戻り値の best は run_ga 互換の [q_1..q_m | x_1..x_n]（0/1 ＋連続）。
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

    # ---- 目的の切替（ga.py と同一）----
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
    sigma_cont  = [(hi - lo) * 0.15 for lo, hi in cont_bounds]  # ga.py と同一

    rng = random.Random(seed)

    # ---- 個体 = {"genes": [int]*G, "x": [float]*n, "fit": float | None} ----
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
        # 構造: 一様交叉（遺伝子毎 p=0.5。ga.py のバイナリ一様交叉に対応）
        for g in range(n_groups):
            if rng.random() < 0.5:
                c1["genes"][g], c2["genes"][g] = c2["genes"][g], c1["genes"][g]
        # 連続: blend（ga.py と同一の gamma 式）
        for k in range(n_cont):
            gamma = (1.0 + 2.0 * 0.5) * rng.random() - 0.5
            v1, v2 = c1["x"][k], c2["x"][k]
            c1["x"][k] = (1.0 - gamma) * v1 + gamma * v2
            c2["x"][k] = gamma * v1 + (1.0 - gamma) * v2

    def mutate(ind: dict) -> None:
        # 構造: 遺伝子毎 p=0.5 で「別の行き先」に付け替え
        # （ga.py のビット反転＝必ず値が変わる、に対応するカテゴリカル版）
        for g in range(n_groups):
            if rng.random() < 0.5 and len(groups[g]) > 1:
                cur = ind["genes"][g]
                alts = [c for c in range(len(groups[g])) if c != cur]
                ind["genes"][g] = rng.choice(alts)
        # 連続: ガウス変異（遺伝子毎 p=0.5・sigma は ga.py と同一）
        for k in range(n_cont):
            if rng.random() < 0.5:
                ind["x"][k] += rng.gauss(0, sigma_cont[k])

    def tournament(pop: list[dict], k: int) -> list[dict]:
        # tools.selTournament(tournsize=3) 相当（無効 fitness は inf 扱い）
        out = []
        for _ in range(k):
            cands = [pop[rng.randrange(len(pop))] for _ in range(3)]
            out.append(min(cands, key=lambda i: i["fit"] if i["fit"] is not None else float("inf")))
        return out

    def evaluate(individuals: list[dict]) -> int:
        """構造キーでグループ化 → 1トポロジー1ビルドで一括評価（ga.py と同じ畳み込み）。"""
        by_struct: dict[tuple, list[dict]] = defaultdict(list)
        for ind in individuals:
            by_struct[tuple(ind["genes"])].append(ind)
        total = 0
        for genes_key, inds in by_struct.items():
            bits = genes_to_bits(list(genes_key), groups, n_binary)
            q_active = {bv["name"]: int(b) for bv, b in zip(bin_vars, bits)}
            topology = active_topology(ss, q_active)
            reason = is_buildable(topology)
            if reason is not None:  # one-hot なら来ないはずの防御
                metrics_list = [Metrics.bad() for _ in inds]
            else:
                x_list = [x_for_topology(ind["x"], cont_vars, topology) for ind in inds]
                metrics_list = evaluator.evaluate_topology(topology, x_list)
            for ind, m in zip(inds, metrics_list):
                obj = _objective(m, ind["x"], topology)
                ind["fit"] = _fitness(obj, m.purity, m.recovery, targets, penalty_w)
            total += len(inds)
        return total

    # ---- メインループ（ga.py の構成を忠実にミラー）----
    t0 = time.monotonic()   # gen_log の "t"＝最適化開始からの経過秒
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
        t_eval_sec = round(time.monotonic() - _t_eval0, 2)   # この世代の評価実時間

        pop = offspring
        gen_best = min(pop, key=lambda i: i["fit"])
        if gen_best["fit"] < best["fit"]:
            best = clone(gen_best)
        # エリート保存: worst を best のクローンで置換（ga.py と同一）
        worst_idx = max(range(len(pop)),
                        key=lambda i: pop[i]["fit"] if pop[i]["fit"] is not None else float("inf"))
        pop[worst_idx] = clone(best)
        best_fit = min(ind["fit"] for ind in pop if ind["fit"] is not None)
        gen_log.append({"gen": gen + 1, "best_fitness": best_fit,
                        "t": round(time.monotonic() - t0, 1),
                        "t_eval": t_eval_sec})
        print(f"  Gen {gen+1:2d}: best={best_fit:.1f}")

    # run_ga 互換のフル染色体（0/1 バイナリ + 連続）で返す
    best_chromosome = genes_to_bits(best["genes"], groups, n_binary) + list(best["x"])
    return best_chromosome, gen_log, n_evals
