"""Dummy worker for testing SubprocessEvaluator (no Aspen, deterministic).

Speaks the same stdout protocol as the real aspen_worker.py (one result = one
line of JSON) but never touches Aspen. case["_test_behavior"] selects the
behaviour:

    normal       : return canned Metrics for every x (happy path)
    partial_hang : return the first half, then sleep forever (mid-way wedge -> parent kills)
    abnormal     : return index 0, then exit(2) (abnormal exit -> the rest are bad)
    full_hang    : return nothing and sleep forever (full wedge)

Forced hangs are used because a natural wedge is timing-dependent (see
timing-dependent) and cannot be tested deterministically. The parent treats a wedge
and a forced hang identically ("timeout -> kill"), so this still exercises the
mechanism.
"""

import json
import sys
import time

req = json.load(open(sys.argv[1], encoding="utf-8"))
behavior = req["case"].get("_test_behavior", "normal")
n = len(req.get("x_list", [0]))

CANNED = {
    "specific_energy": 100.0,
    "purity": 0.9,
    "recovery": 0.8,
    "energy_breakdown": {},
}


def out(i: int) -> None:
    print(json.dumps({"index": i, "metrics": CANNED}), flush=True)


if behavior == "normal":
    for i in range(n):
        out(i)
elif behavior == "partial_hang":
    for i in range(n // 2):
        out(i)
    time.sleep(10**6)
elif behavior == "abnormal":
    out(0)
    sys.exit(2)
elif behavior == "full_hang":
    time.sleep(10**6)
