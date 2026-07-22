"""論文・スライド共用の作図関数。

規約(docs/design-brief.md 要件2):
- 引数は DataFrame、戻り値は matplotlib Figure
- 関数内でファイル保存はしない(保存は呼び出し側 = .qmd / export_figures.py の責務)
- 論文とスライドは必ず同じ関数を呼ぶ(図の食い違いを構造的に防ぐ)
"""

from paperfigs.figures import fig_cost_convergence, fig_method_comparison

__all__ = ["fig_cost_convergence", "fig_method_comparison"]
