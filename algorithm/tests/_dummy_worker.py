"""SubprocessEvaluator のテスト用ダミー worker（Aspen 不要・決定的）。

実 aspen_worker.py と同じ stdout プロトコル（1 結果 = 1 行 JSON）を喋るが、
Aspen には一切触れない。case["_test_behavior"] で挙動を切り替える:

    normal       : 全 x を canned Metrics で返す（ハッピーパス）
    partial_hang : 前半だけ返して以降は永久 sleep（途中 wedge → 親が kill）
    abnormal     : index 0 を返して exit(2)（異常終了 → 残りは bad）
    full_hang    : 1 件も返さず永久 sleep（全 wedge）

forced hang を使うのは、自然 wedge が timing 依存（COORDINATION 参照）で
決定的にテストできないため。親は wedge でも forced hang でも「タイムアウト→kill」で
同じに扱うので、これで機構を検証できる。
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
