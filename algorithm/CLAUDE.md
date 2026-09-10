# CLAUDE.md (algorithm / SST agent)

This file is the instruction sheet for **SST agent mode**.
Here you (Claude Code) are not a developer but **the agent that drives the outer loop of superstructure transition (SST)**.
You read the optimisation results, identify the problem, propose a new SS, and advance the loop autonomously.

> This sheet is self-contained: everything you need for a proposal is here. The semantics of each structural operation are also documented in the docstrings of `src/apply_ss.py`, which you may read (it is source code, not run data).

---

## Your role

Analyse the optimisation results of a fixed superstructure (`results.json`) and improve the performance **by rearranging the structure itself**.
Optimising the continuous variables is the job of the inner loop (the inner optimizer — constrained Bayesian optimisation, CBO — plus Aspen). You are responsible for **the structural decisions only**.

Express a proposal **exclusively** with the **structural operations** `add_unit` / `add_gated_unit` / `delete_unit` / `add_arc` / `delete_arc` / `promote_candidate` (unit-level; `apply_ss.py` expands them to arcs), and write it to `ss_change.json`. **`set_bounds` is kept out of the autonomous agent's hands** — the bounds are a fixed setting given by a human, and the agent does not widen them. (`op_set_bounds` itself is retained in `apply_ss` for **manual/development** use, but the autonomous loop does not use it.)

**When you add a new stage (unit), use `add_gated_unit`, not `add_unit`.** This is the core of the method: **the adoption of a structure is delegated to the inner optimizer**. `add_unit` is a fixed addition whose engagement is always ON, which would mean the SST decides the number of stages itself — a departure from the policy. `add_gated_unit` turns the feed into a bypass candidate toggle, so that when the feed is OFF the dead-unit pruning of `active_topology` removes the membrane and yields a "clean sub-structure without the stage" — i.e. the inner optimizer evaluates "with stage" against "without stage" fairly. (`add_unit` is retained for development and manual use, when you want to fix a stage that is known to be reliably effective.)

---

## The procedure for one iteration

The user only specifies the starting point of the run (e.g. "run runs/<name>"). From then on, **keep the loop going without further instructions**.
**The session is opened in the `algorithm/` directory, and every command is run from there** (the scripts are at `src/...`).
**Run data lives in `algorithm/runs/` (already gitignored)**. Start a new run by copying `ss_seed.json` to `runs/<name>/ss_current.json`.

```
1. Treat the highest-numbered `runs/<name>/iterations/iter_MMM/` that has a `results.json` as the latest and read its `results.json` (the authoritative record; consult the log only on failure). Call this number **M**. Skip directories without a `results.json`, such as an empty `iter_001`. The numbers need not be consecutive.
2. Extract the signals (see below)
3. Write `runs/<name>/iterations/iter_MMM/ss_change.json` as a "deletion + addition set"
4. `uv run python src/apply_ss.py --base-dir runs/<name> --iter M`
5. `uv run python -u src/run_iteration.py --base-dir runs/<name> > runs/<name>/iter_(M+1)_log.txt 2>&1`
   (run_iteration assigns the next number automatically as max+1 of the existing iters, normally M+1.)
6. Check the auto-commit at the end of `run_iteration.py` and go back to 1
```

**For validation / dry runs, add "the reduced-budget flags for the current optimizer" plus `--no-commit` to step 5** (running at small scale without dirtying `case.yaml` or dragging in a commit; `/sst-loop` expands this from `--dry`). **Always check `optimizer` in `case.yaml` and choose accordingly**: for `bo` use `--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2`, and for `ga` (or when unspecified) use `--pop 4 --gen 3`. The `--pop/--gen` flags belong to the GA path and are not read on the BO path (if you pass the wrong ones, a full-scale run starts when you meant to validate; run_iteration emits a warning). For a production run, pass nothing and use the settings in `case.yaml` with auto-commit. **Bound a validation by an iteration limit** — at small scale the signal is noise, so do not rely on the fitness-based stopping decision.

**Do not ask the user for confirmation.** Proceed on your own judgement even with structurally large changes (deleting a unit, adding a new stage, adding a recycle).
**"Moving boldly" is the whole point of this method**; do not let caution stall you.

---

## Signals -> structural proposals

Read the following signals from `results.json` and use them as the grounds for a structural change. **Quote the concrete numbers that supported the decision in `reason`**
(e.g. `"MEMB2_p_perm=0.1 is pinned at its lower bound -> add a permeate-side COMP to widen the driving-force range"`).

| Signal | Interpretation | Structural proposal |
|---|---|---|
| A continuous variable is pinned at its (fixed) upper/lower bound | That limit is binding (the bounds are a fixed setting, so do not widen them) | Work around it structurally (e.g. add a compression stage, put membranes in parallel) or accept it as a limit |
| A single unit dominates the energy (energy_breakdown) | The load is concentrated | Recycle / stage splitting / heat integration |
| The retentate (going to residue) is rich in the valuable component (CO2) | It is not being recovered | Turn that retentate into a residue-vs-recycle toggle pair (see below) |
| A candidate is always OFF (active_candidates=0) | That structure is unnecessary | Delete the candidate arc with `delete_arc`, or leave it |
| A candidate is always ON | It is permanently effective | Promote it to fixed with `promote_candidate` |
| Purity/recovery fall short of the target | Constraint violation | For higher purity add a stage (**as a toggle with `add_gated_unit`; see below**); for higher recovery add a recycle (as a toggle pair; see below) |

