"""図の実体。データは results/aggregated/ のテーブルを前提とする。

各関数のデータ契約(列名)は docstring に明記する。
列を追加するのは自由だが、ここに書かれた列を欠かすと図が壊れる。
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.figure import Figure

# 列挙色は colorblind-safe (Okabe-Ito)
KIND_COLORS = {"sst": "#0072B2", "baseline": "#E69F00"}


def fig_cost_convergence(iterations: pd.DataFrame,
                         runs: list[str] | None = None) -> Figure:
    """SST run の反復ごとのコスト軌跡。

    引数 iterations: results/aggregated/iterations.parquet
        必須列: run, kind, iteration, cost_usd_per_tCO2, CO2_purity, CO2_recovery
    runs: 描く run 名のリスト(省略時は kind=="sst" の全 run)
    """
    df = iterations[iterations["kind"] == "sst"].copy()
    if runs is not None:
        df = df[df["run"].isin(runs)]

    fig, ax = plt.subplots(figsize=(6.0, 3.6))
    for run, g in df.groupby("run", sort=True):
        g = g.sort_values("iteration")
        feasible = (g["CO2_purity"] >= 0.95) & (g["CO2_recovery"] >= 0.90)
        ax.plot(g["iteration"], g["cost_usd_per_tCO2"],
                marker="o", ms=4, lw=1.2, label=run)
        ax.plot(g.loc[feasible, "iteration"],
                g.loc[feasible, "cost_usd_per_tCO2"],
                marker="o", ms=8, lw=0, mfc="none",
                mec=ax.lines[-1].get_color())
    ax.set_xlabel("SST iteration")
    ax.set_ylabel("Cost of CO$_2$ captured [\\$/t]")
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig


def fig_method_comparison(runs_summary: pd.DataFrame,
                          feasible_only: bool = True) -> Figure:
    """run ごとの feasible best コストの比較(SST vs baseline)。

    引数 runs_summary: results/aggregated/runs.csv
        必須列: run, kind, feasible, best_cost_usd_per_tCO2
    """
    df = runs_summary.copy()
    if feasible_only:
        df = df[df["feasible"]]
    df = df.sort_values("best_cost_usd_per_tCO2", ascending=False)

    fig, ax = plt.subplots(figsize=(6.0, 0.35 * len(df) + 1.2))
    colors = df["kind"].map(KIND_COLORS).fillna("#999999")
    ax.barh(df["run"], df["best_cost_usd_per_tCO2"], color=colors)
    for y, v in enumerate(df["best_cost_usd_per_tCO2"]):
        ax.annotate(f"{v:.1f}", (v, y), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8)
    ax.set_xlabel("Best feasible cost [\\$/tCO$_2$]")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in KIND_COLORS.values()]
    ax.legend(handles, ["SST (ours)", "One-shot baseline"],
              frameon=False, fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig
