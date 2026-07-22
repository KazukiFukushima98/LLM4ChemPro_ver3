"""results/runs/ の run 単位 CSV を結合し、原稿が読む集約ファイルを作る。

出力(results/aggregated/、git 追跡対象):

    iterations.parquet   全 run の反復サマリを縦結合したもの
    progress.parquet     全 run の内側最適化時系列を縦結合したもの
    runs.csv             run 1つにつき1行の要約(人間と git diff が読む用)
    meta.json            集約の再現情報

原稿(.qmd)はこのディレクトリだけを参照する。run が増えたら
extract_runs.py → aggregate.py の順に再実行すれば原稿側は無変更で更新される。

使い方:
    uv run python scripts/aggregate.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "results" / "runs"
OUT_DIR = REPO_ROOT / "results" / "aggregated"

# 実行可能性の判定に使う制約(case.yaml の optimization_targets と揃える)
PURITY_MIN = 0.95
RECOVERY_MIN = 0.90


def concat_csvs(pattern: str) -> pd.DataFrame:
    files = sorted(RUNS_DIR.glob(pattern))
    if not files:
        raise SystemExit(f"入力がない: {RUNS_DIR}/{pattern} — 先に extract_runs.py を実行する")
    return pd.concat([pd.read_csv(f) for f in files], ignore_index=True)


def summarize_runs(iterations: pd.DataFrame) -> pd.DataFrame:
    """run 1つにつき1行:feasible best と探索規模。"""
    rows = []
    for run, g in iterations.groupby("run", sort=True):
        feas = g[(g["CO2_purity"] >= PURITY_MIN) & (g["CO2_recovery"] >= RECOVERY_MIN)]
        best = feas.loc[feas["cost_usd_per_tCO2"].idxmin()] if not feas.empty else None
        rows.append({
            "run": run,
            "kind": g["kind"].iloc[0],
            "optimizer": g["optimizer"].iloc[0],
            "n_iterations": len(g),
            "total_evaluations": g["n_evaluations"].sum(),
            "feasible": best is not None,
            "best_iteration": None if best is None else best["iteration"],
            "best_cost_usd_per_tCO2": None if best is None else best["cost_usd_per_tCO2"],
            "best_CO2_purity": None if best is None else best["CO2_purity"],
            "best_CO2_recovery": None if best is None else best["CO2_recovery"],
            "best_specific_energy_kWh_tCO2": None if best is None
                                             else best["specific_energy_kWh_tCO2"],
        })
    return pd.DataFrame(rows)


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    iterations = concat_csvs("*_iterations.csv")
    progress = concat_csvs("*_progress.csv")
    runs = summarize_runs(iterations)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    iterations.to_parquet(OUT_DIR / "iterations.parquet", index=False)
    progress.to_parquet(OUT_DIR / "progress.parquet", index=False)
    runs.to_csv(OUT_DIR / "runs.csv", index=False)

    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "git_commit": git_commit(),
        "n_runs": len(runs),
        "n_iterations": len(iterations),
        "constraints": {"purity_min": PURITY_MIN, "recovery_min": RECOVERY_MIN},
        "inputs": sorted(p.name for p in RUNS_DIR.glob("*.csv")),
    }
    (OUT_DIR / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"aggregated: {len(runs)} runs / {len(iterations)} iterations -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
