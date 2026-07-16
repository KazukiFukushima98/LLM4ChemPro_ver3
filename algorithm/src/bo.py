"""Bayesian optimization (standard BoTorch constrained-BO pipeline).

Inner loop that runs alongside the GA. Like ga.py, it evaluates points through the
Evaluator Protocol, and it exposes the same run_bo(ss, case, evaluator, seed)
signature so that it can be swapped in for ga.py.

Design (two-phase constrained BO; split into two phases from the run22 lessons of
2026-07-08):
    - **Phase 1 (bootstrap)**: while no feasible observation exists, energy is ignored
      entirely and the constraint shortfall max(0,pi_min-pi)+max(0,rho_min-rho) is
      minimized with a single GP + qLogEI.
      Rationale: when every point is infeasible, the P(feasible) term of CEI flattens
      out and CEI degenerates into a plain energy minimizer, so it never reaches the
      feasible basin on the high-energy side (observed in run22). A penalty method
      (lambda=1e5) does not pick that basin on fitness either, so we minimize the
      shortfall directly, which needs no lambda.
    - **Phase 2 (CEI)**: from the iteration in which the first feasible observation
      appears, switch to the usual constrained EI: three outcomes learned by separate
      GPs (energy = objective, purity and recovery = constraints), ModelListGP +
      qLogExpectedImprovement(constraints, objective). This trims energy inside the
      feasible region.
    - Bad observations (is_buildable failure, Aspen crash) are passed to the GP with
      energy clipped to twice the largest observed value -> they are learned correctly
      as an infeasible region and CEI avoids them automatically.
      Runtime bad points are re-evaluated on the same topology up to retry_bad times
      (this prevents a transient wedge from contaminating the GP with spurious
      infeasible points; if it is still bad after the retry, it is learned as genuine
      non-convergence).
    - Unbuildable binary combinations are excluded from fixed_features_list up front
      (so the budget is not diluted).
    - best_f is the best energy among feasible observations (both constraints met).
      **If the run ends with everything still infeasible, the min-shortfall
      observation is returned** (ties broken by the penalized fitness; 12.5(a). With
      bootstrap: off, the legacy penalty-minimum is used instead).
    - **Log scaling (12.5(b))**: positive continuous variables whose bounds ratio
      exceeds 50 are represented internally (GP/acqf/Sobol) in log space. This
      addresses the problem that a linear Normalize squashes narrow basins until the
      GP cannot see them. Values are converted back to the real scale only just
      before being passed to the evaluator and when the best point is returned.
      **Note**: with the Lee-consistent bounds of 12.3 (area ratio 15, p_perm ratio
      9.9, permeance ratio 12) no variable exceeds a ratio of 50, so this **never
      fires** (it is kept as insurance for future cases that use wider ranges, e.g.
      via bounds_override; the log_scale=off line in the startup log is normal).
    - **Rollback switches**: writing `bootstrap: off` / `retry_bad: 0` /
      `log_scale_inputs: off` under bo: in case.yaml restores the old behavior with
      no code change.

Pipeline:
    1. Sobol initial sample (n_init points)
    2. Fit 3 GPs (ModelListGP): energy/purity/recovery
    3. Maximize qLogEI(constraints=[purity>=purity_min, recovery>=recovery_min]) with
       optimize_acqf_mixed
    4. Take q_batch points -> group by identical binary key to amortize the Aspen build
    5. Repeat n_iter times

No change to case.yaml is required: if there is no bo: section, the defaults in this
file are used.
"""

from __future__ import annotations

import itertools
import os
import sys
import time
from collections import defaultdict
from typing import Any

import numpy as np
import torch
from botorch.acquisition.logei import qLogExpectedImprovement
from botorch.acquisition.objective import LinearMCObjective
from botorch.fit import fit_gpytorch_mll
from botorch.models import MixedSingleTaskGP, ModelListGP, SingleTaskGP
from botorch.models.transforms.input import Normalize
from botorch.models.transforms.outcome import Standardize
from botorch.optim import optimize_acqf_mixed
from gpytorch.mlls import ExactMarginalLogLikelihood, SumMarginalLogLikelihood
from torch.quasirandom import SobolEngine

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