**Add a stage with `add_gated_unit`, as a "toggled stage" (never a fixed addition).** It is done in a single op: the membrane feed becomes a bypass candidate toggle, and the pruning in `active_topology` removes the stage when the feed is OFF — so the inner optimizer chooses between "with stage" and "without stage". For example, to add a third stage on the flow where the MEMB2 permeate V5 goes to the product V7:
`{"op":"add_gated_unit","unit_type":"MEMB","unit":"MEMB3","feed_from":"V5","bypass_to":"V7","permeate_to":"V7","retentate_to":"V8","params":{...}}`.
**Caution**: `feed_from` must be the source of **a single flow that you intercept** (if another fixed outgoing arc remains, the out-degree exceeds 1 when the candidate is ON and `is_buildable` rejects it). **It consumes two binaries**, so check `max_binary_variables` (8) for yourself and remove candidates that have served their purpose before adding.

**Before adding a recycle candidate, check the CO2 flow of the retentate you intend to send back, using `stream_results`.** Sending back a retentate with almost no CO2 will not move the recovery (it is pointless), so choose a retentate with a large CO2 flow.

**Pressure and membrane-property rules (blower campaign):**
- **Do not write `permeance_CO2` / `permeance_N2` into the `params` of a membrane.** The membrane properties are
  optimisation variables (`{unit}_perm`, with the selectivity derived automatically from the Robeson upper bound)
  and are overwritten at every evaluation.
  Specifying them directly in a proposal is meaningless, and proposals that assume a "fictitious high-performance
  membrane" are forbidden.
  All you need to write into the params of a membrane are the initial values of `area` and `p_permeate`.
- **Unify every membrane inlet at 1.1 bar (blower).** This campaign is the scenario in which "the whole flue gas is
  not compressed (blower + vacuum driven)", so **COMP is fixed at 1.1 bar per type with no optimisation variable**
  (UNIT_BOUNDS has lo==hi. If you omit `outlet_pressure` in params, 1.1 is filled in automatically; writing
  anything other than 1.1 makes apply_ss reject it). **When you add a new membrane stage, insert a blower into
  the feed flow in the same shape as COMP1/COMP2 of the seed**: pre-mixer -> COMP (params may be omitted) ->
  membrane inlet. For a gated membrane, insert it between the destination of the feed candidate arc (the gated
  inlet) and the membrane.
- **Return a recycle to a pre-mixer (the blower inlet; V9/V11 in the seed).** Since every stream is aligned at
  1.0-1.1 bar, returning any retentate or permeate to any pre-mixer causes no pressure inconsistency (no loss of
  compression from the Mixer following the minimum pressure). No re-compression COMP is needed.
- **The expander `EXP` is effectively useless in this campaign** (it has at most a 1.1->1.0 bar pressure drop, so
  the recovery is nearly zero). Do not propose it.
- **Coolers are inserted automatically (no proposal needed).** The builder always attaches a 35 degC cooler to the
  outlet of a COMP and of an auto-VP, so you do not need to add a `HEAT` unit yourself (doing so would only add a
  useless duplicate with zero variables. `HEAT` is retained for development and manual use).

**Express a recycle as a "toggle pair with the residue" (do not add branches additively).** When you divert a stream from a sink (residue etc.) to a recycle, do not merely add the recycle candidate; also **turn the existing fixed sink arc into a candidate so that the two are mutually exclusive**. The procedure is: `delete_arc` (remove the fixed sink arc) -> `add_arc`(candidate) (return the sink as a candidate) -> `add_arc`(candidate) (add the recycle).
**Reason**: if you add a recycle while leaving the residue fixed, the source vertex has out-degree 2 when the recycle is ON, and `is_buildable` rejects it as BAD, so the iteration spins uselessly. **Except for membrane inlets, every vertex has out-degree <= 1 in a concrete topology** (the builder has no splitter, and is_buildable drops violations). This is why a branch is expressed as a "toggle pair" rather than an "addition".

Do deletions and additions as a set. Remove converged variables, units that are not doing anything (WNET~=0), and candidates that are pinned at a limit (i.e. already explored), and add unexplored structural options, new stages and recycles.

**Do not keep growing the binary candidates (the most important operating principle).** The SST is an operation that moves the variable space, not one that expands it.
The limit is defined by **the number of binary variables (candidate arcs) used for structural decisions only**. Only the binaries cause a combinatorial explosion of 2^n; the continuous variables merely grow and shrink along with the units, so there is no cap on the total.
At every proposal, check for yourself that "the binary candidates do not exceed `case.yaml.max_binary_variables` (8)" and that "they are not growing monotonically across iterations".
To add a new candidate, first remove one that has served its purpose (always OFF, or pinned = already explored).
Do not repeat proposals that only add. At the same time, **do not cut so much that the search degenerates** — do not go below `case.yaml.min_variables` (4) in total. The transitions should continue, so keep "moving" things with deletions and additions.

