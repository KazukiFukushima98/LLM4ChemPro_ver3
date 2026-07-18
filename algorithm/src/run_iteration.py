"""Inner-loop driver: load SS -> GA -> detailed eval of best -> save results.json -> print signals -> auto-commit.

A reduced port of main / get_next_iter_num / auto_commit_iteration from the old
run_iteration.py. The GA itself lives in ga.run_ga, Aspen evaluation in
simulator.AspenEvaluator, and signal extraction in signals.
This module is only the driver that loads, wires, saves and commits.

CLI:
    uv run python algorithm/src/run_iteration.py --base-dir runs/runN
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import signals as sig_mod  # noqa: E402
import topology as T  # noqa: E402
from evaluator import (  # noqa: E402
    BAD_VALUE,
    DetailedResult,
    Metrics,
    cost_per_tco2,
    membrane_areas_from_x,
)
from ga import run_ga  # noqa: E402
from subprocess_evaluator import SubprocessEvaluator, _default_kill_aspen  # noqa: E402
from topology import (  # noqa: E402
    active_topology,
    binary_variables,
    cont_vars_for_topology,
    continuous_variables,
    is_buildable,
    x_for_topology,
)

# bo.py imports torch/botorch, so it is imported lazily (not loaded on the default
# optimizer="ga" path).


# =========================================================
# Fixed paths (ARCH 8, 10)
# =========================================================

HERE = os.path.dirname(os.path.abspath(__file__))
ASPEN_FILE = os.path.join(HERE, "YAspen", "Yaspen.apw")
DMP_DIR    = os.path.join(HERE, "YAspen")
CASE_PATH  = os.path.normpath(os.path.join(HERE, "..", "case.yaml"))
# REPO_ROOT = two levels above algorithm/src = the project root (LLM4ChemPro_ver2/)
# The old version used dirname(dirname(__file__)), treating algorithm/ as the git root.
# ver2 corrects this to the parent (ARCH 10).
REPO_ROOT  = os.path.dirname(os.path.dirname(HERE))


# =========================================================
# Determining the iteration number
# =========================================================

def get_next_iter_num(base_dir: str) -> int:
    """Return max + 1 of the numeric part of iterations/iter_NNN (gaps and deleted numbers are never reused).

    Collect numbers only from entries whose remainder after stripping the iter_ prefix is
    all digits and which are directories, and return max + 1. If there are none, return 1
    (iterations start at 1).
    Foreign or non-directory entries such as iter_log / iter_005_log.txt are skipped rather
    than raising.
    """
    iter_dir = os.path.join(base_dir, "iterations")
    os.makedirs(iter_dir, exist_ok=True)
    nums: list[int] = []
    for name in os.listdir(iter_dir):
        if not name.startswith("iter_"):
            continue
        suffix = name[len("iter_"):]
        if suffix.isdigit() and os.path.isdir(os.path.join(iter_dir, name)):
            nums.append(int(suffix))
    return max(nums) + 1 if nums else 1


# =========================================================
# Assembling results.json
# =========================================================

def build_results_dict(
    iter_num: int,
    best: Any,
    ss: dict,
    detailed: Any,
    gen_log: list[dict],
    n_evals: int,
    optimizer: str = "ga",
    seed: int | None = None,
    case: dict | None = None,
) -> dict:
    """Assemble a dict following the results.json schema of ARCH 3.7.

    optimizer / seed are recorded for reproducibility (which optimizer produced the result
    under which random seed; seed=iteration number can drift with the directory state, so
    the value itself is stored).
    Passing case (1) aligns the naming and dimensionality of the permeance variables with
    the GA/BO side via membrane_model, and (2) records cost_usd_per_tCO2 in performance
    (12.2: both energy and cost are always recorded regardless of the objective setting,
    which guarantees comparability).

    Among the continuous variables, optimal_params records **only those present in the best
    topology**. Variables of units removed by pruning are free dimensions that do not affect
    the evaluation; recording whatever value the optimizer happened to leave there (often a
    bound) would make bounds_hit report a "bound hit on a membrane that does not exist" and
    mislead the SST agent (ghost-signal prevention, review comment 2026-07-10).
    """
    membrane_model = (case or {}).get("membrane_model")
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, membrane_model)
    n_binary  = len(bin_vars)

    q_active: dict[str, int] = {bv["name"]: int(best[k] > 0.5) for k, bv in enumerate(bin_vars)}
    topology_best = active_topology(ss, q_active)

    # Ghost-signal prevention: keep only the variables of the best topology, filtered by the
    # shared predicate of cont_vars_for_topology / x_for_topology (names and values stay
    # aligned because the same filter is applied to both).
    x_cont   = [float(best[n_binary + k]) for k in range(len(cont_vars))]
    kept_cvs = cont_vars_for_topology(cont_vars, topology_best)
    kept_x   = x_for_topology(x_cont, cont_vars, topology_best)
    optimal_params: dict[str, Any] = dict(q_active)
    for cv, v in zip(kept_cvs, kept_x):
        optimal_params[cv["name"]] = v

    m = detailed.metrics
    performance: dict[str, Any] = {
        "CO2_purity":               m.purity,
        "CO2_recovery":             m.recovery,
        "specific_energy_kWh_tCO2": m.specific_energy,
        "total_compressor_kW":      sum(m.energy_breakdown.values()),
    }
    if case is not None:
        # 12.2: always record cost regardless of the objective setting (comparability with
        # energy). This is supplementary information, so a failure (e.g. an unsupported feed
        # format) must not bring the iteration down.
        try:
            areas = membrane_areas_from_x(x_cont, cont_vars, topology_best)
            performance["cost_usd_per_tCO2"] = cost_per_tco2(m, areas, case)
        except Exception as e:
            print(f"    cost recording skipped: {e}")

    return {
        "iteration": iter_num,
        "optimizer": optimizer,
        "seed": seed,
        "performance": performance,
        "optimal_params":    optimal_params,
        "active_candidates": q_active,
        "stream_results":    detailed.stream_results,
        "energy_breakdown":  dict(m.energy_breakdown),
        "gen_log":           gen_log,
        "n_evaluations":     n_evals,
    }


# =========================================================
# auto-commit
# =========================================================

def _driving_ss_change_reason(base_dir: str, iter_num: int) -> str:
    """The ss_change.json that drove this iteration lives in iter_{iter_num-1}/ss_change.json.

    For iter_1 there is no driving change (the initial SS).
    """
    if iter_num <= 1:
        return "(initial — no prior ss_change.json)"
    prior = os.path.join(base_dir, f"iterations/iter_{iter_num-1:03d}/ss_change.json")
    if not os.path.exists(prior):
        return f"(no ss_change.json at iter_{iter_num-1:03d})"
    try:
        with open(prior, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("reason", "(no reason field)")
    except Exception as e:
        return f"(failed to read prior ss_change.json: {e})"


def _format_bounds_hit(signals_obj: sig_mod.Signals) -> str:
    if not signals_obj.bounds_hit:
        return " (none)"
    items = [
        f"{h.name}={h.value:.4g} ({h.side} {h.limit:.4g})"
        for h in signals_obj.bounds_hit
    ]
    return "\n  - " + "\n  - ".join(items)


def auto_commit_iteration(
    base_dir: str,
    iter_num: int,
    results: dict,
    signals_obj: sig_mod.Signals,
) -> None:
    """git add -A + commit. runs/ is excluded by .gitignore, so only code and docs are staged (ARCH 10).

    On failure, print and swallow the error (same as the old version).
    """
    try:
        run_name = os.path.basename(os.path.normpath(os.path.abspath(base_dir)))
        perf     = results.get("performance", {})
        purity   = float(perf.get("CO2_purity", 0.0)) * 100
        recovery = float(perf.get("CO2_recovery", 0.0)) * 100
        spec_e   = float(perf.get("specific_energy_kWh_tCO2", 0.0))

        ss_change_reason = _driving_ss_change_reason(base_dir, iter_num)
        active_cands = results.get("active_candidates", {}) or {}
        active_cands_str = str(active_cands) if active_cands else "(none)"

        cost = perf.get("cost_usd_per_tCO2")
        cost_str = (
            f" cost={cost:.1f}$/t"
            if isinstance(cost, (int, float)) and cost < BAD_VALUE  # do not display the sentinel value
            else ""
        )
        title = (
            f"{run_name} iter{iter_num:03d}: "
            f"E={spec_e:.0f}kWh/tCO2{cost_str} purity={purity:.1f}% recovery={recovery:.1f}%"
        )
        body = (
            f"## Changes\n"
            f"- SS-change: {ss_change_reason}\n"
            f"- Best: specific_energy={spec_e:.2f} kWh/tCO2, "
            f"purity={purity:.1f}%, recovery={recovery:.1f}%\n"
            f"- Active candidates: {active_cands_str}\n"
            f"\n"
            f"## Issues\n"
            f"- Bound-hitting variables:{_format_bounds_hit(signals_obj)}\n"
            f"- Other: TBD (add manually to docs/experiment_log.md)"
        )
        msg = f"{title}\n\n{body}"

        subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
        if diff.returncode == 0:
            print("[git] no staged changes, commit skipped")
            return
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True)
        print(f"[git] committed: {title}")
    except Exception as e:
        # An auto-commit failure must not affect the success of the run (same as the old version)
        print(f"[git] auto-commit failed (ignored): {e}")


# =========================================================
# Main / body of one iteration
# =========================================================

def load_case() -> dict:
    """Load algorithm/case.yaml.

    The caller can modify the case dict before passing it to run_one_iteration(), which
    allows overriding GA parameters and the like without touching the case.yaml file itself
    (used by scratch/dryrun_iteration.py).
    """
    with open(CASE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_ga_overrides(case: dict, pop: int | None, gen: int | None) -> dict:
    """Pure function that overrides pop/gen from the CLI onto an in-memory case dict.

    The case.yaml file is never written (dry runs can be scaled down without polluting the
    config file). Entries whose pop/gen is None are not overridden (the case.yaml value is
    used). The input dict is not modified (a copy is returned when there is an override).
    """
    if pop is None and gen is None:
        return case
    new_case = copy.deepcopy(case)
    ga = new_case.setdefault("ga", {})
    if pop is not None:
        ga["pop_size"] = pop
    if gen is not None:
        ga["n_gen"] = gen
    return new_case


def apply_bo_overrides(
    case: dict,
    optimizer: str | None,
    n_init: int | None,
    n_iter: int | None,
    q_batch: int | None,
) -> dict:
    """Pure function that overrides the optimizer / BO parameters from the CLI onto an in-memory case dict.

    case.yaml is left untouched (so BO validation runs can be scaled down without polluting
    the file). Entries that are None are not touched. The input dict is not modified.
    """
    if all(v is None for v in (optimizer, n_init, n_iter, q_batch)):
        return case
    new_case = copy.deepcopy(case)
    if optimizer is not None:
        new_case["optimizer"] = optimizer
    bo = new_case.setdefault("bo", {})
    if n_init  is not None: bo["n_init"]  = n_init
    if n_iter  is not None: bo["n_iter"]  = n_iter
    if q_batch is not None: bo["q_batch"] = q_batch
    return new_case


def evaluate_detailed_with_retry(
    evaluator: Any,
    topology: dict,
    x: list[float],
    retries: int = 2,
) -> Any:
    """Guard against transient wedges in the detailed eval of best: retry up to `retries` times if bad is returned.

    A point that succeeds during batch evaluation can still fail with a COM wedge in the
    detailed evaluation alone (a fresh build plus re-convergence); this was observed on two
    consecutive iterations in run22, with the real value recovered on the first retry
    (i.e. transient). The detailed evaluation is a single point, so re-running is cheap
    (a few minutes), whereas leaving a sentinel value in performance in results.json would
    blind the outer loop's stopping decision and recycle judgement (stream_results). It is
    therefore worth persisting here.
    """
    detailed = evaluator.evaluate_detailed(topology, x)
    for attempt in range(retries):
        if detailed.metrics.specific_energy < BAD_VALUE:
            break
        print(f"    detailed eval failed (attempt {attempt + 1}/{retries + 1}) — retrying...")
        detailed = evaluator.evaluate_detailed(topology, x)
    return detailed


def run_one_iteration(base_dir: str, case: dict, commit: bool = True) -> dict:
    """Body of a single iteration. The caller supplies the case dict (no file I/O here).

    Returns
    -------
    dict
        The same content as the results.json that was written.
    """
    iter_num = get_next_iter_num(base_dir)
    iter_dir = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}")
    os.makedirs(iter_dir, exist_ok=True)

    print("=" * 60)
    print(f"Iteration {iter_num}  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"base_dir:  {base_dir}")
    print(f"iter_dir:  {iter_dir}")
    print("=" * 60)

    # Load the SS and save a snapshot
    ss_path = os.path.join(base_dir, "ss_current.json")
    ss = T.load_ss(ss_path)
    T.save_ss(ss, os.path.join(iter_dir, "ss_snapshot.json"))

    # Per-evaluation JSONL log (2026-07-18, structure-space map / constraint-plane
    # figures): every inner-loop evaluation of this iteration is appended to the
    # iteration directory. Consumed by bo.py (records "bits"); optimizers that do
    # not read the key simply ignore it.
    case["eval_log_path"] = os.path.join(iter_dir, "eval_log.jsonl")

    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, case.get("membrane_model"))
    print(f"\n[1] SS: {len(ss['vertices'])} vertices, units={list(ss['units'])}")
    print(f"    binary={[bv['name'] for bv in bin_vars]}")
    print(f"    continuous={[cv['name'] for cv in cont_vars]}")
    optimizer = case.get("optimizer", "ga")
    if optimizer == "bo":
        bo_cfg = {**{"n_init": 16, "n_iter": 40, "q_batch": 4}, **case.get("bo", {})}
        print(f"\n[2] BO: n_init={bo_cfg['n_init']}, n_iter={bo_cfg['n_iter']}, "
              f"q_batch={bo_cfg['q_batch']}, seed={iter_num}")
    else:
        # The ga section is required only on the GA path (dropping it from a BO case.yaml
        # does not break anything)
        ga_cfg = case["ga"]
        print(f"\n[2] GA: pop={ga_cfg['pop_size']}, gen={ga_cfg['n_gen']}, seed={iter_num}")

    # Drive the inner loop. Evaluation is process-isolated (SubprocessEvaluator). The parent
    # never touches COM, so no out-of-band watchdog is needed: the parent's stall monitoring
    # (time since the last received result) covers everything.
    evaluator = SubprocessEvaluator(case, ASPEN_FILE, DMP_DIR)
    _t_inner0 = time.monotonic()
    if optimizer == "bo":
        from bo import run_bo  # lazy import: torch/botorch is not loaded on the default GA path
        best, gen_log, n_evals = run_bo(ss, case, evaluator, seed=iter_num)
    else:
        best, gen_log, n_evals = run_ga(ss, case, evaluator, seed=iter_num)
    inner_opt_sec = round(time.monotonic() - _t_inner0, 1)

    # Detailed evaluation of best (including stream_results)
    print("\n[3] Evaluating best solution (detailed)...")
    _t_detail0 = time.monotonic()
    n_binary = len(bin_vars)
    q_active = {bv["name"]: int(best[k] > 0.5) for k, bv in enumerate(bin_vars)}
    topology_best = active_topology(ss, q_active)
    x_best = [float(best[n_binary + k]) for k in range(len(cont_vars))]
    build_reason = is_buildable(topology_best)
    if build_reason is not None:
        # If best lands on an unbuildable q (e.g. because every individual is penalized),
        # state bad explicitly instead of starting Aspen for nothing (this also reads as a
        # failure in results.json)
        print(f"    best topology is unbuildable ({build_reason}) — detailed eval skipped")
        detailed = DetailedResult(metrics=Metrics.bad())
    else:
        # Keep only the variables of the post-pruning topology (aligned with the evaluator's
        # positional zip)
        x_best_topo = x_for_topology(x_best, cont_vars, topology_best)
        detailed = evaluate_detailed_with_retry(evaluator, topology_best, x_best_topo)
    detailed_sec = round(time.monotonic() - _t_detail0, 1)

    # All evaluations of this iteration are done, so reclaim any leftover AspenPlus.exe
    # (this prevents it from holding memory and a license while the outer loop is thinking
    # before the next evaluation). Killing by image name is as safe here as it is during
    # evaluation, given the premise of sequential evaluation = one at a time. It always
    # returns thanks to the timeout.
    _default_kill_aspen()

    # Write results.json (atomically, so that the outer loop's authoritative record is not
    # corrupted by a crash midway)
    results = build_results_dict(
        iter_num, best, ss, detailed, gen_log, n_evals,
        optimizer=optimizer, seed=iter_num, case=case,
    )
    # Instrumentation (2026-07-15): a breakdown of wall-clock time. Together with
    # t/t_fit/t_acq/t_eval on the gen_log side, this lets later analysis separate
    # "Aspen vs optimization overhead" and "wedge losses".
    groups = evaluator.timing["groups"]
    results["timing"] = {
        "inner_opt_sec": inner_opt_sec,     # the whole inner optimization (run_bo/run_ga)
        "detailed_sec": detailed_sec,       # detailed evaluation of best (retries included)
        "eval_wall_sec": round(sum(g["wall_sec"] for g in groups), 1),   # total wall clock of Aspen evaluation
        "n_eval_groups": len(groups),
        "n_wedges": sum(1 for g in groups if g["wedged"]),
        "wedge_lost_sec": round(sum(g["lost_sec"] for g in groups), 1),  # time lost to wedges/anomalies
        "evaluator_groups": groups,         # per-group detail (incl. eval_sec, the receive interval of each evaluation)
    }
    results_path = os.path.join(iter_dir, "results.json")
    tmp_path = results_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, results_path)

    # Extract and print the signals
    signals_obj = sig_mod.extract(results, ss, case)
    print()
    print(sig_mod.summarize(signals_obj))

    # Summary
    perf = results["performance"]
    print()
    print("=" * 60)
    print(f"Iteration {iter_num} summary")
    print("=" * 60)
    print(f"  CO2 purity:       {perf['CO2_purity']*100:.1f}%")
    print(f"  CO2 recovery:     {perf['CO2_recovery']*100:.1f}%")
    print(f"  Specific energy:  {perf['specific_energy_kWh_tCO2']:.1f} kWh/tCO2")
    if "cost_usd_per_tCO2" in perf and perf["cost_usd_per_tCO2"] < BAD_VALUE:
        print(f"  Capture cost:     {perf['cost_usd_per_tCO2']:.2f} $/tCO2")
    print(f"  Total compressor: {perf['total_compressor_kW']:.1f} kW")
    print(f"  Evaluations:      {n_evals}")
    print(f"  Saved to:         {iter_dir}/")

    # auto-commit (ARCH 10: repo_root is the project root, runs/ is excluded by gitignore)
    # On a --no-commit dry run, skip it so that temporary pop/gen values are not dragged in.
    if commit:
        auto_commit_iteration(base_dir, iter_num, results, signals_obj)
    else:
        print("[git] --no-commit: auto-commit skipped")

    return results


def main() -> None:
    # The default Windows console is cp932, and printing non-cp932 characters (e.g. those in
    # an SS reason) raises UnicodeEncodeError. Pin the standard streams to UTF-8 for
    # robustness.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".",
                        help="run directory containing ss_current.json and iterations/")
    parser.add_argument("--pop", type=int, default=None,
                        help="override ga.pop_size in-memory (case.yaml is not modified)")
    parser.add_argument("--gen", type=int, default=None,
                        help="override ga.n_gen in-memory (case.yaml is not modified)")
    parser.add_argument("--optimizer", choices=["ga", "bo"], default=None,
                        help="override optimizer in-memory (case.yaml is not modified)")
    parser.add_argument("--bo-n-init",  type=int, default=None, help="override bo.n_init")
    parser.add_argument("--bo-n-iter",  type=int, default=None, help="override bo.n_iter")
    parser.add_argument("--bo-q-batch", type=int, default=None, help="override bo.q_batch")
    parser.add_argument("--no-commit", action="store_true",
                        help="skip the auto-commit at the end (for dry runs)")
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    case = load_case()
    case = apply_ga_overrides(case, args.pop, args.gen)
    case = apply_bo_overrides(case, args.optimizer, args.bo_n_init, args.bo_n_iter, args.bo_q_batch)

    # Warn on a mismatch between the optimizer and the scale-down flags: --pop/--gen are not
    # read when optimizer=bo (and vice versa). Do not let the "meant to dry-run but ran at
    # full scale" accident pass silently.
    effective_optimizer = case.get("optimizer", "ga")
    if effective_optimizer == "bo" and (args.pop is not None or args.gen is not None):
        print("[WARN] --pop/--gen have no effect because optimizer=bo. "
              "Use --bo-n-init/--bo-n-iter/--bo-q-batch to scale down BO.")
    if effective_optimizer != "bo" and any(
        v is not None for v in (args.bo_n_init, args.bo_n_iter, args.bo_q_batch)
    ):
        print("[WARN] the --bo-* flags have no effect because optimizer=ga. "
              "Use --pop/--gen to scale down the GA.")

    run_one_iteration(base_dir, case, commit=not args.no_commit)


if __name__ == "__main__":
    main()