# Defaults used when case.yaml has no bo: section (n_init ~= 2-3d as a guide, d=binary+continuous)
# bootstrap / retry_bad are stabilizations from the run22 lessons (2026-07-08). Writing
# `bootstrap: off` / `retry_bad: 0` under bo: in case.yaml restores the old behavior with no
# code change (rollback switch).
_BO_DEFAULTS: dict[str, Any] = {
    "n_init":  16,
    "n_iter":  40,
    "q_batch":  4,
    # Acquisition function for phase 1 (while there is no feasible observation):
    #   "shortfall": ignore energy and minimize only the constraint shortfall (default).
    #     Rationale: when every point is infeasible, the P(feasible) term of CEI flattens
    #     out and CEI degenerates into a plain energy minimizer, so it never reaches the
    #     feasible basin on the high-energy side (proven to exist by the run22 probe,
    #     E ~= 3x). A penalty method (lambda=1e5) does not pick that basin on fitness
    #     either (2192 vs 6226), so phase 1 minimizes the shortfall directly, with no lambda.
    #   "off": always CEI (the behavior up to run21).
    "bootstrap": "shortfall",
    # Number of retries within the same topology for runtime bad points (wedge/crash).
    # Prevents a transient wedge (15-20% in practice) from contaminating the GP as a
    # spurious infeasible point with purity=0/recovery=0. If it is still bad after the
    # retry, it is learned as genuine non-convergence.
    "retry_bad": 1,
    # Phase-aware patience (12.5(c)). Adaptively cuts off idle spinning once progress has
    # plateaued (~30 min per iteration). A naive consecutive-no-improvement count was
    # rejected (it would have discarded the 66%/50% improvement of run21 and the 34%
    # improvement of run22), so three measures prevent premature cutoff:
    #   (1) make the decision axis phase-specific (bootstrap=min_shortfall / CEI=feasible objective)
    #   (2) reset the counter on a phase switch (bootstrap->CEI)
    #   (3) never cut off before a floor of n_iter/3 iterations
    # 0 disables it (always runs to n_iter).
    "patience": 10,
    # Log scaling of continuous variables (12.5(b)). Positive continuous variables whose
    # bounds ratio exceeds _LOG_SCALE_RATIO are log-transformed in the internal
    # GP/acqf/Sobol representation. "off" pins the scale to linear.
    # Note: with the Lee-consistent bounds of 12.3 every variable has a ratio below 50, so
    # nothing is selected and this effectively never fires (kept as insurance for future
    # cases that use wider ranges, e.g. via bounds_override).
    "log_scale_inputs": "on",
}

# Bounds-ratio threshold for log transformation (12.5(b): "positive continuous variables
# whose bounds ratio exceeds 50")
_LOG_SCALE_RATIO = 50.0


class _PhasePatience:
    """State machine for phase-aware patience (12.5(c)); pure logic, testable.

    Call update(phase, axis, it) at the end of each iteration. axis is the
    phase-specific decision value, lower is better (bootstrap=min_shortfall /
    cei=minimum feasible objective). A return of True means stop. Rules:
      - on a phase switch, reset the counter and the reference value (never stop
        immediately after a switch)
      - reset the counter whenever axis improves (strictly decreases)
      - stop once there have been patience consecutive non-improvements and at least
        floor_iters iterations have completed
      - always False when patience <= 0 (disabled)
    """

    def __init__(self, patience: int, floor_iters: int) -> None:
        self.patience = int(patience)
        self.floor_iters = int(floor_iters)
        self._phase: str | None = None
        self._best: float = float("inf")
        self._count: int = 0

    def update(self, phase: str, axis: float, it: int) -> bool:
        if self.patience <= 0:
            return False
        if phase != self._phase:
            self._phase = phase
            self._best = float(axis)
            self._count = 0
            return False
        if axis < self._best - 1e-12:
            self._best = float(axis)
            self._count = 0
        else:
            self._count += 1
        return self._count >= self.patience and (it + 1) >= self.floor_iters


def _is_off(value: Any) -> bool:
    """Decide whether a flag value from case.yaml means "off".

    YAML 1.1 (PyYAML) parses bare `off`/`no`/`false` into the bool False, so a plain
    string comparison `value != "off"` would leave the rollback switch ineffective.
    This absorbs both the string and bool representations (valid values such as
    "shortfall" are not treated as off).
    """
    if isinstance(value, str):
        return value.strip().lower() in ("off", "false", "no", "0")
    return value is None or value is False or value == 0


def _log_scale_mask(n_bin: int, cont_vars: list[dict], enabled: bool) -> np.ndarray:
    """Return the mask of dimensions log-transformed in the internal representation
    (GP/acqf/Sobol); length d = n_bin + n_cont.

    Selected are "continuous variables with a positive lower bound and a bounds ratio >
    _LOG_SCALE_RATIO". Binary dimensions are always False.
    With enabled=False (log_scale_inputs: off) everything is False = linear scale (old behavior).
    """
    mask = np.zeros(n_bin + len(cont_vars), dtype=bool)
    if not enabled:
        return mask
    for j, cv in enumerate(cont_vars):
        lo, hi = float(cv["bounds"][0]), float(cv["bounds"][1])
        if lo > 0.0 and hi / lo > _LOG_SCALE_RATIO:
            mask[n_bin + j] = True
    return mask