---

## Rollback decision (so you do not stall)

Do not test buildability in advance. Judge from the execution results.

- If the best solution of the inner optimizer lands **entirely in the penalty region** (not a single concrete topology satisfies the constraints; purity and recovery are extremely low), the previous proposal has probably broken the structure. **Revert to the previous SS, or make a different proposal**.
- Do the same if a build failure or a convergence failure of Aspen has spread across the whole of `results.json`.
- To revert, either write the inverse operations in `ss_change.json`, or restore `ss_before_change.json` (which apply_ss saves) to `ss_current.json` and propose again.
- **When you apply_ss to the same iter a second time (e.g. proposing again after a revert), pass `--force`.** By default apply_ss refuses to re-apply to an iter that already has the saved file `ss_before_change.json` (to prevent the accident of a retry overwriting and losing the rollback point). Use `--force` only knowing that it overwrites the saved copy.

---

## Stopping conditions

Stop the loop on any of the following, and on nothing else.

- An explicit stop instruction from the user
- Judge improvement by **`performance` in `results.json` (the measured values of the best that was returned)**:
  - **While the constraints are unmet**: an improvement means the total shortfall is shrinking (`max(0, purity_min-purity) + max(0, recovery_min-recovery)`; the SHORT display of signals).
  - **Once both constraints are met**: an improvement means **the capture cost (`cost_usd_per_tCO2`) is going down**
    (the objective is `minimize_cost`. `specific_energy_kWh_tCO2` is reported alongside as a reference value,
    but a transition where only the energy goes down while the cost goes up is a regression).
  - On either criterion, treat it as converged **when, and only when, 3 or more consecutive iterations have brought no improvement over the best value attained before them**.
  - **This count is the sole convergence criterion. Do not stop earlier** on the grounds that no structural hypothesis remains, that the residual variation looks like optimizer noise, or that the structure "has converged". If you judge that no structural change is warranted, run the current SS again unchanged (skip steps 2-4; just run `run_iteration.py` on the current `ss_current.json`) and let the count accumulate. A rollback re-run counts as an iteration like any other.
  - **Do not use `gen_log[-1].best_fitness` for this decision**: under CBO the best is chosen feasibility-first, whereas `best_fitness` remains "the penalty-inclusive minimum over all observations" — a different axis. A solution that breaks a constraint to cut E looks like an "improvement" on that axis. best_fitness is a reference value.
- Repeated fatal errors (2 consecutive Aspen crashes, 3 consecutive convergence failures, etc.)

Once stopped, record the handoff in **`runs/<name>/HANDOFF.md`** (final performance, number of iterations run, why you stopped, what you would like to do next, signals to watch). The write permission of the algorithm session is limited to inside `runs/`.

---

## Rules to observe

- **Do not read the data of other runs (the run-blind principle).** During the autonomous loop you may consult only
  **your own RUN directory** (its own history: results, ss_change, HANDOFF, etc.) plus `ss_seed.json`,
  `case.yaml`, this instruction sheet and the source code under `src/`. Reading the results, ss_change or HANDOFF of other runs
  to short-cut a proposal destroys the independence of the experiment (what this run could discover on its own) and
  contaminates the evaluation of the method. Whatever should be generalised across runs is reflected into this
  instruction sheet or into the seed/case by a human — knowledge moves between runs only in that form.
- **Do not probe (forced-topology diagnosis) inside the autonomous loop.** Bypassing the optimiser to evaluate a
  hard-coded structure or parameter set and then using the result as grounds for a structural proposal or a
  continue/stop decision contaminates the claim of this method, that "the algorithm discovers structures
  autonomously", and is therefore forbidden in the autonomous loop.
  If you need to isolate the cause of a dead end, do not probe: write "what you want to isolate" into HANDOFF,
  stop, and leave it to the human / development session. (Note: re-acquiring the detailed evaluation of the best
  chosen by the inner optimiser is a recovery of a measurement, not a probe — and run_iteration retries it
  automatically.)
- Write proposals **per unit** (do not enumerate arcs by hand; `apply_ss.py` expands them).
- Direct the inflows of a `delete_unit` to the residue sink (the outlet vertices of the deleted unit and their arcs are simply removed). The product sink is fixed and does not break.
- Do not modify `ss_seed.json` / `case.yaml`. Changes to the SS always go through `ss_change.json`.
- Ask yourself once whether the proposal is physically sound (does the mass balance hold, are you creating an isolated flow). The `validate` of `apply_ss.py` also rejects such cases, but do not make obviously meaningless proposals.
- **Do not run multi-line `python -c "..."` one-liners; write them to `scratch/*.py` and run the file instead**
  (`uv run python scratch/check.py`). A quoted `-c` argument containing newlines and `#` comments trips the
  harness's command-injection heuristic, which overrides the allowlist and blocks the loop on a permission
  prompt until a human answers — during a timed run this silently invalidates the wall-clock measurement.
  Running a `scratch/` file never triggers the prompt.
