"""使い捨て子プロセス・エントリ：1 評価グループを隔離実行する。

親（subprocess_evaluator.SubprocessEvaluator）が評価グループごとに
`python -u aspen_worker.py <req.json>` を spawn する。子の中で既存の
AspenEvaluator がそのまま動く（build-once・in-band timeout・再ビルド予算を温存）。

各 x の結果は確定したそばから stdout に JSON 1 行で流す（即 flush）。
これだけを親への生存信号とし、親は「最後の受信からの経過」で stall 判定する。
親が wedge を検知したら taskkill /f /im AspenPlus.exe → このプロセスを kill する。
したがって子は自分では watchdog を起動しない（armed()/beat() は thread 未起動で no-op）。

重要 — stdout は結果 JSON 専用チャネル:
    AspenEvaluator はクラッシュ/再ビルド経路で診断 print() を多数出す
    （"Aspen build failed", "Aspen crash/COM-disconnect", "rebuild budget exhausted" 等）。
    これらが stdout に混じると親の json.loads が壊れる。しかも出るのは
    クラッシュ経路＝この隔離機構が存在する理由そのものの主シナリオ。
    そこで起動直後に sys.stdout を sys.stderr に差し替え、結果 JSON だけを
    退避した result_out（本物の stdout）へ書く。stdout = 純 JSON、stderr = 診断。

req.json スキーマ:
    {
      "mode": "topology" | "detailed",
      "topology": <dump_topology 形式（arcs は list）>,
      "x_list": [[...], ...],     # mode=topology
      "x":      [...],            # mode=detailed
      "case": {...}, "aspen_file": "...", "dmp_dir": "..."
    }

stdout の各行:
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

    # stdout を結果 JSON 専用に確保し、AspenEvaluator 等の診断 print() は stderr へ逃がす。
    # （import simulator より前に差し替える必要はないが、ev の構築・評価より前で十分。）
    result_out = sys.stdout
    sys.stdout = sys.stderr

    from simulator import AspenEvaluator  # noqa: E402  （差し替え後に import）
    from topology import load_topology    # noqa: E402

    ev = AspenEvaluator(req["case"], req["aspen_file"], req["dmp_dir"])
    topology = load_topology(req["topology"])  # list 形式 arcs → タプルキーに復元

    if req["mode"] == "topology":
        def on_result(idx: int, m) -> None:
            # 確定そばから 1 件ずつ result_out（純 stdout）へ流す（即 flush）。
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