def _to_eval_space(x_np: np.ndarray, log_mask: np.ndarray) -> np.ndarray:
    """Convert the internal representation (which includes log-space columns) to the
    real scale passed to the evaluator.

    x_np is (N, d). Only the columns flagged in log_mask are exponentiated (binary and
    linear columns are left as they are).
    """
    out = x_np.copy()
    if log_mask.any():
        out[:, log_mask] = np.exp(out[:, log_mask])
    return out


def _fitness(obj_value: float, purity: float, recovery: float,
             targets: dict, penalty_w: float) -> float:
    """Penalized fitness for logging and compatibility (same definition as GA._fitness).

    obj_value is the objective value (energy or cost; it follows the objective switch of 12.2).
    The constrained BO itself does not use this (the acqf handles constraints + objective).
    It is kept only for displaying gen_log / the best return value and for SST compatibility.
    """
    if obj_value >= BAD_VALUE:
        return BAD_VALUE
    penalty = (
        penalty_w * max(0.0, targets["purity_min"]   - purity)   ** 2 +
        penalty_w * max(0.0, targets["recovery_min"] - recovery) ** 2
    )
    return obj_value + penalty


def _evaluate_batch_multi(
    x_np: np.ndarray,
    ss: dict,
    bin_vars: list[dict],
    cont_vars: list[dict],
    n_bin: int,
    evaluator: Evaluator,
    retry_bad: int = 1,
    objective_fn=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """Group by binary key -> evaluate each group in a single build. Returns the objective plus 3 outcomes.

    Same folding as the GA's `_evaluate_population` (points sharing a binary key are
    simulated q at a time from one Aspen build). The continuous x is narrowed by
    `x_for_topology` from the full template dimensions down to the variables of the
    concrete topology (after pruning) before being passed on, so that it lines up with
    the positional zip on the evaluator side.

    When retry_bad > 0, only the x that came back bad although they were buildable are
    re-evaluated on the same topology (up to retry_bad times). This prevents spurious
    purity=0/recovery=0 observations caused by a transient COM wedge (15-20% in
    practice) from contaminating the GP and skewing the search. Points that are still
    bad after the retry are kept as genuine non-convergence (learning them as
    infeasible is the correct behavior).
    Groups rejected by is_buildable are deterministically unbuildable structures, so they
    are not retried.

    objective_fn (12.2): passing `(metrics, x_cont_template, topology) -> float` makes
    objective_arr take that value (cost objective). With None, objective = energy.

    Returns
    -------
    objective_arr: (N,) objective value of the optimization (energy or cost; BAD_VALUE if bad)
    energy_arr   : (N,) specific_energy_kWh_tCO2 (BAD_VALUE if bad)
    purity_arr   : (N,) purity [0,1] (0.0 if bad)
    recovery_arr : (N,) recovery [0,1] (0.0 if bad)
    valid_mask   : (N,) bool (True = not bad = valid observation)
    n_evals      : actual number of Aspen evaluations (including retries; >= N)
    """
    N = x_np.shape[0]
    groups: dict[tuple, list[tuple[int, list[float]]]] = defaultdict(list)
    for i in range(N):
        key = tuple(int(round(float(x_np[i, k]))) for k in range(n_bin))
        x_cont = [float(v) for v in x_np[i, n_bin:]]
        groups[key].append((i, x_cont))

    objectives = np.empty(N, dtype=np.float64)
    energies   = np.empty(N, dtype=np.float64)
    purities   = np.empty(N, dtype=np.float64)
    recoveries = np.empty(N, dtype=np.float64)
    valids     = np.empty(N, dtype=bool)
    n_evals = N

    for binary_key, items in groups.items():
        q_active = {bv["name"]: binary_key[i] for i, bv in enumerate(bin_vars)}
        topology = active_topology(ss, q_active)
        reason = is_buildable(topology)
        if reason is not None:
            metrics_list = [Metrics.bad() for _ in items]
        else:
            x_list = [x_for_topology(x_cont, cont_vars, topology) for _, x_cont in items]
            metrics_list = evaluator.evaluate_topology(topology, x_list)
            # Transient-wedge countermeasure: re-evaluate only the bad points (genuine
            # non-convergence stays bad after the retry)
            for _ in range(max(0, retry_bad)):
                bad_pos = [
                    k for k, m in enumerate(metrics_list)
                    if m.specific_energy >= BAD_VALUE
                ]
                if not bad_pos:
                    break
                retry_metrics = evaluator.evaluate_topology(
                    topology, [x_list[k] for k in bad_pos]
                )
                n_evals += len(bad_pos)
                recovered = 0
                for k, m in zip(bad_pos, retry_metrics):
                    if m.specific_energy < BAD_VALUE:
                        recovered += 1
                    metrics_list[k] = m
                if recovered:
                    print(f"  [CBO] retry recovered {recovered}/{len(bad_pos)} bad eval(s) "
                          f"(transient wedge)")
        for (i, x_cont), m in zip(items, metrics_list):
            is_bad = m.specific_energy >= BAD_VALUE
            energies[i]   = BAD_VALUE if is_bad else float(m.specific_energy)
            purities[i]   = 0.0       if is_bad else float(m.purity)
            recoveries[i] = 0.0       if is_bad else float(m.recovery)
            valids[i]     = not is_bad
            if is_bad:
                objectives[i] = BAD_VALUE
            elif objective_fn is None:
                objectives[i] = float(m.specific_energy)
            else:
                objectives[i] = float(objective_fn(m, x_cont, topology))

    return objectives, energies, purities, recoveries, valids, n_evals


def _select_device() -> torch.device:
    """cuda:0 if a GPU is available, otherwise CPU (fallback for testing)."""
    return torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")


def _sobol_initial(
    n_init: int,
    bounds: torch.Tensor,
    n_bin: int,
    seed: int,
) -> torch.Tensor:
    """Generate n_init Sobol points and round only the binary part to {0,1}."""
    d = bounds.shape[1]
    sobol = SobolEngine(dimension=d, scramble=True, seed=seed)
    raw = sobol.draw(n_init).to(dtype=bounds.dtype, device=bounds.device)  # in [0,1]^d
    x = bounds[0] + (bounds[1] - bounds[0]) * raw
    if n_bin > 0:
        x[:, :n_bin] = x[:, :n_bin].round().clamp_(0.0, 1.0)
    return x


def _build_fixed_features(ss: dict, bin_vars: list[dict]) -> list[dict[int, float]]:
    """Return the binary combinations in fixed_features_list form (unbuildable combinations excluded).

    Structurally unbuildable combinations, such as both sides of a toggle pair being ON,
    are known deterministically, so instead of putting them through the wasteful
    "propose -> BAD -> learn to avoid" loop they are removed from the acquisition
    function's search space from the start (run22 lesson: invalid combinations among the
    16 topologies diluted a budget of 264 evaluations).
    Only in the pathological case where every combination is unbuildable do we fall back
    to the full enumeration as a safeguard.
    """
    n_bin = len(bin_vars)
    if n_bin == 0:
        return [{}]
    all_combos = list(itertools.product([0, 1], repeat=n_bin))
    buildable = []
    for combo in all_combos:
        q_active = {bv["name"]: v for bv, v in zip(bin_vars, combo)}
        if is_buildable(active_topology(ss, q_active)) is None:
            buildable.append({i: float(b) for i, b in enumerate(combo)})
    if not buildable:  # safeguard (only for a pathological SS template)
        return [{i: float(b) for i, b in enumerate(c)} for c in all_combos]
    return buildable


def _make_gp(
    train_x: torch.Tensor,
    train_y_single: torch.Tensor,
    n_bin: int,
    cat_dims: list[int],
    bounds: torch.Tensor,
    d: int,
):
    """Build the GP for one outcome: MixedSingleTaskGP if there are binaries, otherwise SingleTaskGP.

    train_y_single has shape (N, 1) (the BoTorch outcome dim made explicit).
    """
    outcome_transform = Standardize(m=1)
    input_transform   = Normalize(d=d, bounds=bounds)
    if n_bin > 0:
        return MixedSingleTaskGP(
            train_X=train_x,
            train_Y=train_y_single,
            cat_dims=cat_dims,
            input_transform=input_transform,
            outcome_transform=outcome_transform,
        )
    return SingleTaskGP(
        train_X=train_x,
        train_Y=train_y_single,
        input_transform=input_transform,
        outcome_transform=outcome_transform,
    )


def _clip_bad_energy(
    energy_arr: np.ndarray,
    valid_mask: np.ndarray,
) -> np.ndarray:
    """Clip bad energies to "twice the largest valid observation".

    This does not break the GP fit and is learned correctly as infeasible (CEI avoids it
    automatically).
    If everything is invalid, BAD_VALUE is used as it is (rare; all initial Sobol points failed).
    """
    out = energy_arr.copy()
    if valid_mask.any():
        cap = float(energy_arr[valid_mask].max() * 2.0 + 1.0)
        out[~valid_mask] = cap
    return out


def run_bo(
    ss: dict,
    case: dict,
    evaluator: Evaluator,
    seed: int = 1,
) -> tuple[Any, list[dict], int]:
    """Optimize the continuous and binary variables of ss with BoTorch constrained BO.

    Same signature as ga.run_ga. run_iteration.py selects between them with the optimizer flag.

    Objective: minimize energy (specific_energy_kWh_tCO2).
               When optimization_targets.objective in case.yaml is "minimize_cost",
               minimize the annualized capture cost [$/tCO2] (evaluator.cost_per_tco2, 12.2)
    Constraints: purity >= purity_min, recovery >= recovery_min (from case.yaml)

    Returns
    -------
    best     : the best x (a plain list, compatible with a DEAP Individual). If there are
               feasible observations, the one among them with the lowest energy. If
               everything is infeasible, the min-shortfall observation (ties broken by the
               penalized fitness). Only with bootstrap: off is the legacy minimum of the
               penalized fitness used
    gen_log  : [{"gen": i, "best_fitness": f, "phase": ...}, ...] (length <= n_iter; shorter
               when patience cuts the run off, in which case the last element carries an
               "early_stop" key)
               best_fitness is the penalized value, for SST signals compatibility
    n_evals  : total number of evaluations
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss, case.get("membrane_model"))
    n_bin     = len(bin_vars)
    n_cont    = len(cont_vars)
    d         = n_bin + n_cont

    targets      = case["optimization_targets"]
    purity_min   = float(targets["purity_min"])
    recovery_min = float(targets["recovery_min"])

    # ---- Objective switch (12.2): energy (legacy) / cost ($/tCO2) ----
    # In cost mode the objective outcome of phase 2 CEI, the best selection and the logging
    # fitness all move to the cost axis. Phase 1 (bootstrap = minimizing the constraint
    # shortfall) is unchanged regardless of the objective.
    cost_mode = str(targets.get("objective", "")).strip() == "minimize_cost"
    if cost_mode:
        econ = {**ECONOMICS_DEFAULTS, **(case.get("economics") or {})}
        penalty_w = float(econ["penalty_weight"])   # lambda on the cost (<100 $/tCO2) scale (Lee's r)

        def objective_fn(m: Metrics, x_cont: list[float], topology: dict) -> float:
            areas = membrane_areas_from_x(x_cont, cont_vars, topology)
            return cost_per_tco2(m, areas, case)
    else:
        penalty_w = float(case.get("penalty_weight", 1e5))  # for the logging fitness only
        objective_fn = None

    bo_cfg       = {**_BO_DEFAULTS, **case.get("bo", {})}
    n_init       = int(bo_cfg["n_init"])
    n_iter       = int(bo_cfg["n_iter"])
    q_batch      = int(bo_cfg["q_batch"])
    # "shortfall" | "off". YAML 1.1 parses `off` into the bool False, so _is_off absorbs it
    bootstrap_on = not _is_off(bo_cfg.get("bootstrap", "shortfall"))
    retry_bad    = int(bo_cfg.get("retry_bad", 1))
    log_scale_on = not _is_off(bo_cfg.get("log_scale_inputs", "on"))
    patience     = int(bo_cfg.get("patience", 10))
    # Floor: never cut off before n_iter/3 iterations (12.5(c); prevents an early cutoff
    # during an initial plateau)
    tracker = _PhasePatience(patience, floor_iters=max(1, n_iter // 3))

    device = _select_device()
    dtype  = torch.double

    # Log scaling (12.5(b)): for the selected dimensions the internal representation
    # (bounds/Sobol/GP/acqf) is kept uniformly in log space, and _to_eval_space converts
    # back to the real scale only just before passing to the evaluator and when returning best.
    log_mask = _log_scale_mask(n_bin, cont_vars, log_scale_on)

    # bounds: shape (2, d). The binary part is [0,1]; continuous is cv["bounds"] (log space for
    # the log-scaled ones).
    bounds_list = [[0.0, 1.0]] * n_bin + [list(cv["bounds"]) for cv in cont_vars]
    for k in np.flatnonzero(log_mask):
        bounds_list[k] = [float(np.log(bounds_list[k][0])), float(np.log(bounds_list[k][1]))]
    bounds = torch.tensor(bounds_list, dtype=dtype, device=device).T  # (2, d)

    # Reproducibility: fix all torch-side randomness with seed (Sobol is seeded separately on the engine)
    torch.manual_seed(seed)

    cat_dims = list(range(n_bin))
    fixed_features_list = _build_fixed_features(ss, bin_vars)

    log_scaled_names = [cont_vars[k - n_bin]["name"] for k in np.flatnonzero(log_mask)]
    print(f"[CBO] device={device}, d={d} (binary={n_bin}, cont={n_cont}), "
          f"n_init={n_init}, n_iter={n_iter}, q_batch={q_batch}, "
          f"fixed_features={len(fixed_features_list)}/{2**n_bin} (unbuildable excluded), "
          f"bootstrap={'shortfall' if bootstrap_on else 'off'}, retry_bad={retry_bad}, "
          f"log_scale={log_scaled_names if log_scaled_names else 'off'}, "
          f"objective={'cost($/tCO2)' if cost_mode else 'energy(kWh/tCO2)'}, "
          f"patience={patience if patience > 0 else 'off'}"
          f"(floor={max(1, n_iter // 3)}), "
          f"constraints: purity>={purity_min}, recovery>={recovery_min}")

    t0 = time.monotonic()   # for the computation-time breakdown (gen_log "t" = seconds since the optimization started)

    # ----- 1. Sobol initial sample -----
    # train_x_np is the internal representation (log-scaled columns are in log space, i.e.
    # log-uniform sampling)
    train_x_np = _sobol_initial(n_init, bounds, n_bin, seed).detach().cpu().numpy()
    _t_init0 = time.monotonic()
    o_arr, e_arr, p_arr, r_arr, v_mask, n_evals = _evaluate_batch_multi(
        _to_eval_space(train_x_np, log_mask), ss, bin_vars, cont_vars, n_bin, evaluator,
        retry_bad=retry_bad, objective_fn=objective_fn
    )
    init_eval_sec = round(time.monotonic() - _t_init0, 2)   # wall time of the initial-sample evaluation

    # Move all observations to tensors (the GP gets the clipped objective; the logging fitness uses raw)
    all_x = torch.tensor(train_x_np, dtype=dtype, device=device)
    all_o_raw = o_arr.copy()   # objective value (energy or cost)
    all_p_raw = p_arr.copy()
    all_r_raw = r_arr.copy()
    all_v = v_mask.copy()

    # ----- 2-4. BO loop -----
    gen_log: list[dict] = []
    objective = LinearMCObjective(weights=torch.tensor([1.0, 0.0, 0.0], dtype=dtype, device=device))

    for it in range(n_iter):
        o_capped = _clip_bad_energy(all_o_raw, all_v)   # objective value (same clipping for energy/cost)
        feasible_mask = (all_p_raw >= purity_min) & (all_r_raw >= recovery_min) & all_v

        # ---- Two-phase switch (run22 lesson, 2026-07-08) ----
        # While there is no feasible observation, CEI flattens out at P(feasible) ~= 0 and
        # degenerates into a plain energy minimizer, so it never goes to the feasible basin
        # on the high-energy side. Phase 1 ignores energy entirely and minimizes only the
        # constraint shortfall, then switches to CEI from the iteration in which the first
        # feasible point appears.
        # bootstrap="off" reverts to always-CEI (the behavior up to run21).
        use_bootstrap = bootstrap_on and (not bool(feasible_mask.any()))
        phase = "bootstrap" if use_bootstrap else "cei"

        # GP fit -> acqf optimization. On failure (e.g. all initial Sobol points bad so an
        # outcome has zero variance, GP numerical instability, an internal error in the acqf
        # optimization) do not kill the loop: fall back to Sobol exploration to add
        # observations (once valid observations come in, the next cycle returns to the GP).
        _t_model0 = time.monotonic()   # breakdown timing: GP fit / acqf optimization / Aspen evaluation
        t_fit_sec = 0.0
        try:
            if use_bootstrap:
                # ---- Phase 1: minimize the constraint shortfall (no lambda needed, energy ignored) ----
                # Bad observations have purity=0/recovery=0, so their shortfall is maximal and
                # they are naturally learned as "points not worth approaching" (what remains
                # after the retries is genuine non-convergence).
                shortfall = (
                    np.maximum(0.0, purity_min   - all_p_raw)
                    + np.maximum(0.0, recovery_min - all_r_raw)
                )
                train_s = torch.tensor(-shortfall, dtype=dtype, device=device).unsqueeze(-1)
                gp_s = _make_gp(all_x, train_s, n_bin, cat_dims, bounds, d)
                mll = ExactMarginalLogLikelihood(gp_s.likelihood, gp_s)
                fit_gpytorch_mll(mll)
                best_f_tensor = torch.tensor(
                    float(-shortfall.min()), dtype=dtype, device=device
                )
                acqf = qLogExpectedImprovement(model=gp_s, best_f=best_f_tensor)
            else:
                # ---- Phase 2: constrained EI (trim the objective inside the feasible region) ----
                # GP fit (the objective is minimized, so the sign is flipped to maximize; bad
                # points are capped and learned as infeasible)
                train_o = torch.tensor(-o_capped, dtype=dtype, device=device).unsqueeze(-1)
                train_p = torch.tensor(all_p_raw,  dtype=dtype, device=device).unsqueeze(-1)
                train_r = torch.tensor(all_r_raw,  dtype=dtype, device=device).unsqueeze(-1)

                gp_o = _make_gp(all_x, train_o, n_bin, cat_dims, bounds, d)
                gp_p = _make_gp(all_x, train_p, n_bin, cat_dims, bounds, d)
                gp_r = _make_gp(all_x, train_r, n_bin, cat_dims, bounds, d)
                model = ModelListGP(gp_o, gp_p, gp_r)
                mll = SumMarginalLogLikelihood(model.likelihood, model)
                fit_gpytorch_mll(mll)

                # best_f: the largest objective (negated value) among feasible observations
                # = the smallest objective.
                # (The legacy "below the worst" path is taken only when everything is
                # infeasible with bootstrap="off".)
                if feasible_mask.any():
                    best_f = float(-o_capped[feasible_mask].min())  # = max(-objective)
                else:
                    best_f = float(-o_capped.max() - 1.0)  # below the worst = treated as leaving room to improve
                best_f_tensor = torch.tensor(best_f, dtype=dtype, device=device)

                # acqf: constrained EI (the convention is that a constraint is feasible when <=0)
                constraints = [
                    lambda Z: purity_min   - Z[..., 1],  # purity   >= purity_min   <=> purity_min - purity <= 0
                    lambda Z: recovery_min - Z[..., 2],  # recovery >= recovery_min <=> recovery_min - recovery <= 0
                ]
                acqf = qLogExpectedImprovement(
                    model=model,
                    best_f=best_f_tensor,
                    objective=objective,
                    constraints=constraints,
                )

            t_fit_sec = round(time.monotonic() - _t_model0, 2)
            # Categorical enumeration x continuous L-BFGS (common to both phases)
            candidates, _ = optimize_acqf_mixed(
                acq_function=acqf,
                bounds=bounds,
                q=q_batch,
                num_restarts=10,
                raw_samples=256,
                fixed_features_list=fixed_features_list,
            )
        except Exception as e:
            print(f"  [CBO] Iter {it+1}: GP/acqf failed ({type(e).__name__}: {e}) "
                  f"-> falling back to Sobol, exploring {q_batch} points")
            if t_fit_sec == 0.0:   # if the failure happened during the fit, attribute the elapsed time to fit
                t_fit_sec = round(time.monotonic() - _t_model0, 2)
            # Vary the seed per iteration to avoid duplicate samples (reproducibility is preserved from the base seed)
            candidates = _sobol_initial(q_batch, bounds, n_bin, seed=seed * 10007 + it + 1)
        t_acq_sec = round(time.monotonic() - _t_model0 - t_fit_sec, 2)

        # Evaluation (candidates are in the internal representation, so convert back to the real scale first)
        c_np = candidates.detach().cpu().numpy()
        _t_eval0 = time.monotonic()
        new_o, new_e, new_p, new_r, new_v, n_new = _evaluate_batch_multi(
            _to_eval_space(c_np, log_mask), ss, bin_vars, cont_vars, n_bin, evaluator,
            retry_bad=retry_bad, objective_fn=objective_fn
        )
        t_eval_sec = round(time.monotonic() - _t_eval0, 2)

        # Accumulate the observations
        all_x     = torch.cat([all_x, candidates], dim=0)
        all_o_raw = np.concatenate([all_o_raw, new_o])
        all_p_raw = np.concatenate([all_p_raw, new_p])
        all_r_raw = np.concatenate([all_r_raw, new_r])
        all_v     = np.concatenate([all_v,     new_v])
        n_evals  += n_new

        # Logging: the best penalized fitness over all observations (for compatibility; objective-based)
        all_fitness = np.array([
            _fitness(o, p, r, targets, penalty_w)
            for o, p, r in zip(all_o_raw, all_p_raw, all_r_raw)
        ])
        best_fit_log = float(all_fitness.min())
        # phase is diagnostic information for the SST agent and for analysis
        # (bootstrap = still searching for constraints / cei = inside the feasible region).
        # "t" is the seconds elapsed since the optimization started (for best-so-far vs time
        # convergence curves and for aggregating the time breakdown).
        # t_fit/t_acq/t_eval are this iteration's breakdown in seconds (GP training /
        # acquisition-function optimization / Aspen evaluation).
        entry = {"gen": it + 1, "best_fitness": best_fit_log, "phase": phase,
                 "t": round(time.monotonic() - t0, 1),
                 "t_fit": t_fit_sec, "t_acq": t_acq_sec, "t_eval": t_eval_sec}
        if it == 0:
            entry["t_init_eval"] = init_eval_sec   # wall time of the Sobol initial-sample evaluation
        gen_log.append(entry)

        # Progress display: both the best fitness and the feasible best objective (if any)
        if feasible_mask.any():
            fo_min = float(o_capped[feasible_mask].min())
            print(f"  Iter {it+1:2d} [{phase}]: best_fit={best_fit_log:.1f}  feasible_obj_min={fo_min:.1f}")
        else:
            min_short = float((
                np.maximum(0.0, purity_min   - all_p_raw)
                + np.maximum(0.0, recovery_min - all_r_raw)
            ).min())
            print(f"  Iter {it+1:2d} [{phase}]: best_fit={best_fit_log:.1f}  "
                  f"(no feasible yet, min_shortfall={min_short:.3f})")

        # ---- Phase-aware patience (12.5(c)) ----
        # Determine the phase and the decision axis from the state after the new observations
        # have been taken in (bootstrap=min_shortfall / cei=minimum feasible objective; reset
        # on a phase switch).
        feas_now = (all_p_raw >= purity_min) & (all_r_raw >= recovery_min) & all_v
        if feas_now.any():
            phase_now = "cei"
            axis = float(np.where(feas_now, all_o_raw, np.inf).min())
        else:
            phase_now = "bootstrap"
            axis = float((
                np.maximum(0.0, purity_min   - all_p_raw)
                + np.maximum(0.0, recovery_min - all_r_raw)
            ).min())
        if tracker.update(phase_now, axis, it):
            gen_log[-1]["early_stop"] = f"patience={patience} ({phase_now})"
            print(f"  [CBO] early stop at iter {it+1}/{n_iter}: "
                  f"no improvement on the {phase_now} axis for {patience} consecutive iterations "
                  f"(the floor of {max(1, n_iter // 3)} iterations has been served)")
            break

    # ----- 5. Pick the best individual and return it as a DEAP-Individual-compatible list -----
    # CBO is designed to prioritize constraint satisfaction, so if there are feasible
    # observations it returns the one with the lowest objective (energy/cost) among them
    # (picking a good infeasible point would contradict the SST decision axis).
    # When everything is infeasible, the returned axis is aligned with the search axis of the
    # bootstrap phase and the min-shortfall observation is returned (12.5(a); in run23
    # iter_004 a point with shortfall 0.014 that the search had hit was buried by the legacy
    # penalty-min selection and 0.063 was recorded instead). Ties are broken by the penalized
    # fitness (lexsort is a stable sort, so it is deterministic for a given seed).
    # Only with bootstrap: off is the legacy penalty-min fallback used.
    final_feasible_mask = (all_p_raw >= purity_min) & (all_r_raw >= recovery_min) & all_v
    if final_feasible_mask.any():
        objective_for_select = np.where(final_feasible_mask, all_o_raw, np.inf)
        best_idx = int(np.argmin(objective_for_select))
    else:
        all_fitness = np.array([
            _fitness(o, p, r, targets, penalty_w)
            for o, p, r in zip(all_o_raw, all_p_raw, all_r_raw)
        ])
        if bootstrap_on:
            # Bad observations have purity=0/recovery=0 and thus fall to the maximum shortfall;
            # even when they tie with a valid point, the tie-break on fitness (BAD_VALUE for
            # bad) lets the valid point win
            shortfall = (
                np.maximum(0.0, purity_min   - all_p_raw)
                + np.maximum(0.0, recovery_min - all_r_raw)
            )
            best_idx = int(np.lexsort((all_fitness, shortfall))[0])
        else:
            best_idx = int(np.argmin(all_fitness))
    # all_x is the internal representation (including log space), so convert back to the real scale before returning
    best_x = _to_eval_space(all_x[best_idx].detach().cpu().numpy().reshape(1, -1), log_mask)[0]
    best_individual: list[float] = [float(v) for v in best_x]

    return best_individual, gen_log, n_evals
