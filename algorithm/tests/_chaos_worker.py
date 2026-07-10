"""障害注入（chaos）ワーカー：SubprocessEvaluator の耐久テスト用。

aspen_worker.py と同じプロトコル（req.json → stdout に 1結果=1行 JSON）を話すが、
評価の代わりに「現実の Aspen ワーカーが起こし得る故障」を決定論的に再現する。

故障の選択は behavior_for(x_list, seed) で決まる：入力とシードのハッシュから
決定するので、テスト側（親）が同じ関数で「このグループはどう壊れるはずか」を
予言でき、単なる完走確認ではなく挙動ごとのオラクル検証ができる。

behaviors:
    normal         : 全件を canned metrics で正常返答
    slow           : 1件ごとに小さな sleep を挟んで返答（遅いが健全 → 誤 wedge しないこと）
    garbage        : 正常返答の合間に stdout へゴミ（非JSON・非dictのJSON・不正index）を混ぜる
    crash          : 先頭1件だけ返して exit 3（異常終了）
    silent_exit    : 何も出力せず exit 0（正常コードでの沈黙）
    partial_hang   : 前半だけ返して永久スリープ（in-flight wedge）
    hang_no_output : 一切出力せず永久スリープ（build 中 wedge 相当）

pid_dir（case["_chaos"]["pid_dir"]）に自 PID を書き出す。テスト終了後、
親がこの PID 一覧と tasklist を突き合わせて「ゾンビが残っていない」ことを検証する。
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

# 「永久」スリープ。親の stall 判定（数秒）より十分長ければよい。
_HANG_SEC = 600


def behavior_for(x_list: list, seed: int) -> str:
    """入力とシードから故障モードを決定論的に選ぶ（親テストと共有するオラクル）。

    x_list は JSON 往復しても同値になる float のみを想定（json.dumps が安定）。
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
    return "normal"  # 到達しない


def _emit(out, index: int, extra: dict | None = None) -> None:
    msg = {"index": index, "metrics": dict(CANNED)}
    if extra:
        msg.update(extra)
    print(json.dumps(msg), file=out, flush=True)


def _emit_garbage(out) -> None:
    """親の受信ループが耐えるべき stdout 上のゴミを一通り吐く。"""
    for line in (
        "plain text noise from some C library",
        "123",                         # 有効な JSON だが dict でない（数値）
        '"just a string"',             # 同（文字列）
        "[1, 2, 3]",                   # 同（リスト）
        '{"no_index": true}',          # dict だが index なし
        '{"index": 9999, "metrics": {}}',  # 範囲外 index
        "",                            # 空行
        "{broken json",                # 壊れた JSON
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

    # detailed モードは x 1点をリスト化して同じオラクルに乗せる
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
            time.sleep(0.3)  # stall_sec より十分短い「健全な遅さ」
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
