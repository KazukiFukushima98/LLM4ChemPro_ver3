---
description: Analyse the latest results.json and propose/write the next ss_change.json (does not apply or run)
argument-hint: <run dir, e.g. runs/run24>
---
Act as the SST agent (`CLAUDE.md`, the instructions for this algorithm directory). Only make a proposal; do not apply or execute it. **Assume the session was opened in `algorithm/`** (run data lives in `runs/`).

**Interpreting the arguments**: take the first whitespace-separated token of `$ARGUMENTS` as the **target run directory** (referred to as **RUN** below). Do not use the positional `$1`, because whether it is 0- or 1-based varies between environments. If it is empty, ask which run to use.

1. In `RUN/iterations/`, treat the highest-numbered `iter_MMM/` that has a `results.json` as the latest and read that `results.json` (call the number M; skip directories without a `results.json`).
2. Extract the signals following "Signals -> structural proposals" in `CLAUDE.md` (variables pinned at a bound / energy domination / CO2 loss in the retentate / inactive candidates / constraint violations). **Always quote the concrete numbers you read.**
3. Assemble an `ss_change.json` as a "deletion + addition set" and write it to `RUN/iterations/iter_MMM/ss_change.json`. Quote the supporting numbers in `reason`. If you add a recycle, check the CO2 flow of the retentate you send back. Check for yourself that the number of binary candidates stays within `case.yaml.max_binary_variables` (the cap on the total has been removed).
4. **Do not run `apply_ss.py` or `run_iteration.py`.** Report the proposal and its rationale, then stop (this mode exists so a human can inspect the content).
