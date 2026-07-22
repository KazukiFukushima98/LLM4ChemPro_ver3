"""生 run データ(algorithm/runs/)から論文用の軽量 CSV を抽出する。

計算ノード上の runs/ は gitignore されている(ログ・スナップショット込みで重い)。
このスクリプトは results.json / baseline_summary.json だけを読み、
run 1つにつき次の3ファイルを results/runs/ に書き出す(こちらは git 追跡対象)。

    <run>_iterations.csv   反復ごとの best 性能(1行 = 1反復)
    <run>_progress.csv     内側最適化の時系列(1行 = gen_log の1エントリ)
    <run>.meta.json        再現情報(抽出時刻・hostname・commit hash・元データの所在)

追記専用:出力が既に存在する run はスキップする(--force で明示的に上書き)。
既存 run の CSV は不変なので、run が増えても過去の抽出結果は変わらない。

依存は標準ライブラリのみ。計算ノード(Windows)でも Mac でもそのまま動く。

使い方:
    uv run python scripts/extract_runs.py                 # 新規 run だけ抽出
    uv run python scripts/extract_runs.py --runs run30    # 特定 run のみ
    uv run python scripts/extract_runs.py --force --runs run30
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "algorithm" / "runs"
DEFAULT_OUT = REPO_ROOT / "results" / "runs"

ITER_FIELDS = [
    "run", "kind", "iteration", "optimizer", "seed", "n_evaluations",
    "CO2_purity", "CO2_recovery", "specific_energy_kWh_tCO2",
    "total_compressor_kW", "cost_usd_per_tCO2",
    "inner_opt_sec", "n_wedges",
]

PROGRESS_FIELDS = ["run", "iteration", "gen", "phase", "t", "best_fitness"]


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def run_kind(name: str) -> str:
    return "baseline" if name.startswith("baseline_") else "sst"


def iter_dirs(run_dir: Path) -> list[Path]:
    root = run_dir / "iterations"
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "results.json").exists())


def extract_run(run_dir: Path, out_dir: Path) -> dict:
    """1 run 分を抽出し、書き出したファイルの情報を返す。"""
    name = run_dir.name
    iters, progress = [], []

    for d in iter_dirs(run_dir):
        r = json.loads((d / "results.json").read_text(encoding="utf-8"))
        perf = r.get("performance", {})
        timing = r.get("timing") or {}
        gen_log = r.get("gen_log", [])
        iters.append({
            "run": name,
            "kind": run_kind(name),
            "iteration": r.get("iteration"),
            "optimizer": r.get("optimizer"),
            "seed": r.get("seed"),
            "n_evaluations": r.get("n_evaluations"),
            "CO2_purity": perf.get("CO2_purity"),
            "CO2_recovery": perf.get("CO2_recovery"),
            "specific_energy_kWh_tCO2": perf.get("specific_energy_kWh_tCO2"),
            "total_compressor_kW": perf.get("total_compressor_kW"),
            "cost_usd_per_tCO2": perf.get("cost_usd_per_tCO2"),
            "inner_opt_sec": timing.get("inner_opt_sec")
                             or (gen_log[-1].get("t") if gen_log else None),
            "n_wedges": timing.get("n_wedges"),
        })
        for g in gen_log:
            progress.append({
                "run": name,
                "iteration": r.get("iteration"),
                "gen": g.get("gen"),
                "phase": g.get("phase"),
                "t": g.get("t"),
                "best_fitness": g.get("best_fitness"),
            })

    if not iters:
        return {}

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for suffix, fields, rows in [
        ("_iterations.csv", ITER_FIELDS, iters),
        ("_progress.csv", PROGRESS_FIELDS, progress),
    ]:
        path = out_dir / f"{name}{suffix}"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        files[path.name] = len(rows)

    meta = {
        "run": name,
        "kind": run_kind(name),
        "n_iterations": len(iters),
        "source_dir": str(run_dir),
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "git_commit": git_commit(),
        "files": files,
    }
    summary_path = run_dir / "baseline_summary.json"
    if summary_path.exists():
        meta["baseline_summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    (out_dir / f"{name}.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--runs", nargs="*", help="対象 run 名(省略時は source 直下すべて)")
    ap.add_argument("--force", action="store_true",
                    help="既存の抽出結果を上書きする(既定はスキップ=追記専用)")
    args = ap.parse_args()

    if not args.source.is_dir():
        print(f"source が見つからない: {args.source}", file=sys.stderr)
        return 1

    candidates = ([args.source / r for r in args.runs] if args.runs
                  else sorted(d for d in args.source.iterdir() if d.is_dir()))
    n_new = n_skip = 0
    for run_dir in candidates:
        meta_out = args.out / f"{run_dir.name}.meta.json"
        if meta_out.exists() and not args.force:
            n_skip += 1
            continue
        meta = extract_run(run_dir, args.out)
        if meta:
            n_new += 1
            print(f"extracted: {meta['run']} ({meta['n_iterations']} iterations)")
    print(f"done: {n_new} extracted, {n_skip} skipped (already present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
