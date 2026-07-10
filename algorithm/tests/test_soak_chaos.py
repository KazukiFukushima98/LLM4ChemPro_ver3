"""SubprocessEvaluator の障害注入・耐久テスト（soak / chaos。Aspen 不要・決定論）。

_chaos_worker.py が「現実の Aspen ワーカーが起こし得る故障」（wedge・クラッシュ・
沈黙・stdout ゴミ・遅延）をグループごとにランダム（ただしシード決定論）に再現し、
親（SubprocessEvaluator）の3つの生存不変条件を多数グループにわたって検証する：

    1. evaluate_topology / evaluate_detailed は決して例外を投げず、
       必ず len(x_list) 件の Metrics（受信分は canned、欠損分は bad）を返す
    2. wedge / 異常終了時は kill が呼ばれ、それ以外では呼ばれない
    3. テスト終了後にワーカーのゾンビプロセスが残らない

故障モードは behavior_for()（テストとワーカーで共有）で予言できるため、
「完走した」だけでなく「各故障で期待どおりの結果になった」ことまで検証する。

実行:
    uv run python -m unittest algorithm.tests.test_soak_chaos          # 既定 24 グループ（~1分）
    CHAOS_GROUPS=200 uv run python -m unittest algorithm.tests.test_soak_chaos   # 本格 soak
    （PowerShell: $env:CHAOS_GROUPS="200"; uv run python -m unittest ...）
    CHAOS_SEED で故障系列を変えられる（既定 20260707）。
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from evaluator import BAD_VALUE, Metrics  # noqa: E402
from subprocess_evaluator import SubprocessEvaluator  # noqa: E402
from _chaos_worker import behavior_for  # noqa: E402

CHAOS_WORKER = os.path.join(HERE, "_chaos_worker.py")

CHAOS_GROUPS = int(os.environ.get("CHAOS_GROUPS", "24"))
CHAOS_SEED = int(os.environ.get("CHAOS_SEED", "20260707"))
STALL_SEC = 4.0  # 故障検知までの待ち。spawn jitter に耐える値（test_subprocess_evaluator と同基準）

TOPO = {"vertices": {"V0": {"role": "feed"}}, "arcs": {}, "units": {}}

# wedge / 異常終了として kill が呼ばれるべき故障モード
KILL_BEHAVIORS = {"crash", "partial_hang", "hang_no_output"}


def _is_bad(m: Metrics) -> bool:
    return m.specific_energy >= BAD_VALUE


def _is_canned(m: Metrics) -> bool:
    return m.specific_energy == 100.0 and m.purity == 0.9 and m.recovery == 0.8


def _alive_python_pids(pids: set[int]) -> list[int] | None:
    """記録した PID のうち python プロセスとして生存しているものを返す。

    tasklist が使えない環境では None（検証スキップ）。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return None
    alive: list[int] = []
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 2 and cols[0].lower().startswith("python"):
            try:
                pid = int(cols[1])
            except ValueError:
                continue
            if pid in pids:
                alive.append(pid)
    return alive


def _expected_pattern(behavior: str, n: int) -> list[str]:
    """故障モードごとの期待結果（'canned' / 'bad' の並び）。"""
    if behavior in ("normal", "slow", "garbage"):
        return ["canned"] * n
    if behavior == "crash":
        return ["canned"] + ["bad"] * (n - 1)
    if behavior == "partial_hang":
        k = n // 2
        return ["canned"] * k + ["bad"] * (n - k)
    # silent_exit / hang_no_output
    return ["bad"] * n


