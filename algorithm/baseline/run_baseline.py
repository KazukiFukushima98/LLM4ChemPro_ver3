"""Baseline runner (12.6 single-shot optimization): optimize a Lee Fig.2-style SS once.

The conventional approach used as a comparison against SST: fix a large
superstructure (ss_lee2/ss_lee3) and optimize the structural binaries together
with the continuous variables in a single shot, using BO or GA (no structural
transitions).

The existing SST code (src/) is left completely untouched. Every difference is
confined to the "runtime substitution" performed inside this script:
  [BO]  bo._build_fixed_features -> replaced by a function that simply returns an
        analytically generated list of valid one-hot combinations (exactly one
        arc ON per outlet). The default 2^n exhaustive enumeration is impractical:
        the 3-stage case (n=24) would yield 16.77 million combinations.
        bo._sobol_initial -> replaced by a wrapper that snaps the binary part of
        the initial points onto valid one-hot combinations (with independent bit
        rounding, nearly every initial point would be unbuildable).
  [GA]  run_iteration.run_ga -> replaced by baseline/ga_onehot.run_ga_onehot
        (one categorical gene per outlet selecting its destination; see the header
        of ga_onehot.py for details).

Budget (user decision, 2026-07-12): the single-shot side is given about 2,000
evaluations, clearly more than SST (1,449 evaluations measured in run24), and
patience is disabled (0), i.e. no early termination. This setting leaves no room
for the excuse that "there was not enough time".

Wall-clock protocol (user decision, 2026-07-16, new-model campaign): the primary
budget is wall-clock — fix the population size and cut off at a GENERATION
BOUNDARY once --max-hours (default 6.5 h, matching SST run30's 6.1 h) has
elapsed. Two population patterns (--pop 40 / --pop 100) are run per SS to show
the choice of population is not the reason for the outcome. n_gen is then only
a safety cap.

usage (from algorithm/):
    uv run python baseline/run_baseline.py --ss lee2 --optimizer bo [--pilot] [--force]
    uv run python baseline/run_baseline.py --ss lee3 --optimizer ga --pop 40 --max-hours 6.5
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ALGO = os.path.normpath(os.path.join(HERE, ".."))
SRC = os.path.join(ALGO, "src")
for p in (SRC, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import topology as T  # noqa: E402

# Production budget: about 2,000 evaluations (~1.4x the 1,449 measured for SST
# run24), patience disabled
FULL_BO = {"n_init": 100, "n_iter": 475, "q_batch": 4, "patience": 0}
FULL_GA = {"pop_size": 40, "n_gen": 50}                     # 40 + 50*40 = 2,040
# Pilot: reduced budget for checking the plumbing (real Aspen, a few to ~15 minutes)
PILOT_BO = {"n_init": 8, "n_iter": 4, "q_batch": 2, "patience": 0}
PILOT_GA = {"pop_size": 6, "n_gen": 3}


# =========================================================
# one-hot generation (the substance of the BO substitution; also imported by tests)
# =========================================================

def build_onehot_fixed_features(ss: dict[str, Any]) -> list[dict[int, float]]:
    """Analytically enumerate every valid "exactly one destination per outlet" combination.

    Instead of the 2^n exhaustive enumeration (the default of
    bo._build_fixed_features), build only the Cartesian product of the choices per
    group (outlet) = prod|group| combinations.
    lee2: 3^4 = 81, lee3: 4^6 = 4096.
    """
    from ga_onehot import onehot_groups
    bin_vars = T.binary_variables(ss)
    groups = onehot_groups(ss)
    combos: list[dict[int, float]] = []
    for picks in itertools.product(*[range(len(g)) for g in groups]):
        ff = {i: 0.0 for i in range(len(bin_vars))}
        for g, choice in zip(groups, picks):
            ff[g[choice]] = 1.0
        combos.append(ff)
    return combos


def assert_all_buildable(ss: dict[str, Any], combos: list[dict[int, float]]) -> None:
    """Runtime check that every one-hot combination is buildable (a design invariant)."""
    bin_vars = T.binary_variables(ss)
    for ff in combos:
        q_active = {bv["name"]: int(ff[i]) for i, bv in enumerate(bin_vars)}
        reason = T.is_buildable(T.active_topology(ss, q_active))
        assert reason is None, f"one-hot combo unbuildable: {q_active} -> {reason}"


def patch_bo_for_onehot(ss: dict[str, Any], seed: int) -> None:
    """Inject one-hot support into the bo module at runtime (its source is unchanged).

    - _build_fixed_features: becomes a function that just returns the pre-generated
      one-hot list
    - _sobol_initial: becomes a wrapper that keeps the original Sobol points for the
      continuous part and overwrites the binary part with a one-hot combination
      (chosen at random, deterministically from the seed)
    """
    import random

    import bo as bo_mod

    combos = build_onehot_fixed_features(ss)
    # The start-up check is a sample (<=128 combinations, deterministic). The
    # exhaustive check (lee3=4096, ~10 min) is proven by BASELINE_FULL=1 in
    # tests/test_baseline.py (PASS on 2026-07-12).
    step = max(1, len(combos) // 128)
    assert_all_buildable(ss, combos[::step])

    bo_mod._build_fixed_features = lambda _ss, _bin_vars: combos

    orig_sobol = bo_mod._sobol_initial
    rng = random.Random(seed * 7919 + 1)

    def sobol_onehot(n_init, bounds, n_bin, sobol_seed):
        x = orig_sobol(n_init, bounds, n_bin, sobol_seed)
        if n_bin > 0:
            for row in range(x.shape[0]):
                ff = combos[rng.randrange(len(combos))]
                for i in range(n_bin):
                    x[row, i] = ff[i]
        return x

    bo_mod._sobol_initial = sobol_onehot
    print(f"[baseline] BO one-hot injection: {len(combos)} valid combination(s) "
          f"(avoiding the 2^{len(T.binary_variables(ss))} exhaustive enumeration)")


# =========================================================
# Execution
# =========================================================

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ss", choices=["lee2", "lee3", "lee3_blower", "lee4_blower"], required=True,
                        help="lee2/lee3 = Lee-consistent variable compression (1.0-4.0 bar); "
                             "*_blower = blower campaign (COMP fixed at 1.1 bar, matches run27+)")
    parser.add_argument("--optimizer", choices=["bo", "ga"], required=True)
    parser.add_argument("--pilot", action="store_true", help="check the plumbing on a reduced budget")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="append to an existing run directory")
    parser.add_argument("--pop", type=int, default=None,
                        help="GA population size override (also suffixes the run name)")
    parser.add_argument("--selection", choices=["deb", "penalty"], default="deb",
                        help="GA selection rule: Deb's parameter-free feasibility rule "
                             "(default) or the legacy penalized fitness")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="wall-clock budget; the GA stops at the first generation "
                             "boundary past this. With it, n_gen is only a safety cap.")
    args = parser.parse_args()

    import run_iteration as RI

    seed_path = os.path.join(HERE, f"ss_{args.ss}.json")
    run_name = f"baseline_{args.ss}_{args.optimizer}"
    if args.optimizer == "ga":
        run_name += f"_{args.selection}"
    if args.pop is not None:
        run_name += f"_pop{args.pop}"
    if args.seed != 1:
        run_name += f"_seed{args.seed}"
    if args.pilot:
        run_name += "_pilot"
    base_dir = os.path.join(ALGO, "runs", run_name)

    if os.path.exists(os.path.join(base_dir, "iterations")) and not args.force:
        raise SystemExit(f"{base_dir} already contains results (overwrite guard). Use --force to append.")
    os.makedirs(base_dir, exist_ok=True)
    shutil.copyfile(seed_path, os.path.join(base_dir, "ss_current.json"))

    # Read case.yaml and override in memory only (the file is left unchanged)
    case = RI.load_case()
    case["optimizer"] = args.optimizer
    if args.optimizer == "bo":
        case["bo"] = dict(PILOT_BO if args.pilot else FULL_BO)
    else:
        case["ga"] = dict(PILOT_GA if args.pilot else FULL_GA)
        case["ga"]["selection"] = args.selection
        # Per-evaluation JSONL log (structure-space map / constraint-plane figures)
        case["eval_log_path"] = os.path.join(base_dir, "eval_log.jsonl")
        if args.pop is not None:
            case["ga"]["pop_size"] = args.pop
        if args.max_hours is not None:
            case["ga"]["max_wall_sec"] = args.max_hours * 3600.0
            # In wall-clock mode n_gen is only a safety cap, far above what the
            # budget can reach (pilot keeps its small n_gen for quick plumbing checks)
            if not args.pilot:
                case["ga"]["n_gen"] = 100_000

    ss = T.load_ss(seed_path)
    n_bin = len(T.binary_variables(ss))
    n_cont = len(T.continuous_variables(ss, case.get("membrane_model")))
    if args.optimizer == "ga" and args.max_hours is not None:
        budget = f"wall-clock {args.max_hours}h (generation-boundary cutoff)"
    elif args.optimizer == "bo":
        budget = f"~{case['bo']['n_init'] + case['bo']['n_iter'] * case['bo']['q_batch']} evaluations"
    else:
        budget = f"~{case['ga']['pop_size'] * (case['ga']['n_gen'] + 1)} evaluations"
    print(f"[baseline] SS={args.ss} ({n_bin} binary, {n_cont} continuous), "
          f"optimizer={args.optimizer}, budget = {budget}, pilot={args.pilot}")

    # Runtime substitution (confined to this script; src is unchanged)
    if args.optimizer == "bo":
        patch_bo_for_onehot(ss, args.seed)
    else:
        import ga_onehot
        RI.run_ga = ga_onehot.run_ga_onehot
        print("[baseline] GA one-hot injection: run_iteration.run_ga -> ga_onehot.run_ga_onehot")

    results = RI.run_one_iteration(base_dir, case, commit=False)

    perf = results.get("performance", {})
    summary = {
        "run": run_name, "ss": args.ss, "optimizer": args.optimizer,
        "pilot": args.pilot, "budget": budget,
        "n_evaluations": results.get("n_evaluations"),
        "performance": perf,
        "active_candidates": results.get("active_candidates"),
    }
    with open(os.path.join(base_dir, "baseline_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[baseline] done: {base_dir}")


if __name__ == "__main__":
    # Workaround for cp932 consoles (same hardening as apply_ss.main)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
