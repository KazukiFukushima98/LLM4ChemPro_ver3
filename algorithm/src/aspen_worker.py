"""Disposable child-process entry point: runs one evaluation group in isolation.

The parent (subprocess_evaluator.SubprocessEvaluator) spawns
`python -u aspen_worker.py <req.json>` per evaluation group. The existing
AspenEvaluator runs unchanged inside the child (preserving build-once, in-band
timeout and the rebuild budget).

Each x's result is streamed to stdout as a single JSON line as soon as it is
final (flushed immediately). This is the only liveness signal to the parent,
which declares a stall based on the time since the last message received.
On detecting a wedge the parent runs taskkill /f /im AspenPlus.exe and kills
this process. The child therefore does not start a watchdog of its own
(armed()/beat() are no-ops while the thread is not running).

Important -- stdout is a dedicated channel for result JSON:
    AspenEvaluator emits many diagnostic print() calls on the crash/rebuild
    paths ("Aspen build failed", "Aspen crash/COM-disconnect",
    "rebuild budget exhausted", etc.). If they mix into stdout, the parent's
    json.loads breaks -- and they appear exactly on the crash path, i.e. the
    main scenario this isolation mechanism exists for. So sys.stdout is
    redirected to sys.stderr right at startup, and only result JSON is written
    to the saved result_out (the real stdout). stdout = pure JSON,
    stderr = diagnostics.

req.json schema:
    {
      "mode": "topology" | "detailed",
      "topology": <dump_topology format (arcs as a list)>,
      "x_list": [[...], ...],     # mode=topology
      "x":      [...],            # mode=detailed
      "case": {...}, "aspen_file": "...", "dmp_dir": "..."
    }

Each stdout line:
    {"index": i, "metrics": {...}}                          # mode=topology
    {"index": 0, "metrics": {...}, "stream_results": {...}} # mode=detailed
"""

import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        req = json.load(f)

    # Reserve stdout for result JSON only; divert diagnostic print() from
    # AspenEvaluator and friends to stderr. (Redirecting before `import simulator`
    # is not required -- before constructing ev and evaluating is enough.)
    result_out = sys.stdout
    sys.stdout = sys.stderr

    from simulator import AspenEvaluator  # noqa: E402  (imported after the redirect)
    from topology import load_topology    # noqa: E402

    ev = AspenEvaluator(req["case"], req["aspen_file"], req["dmp_dir"])
    topology = load_topology(req["topology"])  # arcs in list form -> restored to tuple keys

    if req["mode"] == "topology":
        def on_result(idx: int, m) -> None:
            # Stream one result at a time to result_out (the pure stdout) as soon
            # as it is final (flushed immediately).
            print(
                json.dumps({"index": idx, "metrics": asdict(m)}),
                file=result_out,
                flush=True,
            )

        ev.evaluate_topology(topology, req["x_list"], on_result=on_result)
    else:  # detailed
        d = ev.evaluate_detailed(topology, req["x"])
        print(
            json.dumps(
                {
                    "index": 0,
                    "metrics": asdict(d.metrics),
                    "stream_results": d.stream_results,
                }
            ),
            file=result_out,
            flush=True,
        )


if __name__ == "__main__":
    main()
