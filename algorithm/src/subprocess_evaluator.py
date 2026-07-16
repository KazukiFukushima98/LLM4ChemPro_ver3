"""Process-isolation supervisor (Evaluator Protocol implementation).

A guaranteed last resort against the run6 wedge (an in-flight COM call that never
returns even after a server kill). The parent (this class) never touches COM; it
spawns the child process aspen_worker.py per evaluation group, and the existing
AspenEvaluator runs inside the child.

Layers:
    inside the child = fast first-line recovery (class-1 path: a crash that returns
                       via RPC disconnect -> rebuild)
    parent process   = guaranteed last resort (an in-flight stall that never
                       returns -> kill the whole child). TerminateProcess is
                       unconditional, so it does not depend on how COM is stuck.

The only liveness signal is the result stream on the child's stdout. No separate
heartbeat thread is used (a heartbeat would mask a wedge). If the time since the
last result exceeds stall_sec, it is treated as a wedge. Evaluations are sequential
within run_ga, so there is only ever one child and one Aspen at a time, which makes
taskkill /f /im AspenPlus.exe (kill by image name) safe.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import DetailedResult, Metrics  # noqa: E402
from topology import dump_topology              # noqa: E402


def _default_kill_aspen() -> None:
    """taskkill AspenPlus.exe (with a timeout).

    os.system has no timeout and can block the caller (the supervising thread)
    indefinitely against a wedged Aspen that has become unkillable (COORDINATION:
    the main cause of the 85-minute hang in run13 -- the "guaranteed last resort"
    itself got stuck here). The subprocess.run timeout guarantees a return, so a
    stuck kill never ties up the supervisor.
    """
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "AspenPlus.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except Exception:
        pass  # never block on a timeout / failure (keeping the supervisor alive comes first)


class SubprocessEvaluator:
    """Evaluator that isolates each evaluation group in a disposable child process."""

    def __init__(
        self,
        case: dict,
        aspen_file: str,
        dmp_dir: str,
        *,
        worker_script: str | None = None,
        python_exe: str | None = None,
        stall_sec: float | None = None,
        kill_aspen=None,
    ) -> None:
        self._case = case
        self._aspen_file = aspen_file
        self._dmp_dir = dmp_dir
        self._worker = worker_script or os.path.join(os.path.dirname(__file__), "aspen_worker.py")
        self._python = python_exe or sys.executable

        et = int(case.get("aspen_timeout_eval", 60))
        dt = int(case.get("aspen_timeout_detail", 120))
        # Upper bound on "no result received" before declaring a wedge. Must exceed the
        # in-band per-x timeout (et/dt). The lower bound is the legitimate silence of a
        # build plus an intermediate rebuild (~150s): going below it falsely kills healthy
        # work, turning correct evaluations into spurious BAD_VALUEs that pollute GA/BO.
        # Split by use: during GA (topology) only eval(et) applies, so keep it short; the
        # final detailed extraction (detailed) follows detail(dt). The buffer is 120s
        # (~90s for build/rebuild plus a 30s margin against false wedges; the old 90s
        # equalled the ~150s estimated ceiling of legitimate silence, i.e. zero margin,
        # and could falsely kill a slow build).
        # An explicit override (stall_sec / case.subprocess_stall_sec) applies to both
        # modes (for tests and manual overrides).
        override = (
            stall_sec
            if stall_sec is not None
            else (int(case["subprocess_stall_sec"]) if "subprocess_stall_sec" in case else None)
        )
        self._stall_topology = override if override is not None else et + 120
        self._stall_detailed = override if override is not None else dt + 120
        # Injectable for tests (essential so tests never kill a real Aspen).
        # The default has a timeout (_default_kill_aspen); see its docstring.
        self._kill_aspen = kill_aspen or _default_kill_aspen

        # Instrumentation (2026-07-15): wall-clock record per evaluation group.
        # run_iteration copies it into the timing field of results.json. Never used
        # for decisions or control (record-only).
        #   groups[i] = {mode, n_requested, n_results, wall_sec, wedged, rc,
        #                eval_sec: [seconds between received results (the first includes the build)],
        #                lost_sec: seconds lost to a wedge/abnormal exit (last result -> exit)}
        self.timing: dict[str, Any] = {"groups": []}

    # ------------------------------------------------------------------
    # Child-process driver (spawn -> receive stream -> detect stall -> kill)
    # ------------------------------------------------------------------

    def _run_worker(self, request: dict, n: int, stall: float) -> list[dict | None]:
        """Spawn the child and fill the stdout result stream into index positions.

        stall : seconds without a result before declaring a wedge (per mode; see __init__).
        Returns a list of length n: the child's message dict at every received index,
        None where nothing arrived (wedge / abnormal exit / missing).
        """
        results: list[dict | None] = [None] * n
        _t0 = time.monotonic()          # instrumentation: wall clock for the whole group
        _t_last = _t0                   # instrumentation: last result timestamp (gap = wall time of one evaluation)
        _eval_sec: list[float] = []

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(request, f)
            req_path = f.name

        # stdout = pure JSON channel, stderr = the child's diagnostics (a separate pipe,
        # not merged into STDOUT). Force the child's stdio to UTF-8: AspenEvaluator's
        # diagnostics contain Japanese, and if the child defaults to cp932 while the parent
        # reads strict utf-8, erdr dies with UnicodeDecodeError and all subsequent crash
        # diagnostics are lost. PYTHONUTF8=1 puts the child in UTF-8; errors="replace" is a
        # safety net.
        proc = subprocess.Popen(
            [self._python, "-u", self._worker, req_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        q: queue.Queue = queue.Queue()
        SENT = object()  # signals stdout closed (the child finished emitting results / died)
        err: list[str] = []

        def rdr() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    q.put(line)
            finally:
                q.put(SENT)

        def erdr() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    err.append(line)
            except Exception:
                pass

        threading.Thread(target=rdr, daemon=True).start()
        erdr_thread = threading.Thread(target=erdr, daemon=True)
        erdr_thread.start()

        wedged = False
        try:
            while True:
                try:
                    item = q.get(timeout=stall)
                except queue.Empty:
                    wedged = True  # no result for `stall` seconds -> wedge
                    break
                if item is SENT:
                    break
                line = item.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    # Ignore non-JSON noise on stdout (output from the C layer or
                    # libraries) defensively.
                    continue
                if not isinstance(msg, dict):
                    # Valid JSON but not a dict (a number, a string, ...). msg.get would
                    # raise AttributeError and take down the whole evaluation loop, so
                    # ignore it as noise.
                    continue
                i = msg.get("index")
                if isinstance(i, int) and 0 <= i < n:
                    results[i] = msg
                    _now = time.monotonic()          # instrumentation: the gap is this item's wall time
                    _eval_sec.append(round(_now - _t_last, 2))
                    _t_last = _now
        finally:
            # Play the strongest card first (COORDINATION: fix for the ordering trap).
            # proc.kill() is TerminateProcess -- a last resort that always works,
            # independent of how COM is stuck. Doing it first releases the worker's
            # in-flight COM handles as well, so the pipeline is guaranteed to move on.
            # Previously _kill_aspen() below (os.system, no timeout) came first, and an
            # unkillable Aspen stalled there, never reaching proc.kill(): an 85-minute hang.
            try:
                proc.kill()         # unconditionally kill the child itself (harmless if already exited)
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            # Clean up Aspen itself (with a timeout) only after the worker is definitely
            # gone. Anything missed here is reclaimed by the next worker's build, which
            # runs taskkill at startup.
            if wedged:
                self._kill_aspen()
            erdr_thread.join(timeout=2)  # wait for the drain to finish before reading the err tail
            try:
                os.remove(req_path)
            except OSError:
                pass

        abnormal = proc.returncode not in (0, None)
        # Clean up when an abnormal exit may have left Aspen behind (evaluation is
        # sequential, i.e. one at a time, so kill by image name is safe). On a wedge the
        # kill already happened, so do not call it twice.
        if abnormal and not wedged:
            self._kill_aspen()
        if wedged or abnormal:
            missing = sum(r is None for r in results)
            print(
                f"    [subprocess] wedged={wedged} rc={proc.returncode} "
                f"missing={missing}/{n}"
                + (f"\n    stderr tail: {''.join(err[-5:])}" if err else "")
            )

        # Instrumentation record (record-only; on a wedge/abnormal exit, "last result ->
        # exit" is charged to lost_sec)
        _t_end = time.monotonic()
        self.timing["groups"].append({
            "mode": request.get("mode"),
            "n_requested": n,
            "n_results": sum(r is not None for r in results),
            "wall_sec": round(_t_end - _t0, 2),
            "wedged": wedged,
            "rc": proc.returncode,
            "eval_sec": _eval_sec,
            "lost_sec": round(_t_end - _t_last, 2) if (wedged or abnormal) else 0.0,
        })
        return results

    # ------------------------------------------------------------------
    # Evaluator Protocol
    # ------------------------------------------------------------------

    def evaluate_topology(
        self,
        topology: dict[str, Any],
        x_list: list[list[float]],
    ) -> list[Metrics]:
        req = {
            "mode": "topology",
            "topology": dump_topology(topology),  # tuple-keyed arcs -> list form (JSON-serialisable)
            "x_list": x_list,
            "case": self._case,
            "aspen_file": self._aspen_file,
            "dmp_dir": self._dmp_dir,
        }
        raw = self._run_worker(req, len(x_list), self._stall_topology)
        return [
            Metrics(**m["metrics"]) if m is not None else Metrics.bad() for m in raw
        ]

    def evaluate_detailed(
        self,
        topology: dict[str, Any],
        x: list[float],
    ) -> DetailedResult:
        req = {
            "mode": "detailed",
            "topology": dump_topology(topology),
            "x": x,
            "case": self._case,
            "aspen_file": self._aspen_file,
            "dmp_dir": self._dmp_dir,
        }
        raw = self._run_worker(req, 1, self._stall_detailed)
        m = raw[0]
        if m is None:
            return DetailedResult(metrics=Metrics.bad())
        return DetailedResult(
            metrics=Metrics(**m["metrics"]),
            stream_results=m.get("stream_results", {}),
        )