class _KillRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class TestChaosSoak(unittest.TestCase):

    def test_soak_topology_groups(self) -> None:
        """多数グループに故障を注入しても、結果の形・復旧・後始末の不変条件が守られる。"""
        rng = random.Random(CHAOS_SEED)
        rec = _KillRecorder()
        behavior_counts: Counter[str] = Counter()
        expected_kills = 0
        pids: set[int] = set()

        with tempfile.TemporaryDirectory() as pid_dir:
            case = {"_chaos": {"seed": CHAOS_SEED, "pid_dir": pid_dir}}
            ev = SubprocessEvaluator(
                case, aspen_file="<unused>", dmp_dir="<unused>",
                worker_script=CHAOS_WORKER, stall_sec=STALL_SEC, kill_aspen=rec,
            )

            for g in range(CHAOS_GROUPS):
                n = rng.randint(1, 5)
                dim = rng.randint(1, 3)
                x_list = [[round(rng.uniform(0.0, 100.0), 6) for _ in range(dim)]
                          for _ in range(n)]
                behavior = behavior_for(x_list, CHAOS_SEED)
                behavior_counts[behavior] += 1
                if behavior in KILL_BEHAVIORS:
                    expected_kills += 1

                out = ev.evaluate_topology(TOPO, x_list)

                # 不変条件1: 形が必ず揃う（例外はここまで到達した時点で無し）
                self.assertEqual(
                    len(out), n,
                    f"group {g} ({behavior}): {n} 件要求に {len(out)} 件返答",
                )
                # オラクル: 故障モードごとの期待パターン
                for i, (m, exp) in enumerate(zip(out, _expected_pattern(behavior, n))):
                    if exp == "canned":
                        self.assertTrue(
                            _is_canned(m),
                            f"group {g} ({behavior}) x[{i}]: canned 期待が {m}",
                        )
                    else:
                        self.assertTrue(
                            _is_bad(m),
                            f"group {g} ({behavior}) x[{i}]: bad 期待が {m}",
                        )

            # 不変条件2: kill は wedge / 異常終了のグループ数とちょうど一致
            self.assertEqual(
                rec.calls, expected_kills,
                f"kill 回数 {rec.calls} != 期待 {expected_kills}（{dict(behavior_counts)}）",
            )

            # 不変条件3: ゾンビワーカーが残らない
            for name in os.listdir(pid_dir):
                if name.endswith(".pid"):
                    pids.add(int(name[:-4]))
            alive = _alive_python_pids(pids)
            if alive is not None:
                self.assertEqual(
                    alive, [],
                    f"ゾンビワーカー残存: PID {alive}",
                )

        print(f"\n[soak] groups={CHAOS_GROUPS} seed={CHAOS_SEED} "
              f"behaviors={dict(behavior_counts)} kills={rec.calls} "
              f"workers_spawned={len(pids)}")

    def test_soak_detailed_calls(self) -> None:
        """evaluate_detailed（best 詳細抽出の経路）も故障注入下で必ず DetailedResult を返す。"""
        rng = random.Random(CHAOS_SEED + 1)
        rec = _KillRecorder()

        with tempfile.TemporaryDirectory() as pid_dir:
            case = {"_chaos": {"seed": CHAOS_SEED, "pid_dir": pid_dir}}
            ev = SubprocessEvaluator(
                case, aspen_file="<unused>", dmp_dir="<unused>",
                worker_script=CHAOS_WORKER, stall_sec=STALL_SEC, kill_aspen=rec,
            )

            n_calls = max(4, CHAOS_GROUPS // 4)
            for _ in range(n_calls):
                x = [round(rng.uniform(0.0, 100.0), 6)]
                behavior = behavior_for([x], CHAOS_SEED)
                d = ev.evaluate_detailed(TOPO, x)
                if behavior in ("normal", "slow", "garbage", "crash"):
                    # crash は index0 送出後に exit するので detailed(1点) は受信済み
                    self.assertTrue(_is_canned(d.metrics),
                                    f"{behavior}: canned 期待が {d.metrics}")
                    self.assertIn("V0", d.stream_results)
                else:
                    self.assertTrue(_is_bad(d.metrics),
                                    f"{behavior}: bad 期待が {d.metrics}")


if __name__ == "__main__":
    unittest.main()
