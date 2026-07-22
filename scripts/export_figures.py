"""投稿用の図を所定のファイル名で出力する(design-brief 7節)。

.qmd のチャンクが生成する fig-xxx-1.pdf のような名前はジャーナル要件に
合わないため、投稿図は必ずこのスクリプトで書き出す。
図の実体は src/paperfigs(論文・スライドと同一関数)なので食い違わない。

使い方:
    uv run --group docs python scripts/export_figures.py
    SST_DATA_DIR=results/frozen/v1.0-submitted uv run --group docs python scripts/export_figures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from paperfigs import data, fig_cost_convergence, fig_method_comparison

OUT_DIR = Path(__file__).resolve().parent.parent / "paper" / "submission_figures"

# 掲載 run の選択は paper/paper.qmd の PAPER_SST_RUNS と揃えること
PAPER_SST_RUNS = ["run25", "run26", "run30"]


def main() -> int:
    iterations = data.load_iterations()
    runs = data.load_runs()

    figures = {
        "fig1.pdf": fig_cost_convergence(iterations, runs=PAPER_SST_RUNS),
        "fig2.pdf": fig_method_comparison(runs),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, fig in figures.items():
        path = OUT_DIR / name
        fig.savefig(path, bbox_inches="tight")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
