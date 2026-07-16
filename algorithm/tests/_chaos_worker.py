"""Fault-injection (chaos) worker for soak-testing SubprocessEvaluator.

Speaks the same protocol as aspen_worker.py (req.json -> one result = one line
of JSON on stdout), but instead of evaluating anything it deterministically
reproduces the failures a real Aspen worker can exhibit.

The failure is selected by behavior_for(x_list, seed): it is derived from a hash
of the input and the seed, so the test (the parent) can predict how a given
group is supposed to break using the same function. That turns the test into a
per-behaviour oracle check rather than a mere "it ran to completion" check.

behaviors:
    normal         : respond normally with canned metrics for every item
    slow           : respond with a short sleep between items (slow but healthy -> must not be misread as a wedge)
    garbage        : interleave junk on stdout (non-JSON, non-dict JSON, invalid index) between valid responses
    crash          : return only the first item, then exit 3 (abnormal exit)
    silent_exit    : output nothing and exit 0 (silence with a success code)
    partial_hang   : return the first half, then sleep forever (in-flight wedge)
    hang_no_output : output nothing at all and sleep forever (equivalent to a wedge during build)

Writes its own PID into pid_dir (case["_chaos"]["pid_dir"]). After the test, the
parent cross-checks this list of PIDs against tasklist to verify that no zombie
process is left behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

BEHAVIORS: list[tuple[str, int]] = [
    # (name, weight)
    ("normal",         5),
    ("slow",           2),
    ("garbage",        4),
    ("crash",          3),
    ("silent_exit",    2),
    ("partial_hang",   2),
    ("hang_no_output", 1),
]

CANNED = {"specific_energy": 100.0, "purity": 0.9, "recovery": 0.8,
          "energy_breakdown": {}}

# "Forever" sleep. It only needs to be well beyond the parent's stall threshold
# (a few seconds).
_HANG_SEC = 600


def behavior_for(x_list: list, seed: int) -> str:
    """Deterministically pick a failure mode from the input and the seed (the oracle shared with the parent test).

    x_list is assumed to hold only floats that survive a JSON round trip
    unchanged (so json.dumps is stable).
    """
    digest = hashlib.sha256(
        json.dumps([seed, x_list], sort_keys=True).encode("utf-8")
    ).hexdigest()
    r = int(digest[:8], 16) % sum(w for _, w in BEHAVIORS)
    acc = 0
    for name, w in BEHAVIORS:
        acc += w
        if r < acc:
            return name
    return "normal"  # unreachable


def _emit(out, index: int, extra: dict | None = None) -> None:
    msg = {"index": index, "metrics": dict(CANNED)}
    if extra:
        msg.update(extra)
    print(json.dumps(msg), file=out, flush=True)


def _emit_garbage(out) -> None:
    """Emit the full range of stdout junk the parent's receive loop must tolerate."""
    for line in (
        "plain text noise from some C library",
        "123",                         # valid JSON but not a dict (number)
        '"just a string"',             # ditto (string)
        "[1, 2, 3]",                   # ditto (list)
        '{"no_index": true}',          # a dict, but with no index
        '{"index": 9999, "metrics": {}}',  # index out of range
        "",                            # empty line
        "{broken json",                # malformed JSON
    ):
        print(line, file=out, flush=True)


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        req = json.load(f)

    case = req.get("case", {})
    chaos = case.get("_chaos", {})
    seed = int(chaos.get("seed", 0))

    pid_dir = chaos.get("pid_dir")
    if pid_dir:
        with open(os.path.join(pid_dir, f"{os.getpid()}.pid"), "w") as f:
            f.write(str(os.getpid()))

    # in detailed mode, wrap the single x in a list so it goes through the same oracle
    if req["mode"] == "detailed":
        x_list = [req["x"]]
        detailed = True
    else:
        x_list = req["x_list"]
        detailed = False

    behavior = behavior_for(x_list, seed)
    out = sys.stdout
    n = len(x_list)

    def emit(i: int) -> None:
        extra = {"stream_results": {"V0": {"CO2_molfrac": 0.15}}} if detailed else None
        _emit(out, i, extra)

    if behavior == "normal":
        for i in range(n):
            emit(i)
    elif behavior == "slow":
        for i in range(n):
            time.sleep(0.3)  # "healthy slowness": comfortably shorter than stall_sec
            emit(i)
    elif behavior == "garbage":
        for i in range(n):
            _emit_garbage(out)
            emit(i)
    elif behavior == "crash":
        if n > 0:
            emit(0)
        sys.exit(3)
    elif behavior == "silent_exit":
        sys.exit(0)
    elif behavior == "partial_hang":
        for i in range(n // 2):
            emit(i)
        time.sleep(_HANG_SEC)
    elif behavior == "hang_no_output":
        time.sleep(_HANG_SEC)


if __name__ == "__main__":
    main()
