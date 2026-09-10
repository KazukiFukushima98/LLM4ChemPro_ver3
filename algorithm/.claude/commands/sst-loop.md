---
description: Drive the SST outer loop autonomously (repeat analyze -> apply_ss -> run_iteration until a stopping condition)
argument-hint: <run dir, e.g. runs/<name>> [max iterations] [--dry]
---
Act as the SST agent (`CLAUDE.md`, the instructions for this algorithm directory) and drive the outer loop autonomously. **Do not ask the user for confirmation at each iteration** (the policy in `CLAUDE.md`).
**Assume the session was opened in `algorithm/`. Run every command from there** (scripts are at `src/...`, run data lives in `runs/`, which is gitignored).

**Interpreting the arguments**: read `$ARGUMENTS` (the full text of the arguments passed) as whitespace-separated tokens. Do not use the positional `$1`/`$2`, because whether they are 0- or 1-based varies between environments — always split `$ARGUMENTS` yourself.
- The first token is the **target run directory** (e.g. `runs/<name>`), referred to as **RUN** below. If it is empty, ask which run to use and stop.
- The second integer token, if present, is the **maximum number of iterations**, referred to as **N** below. Without it, run until a stopping condition.
- If `--dry` is present, use validation mode.

`--dry`: validation mode. Run step 4 with the reduced-budget flags plus `--no-commit` (small scale, `case.yaml` untouched, no commit), and skip the record-keeping on stop. **Choose the reduced-budget flags by looking at `optimizer` in `case.yaml`**: for `bo` use `--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2`, and for `ga` (or when unspecified) use `--pop 4 --gen 3`. The `--pop/--gen` flags belong to the GA path and are not read under BO (and vice versa; run_iteration emits a warning). For a production run, pass neither.

**Notes for hands-off operation (so an unattended run does not stall)**. Permission is decided by **the shape of the command** (`.claude/settings.json`), so observe the following:
- Read results.json and the logs **with the Read tool** (do not dump them to the shell with `cat` or a bare `python -c`). If you need to compute something, use **`uv run python -c '…'`** (no `cd`, a single `uv run`).
- **Do not build compound shell commands containing pipes, `while`/`for` loops, `$(…)`, or variable expansion** — such a command cannot be judged safe even with an allowlist and will always stop at a confirmation dialog (which kills unattended operation). To look at several files, repeat Glob + Read, or fold the work into a single `uv run python -c '…'`.
- To roll back, either **write an inverse `ss_change` and run `apply_ss`**, or write the contents of `ss_before_change.json` back to `RUN/ss_current.json` with the Write tool (writes into `runs/` pass through; use `--force` when re-applying to the same iter). Do not use the shell's `copy`/`cp`/`mv`.
- Confirm the auto-commit from the output of run_iteration; do not separately invoke `git status`/`git log`.
- Issue commands with the **Bash tool** (with the PowerShell tool the permission rules do not apply and everything turns into a confirmation dialog).
- `case.yaml` / `ss_seed.json` cannot be written because of the deny settings (this is by design; ask a human for changes).
- **Do not read any run directory other than RUN** (the run-blind principle; see "Rules to observe" in `CLAUDE.md`). If you need the format of ss_change, look at the examples in `CLAUDE.md` (and the docstrings of `src/apply_ss.py`).

Repeat "The procedure for one iteration" from `CLAUDE.md` as follows:

0. **Bootstrapping a new run**: if `RUN/ss_current.json` does not exist, copy the contents of `ss_seed.json` verbatim to `RUN/ss_current.json` with the Write tool, take a baseline (iter_001, no ss_change) with `uv run python -u src/run_iteration.py --base-dir RUN > RUN/iter_001_log.txt 2>&1`, then go to 1.
1. In `RUN/iterations/`, treat the highest-numbered `iter_MMM/` that has a `results.json` as the latest (call the number M). Read that `results.json`.
2. If `iter_MMM/ss_change.json` **already exists** (hand-written, or prepared by `/sst-analyze`), use it. **If it does not**, extract the signals and write a "deletion + addition set" `ss_change.json` into `iter_MMM/` (quote the numbers in `reason`, check the CO2 of the retentate you send back for a recycle, and check for yourself that the binary cap `max_binary_variables` in `case.yaml` is respected).
3. `uv run python src/apply_ss.py --base-dir RUN --iter M`
4. `uv run python -u src/run_iteration.py --base-dir RUN > RUN/iter_(M+1)_log.txt 2>&1` (run_iteration assigns the next number automatically as max+1). **When `--dry` is given, run it with the reduced-budget flags for the current optimizer plus `--no-commit`** (BO: `--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2`; GA: `--pop 4 --gen 3`).
5. Read the newly produced `results.json` and **report concisely**: the signals you read (with numbers), the ss_change you wrote and its rationale, and the purity, recovery and specific energy of the new results.
6. **Stopping decision**: stop when a stopping condition in `CLAUDE.md` is met (an explicit stop from the user / **no improvement for 3 consecutive iterations** on the `performance` criterion [the shortfall while the constraints are unmet, and the **cost `cost_usd_per_tCO2`** once they are met; **do not use best_fitness for this decision**] / 2 consecutive Aspen crashes or 3 consecutive convergence failures), or when **N** iterations have been reached. **The 3-consecutive count is the sole convergence criterion: do not stop earlier because no structural hypothesis remains or the variation looks like optimizer noise — in that case re-run the current SS unchanged (step 4 only) and let the count accumulate.** Otherwise go back to 1.

Once stopped, record the handoff in **`RUN/HANDOFF.md`** (final performance, number of iterations run, why you stopped, what you would do next, signals to watch). You do not need to copy anything into `../docs/` — the development session (at the repository root) does that. Leave the auto-commit to run_iteration. On a failure, record it and then judge whether to continue (the rollback decision in `CLAUDE.md`).

**In `--dry` validation**, skip the auto-commit check in step 6 and **do not** record `HANDOFF.md` on stop (report the validation result in chat instead).
