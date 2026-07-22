# 原稿・スライドのビルド(docs/design-brief.md)。重い計算はここには入れない。
# 計算(SST run)は Windows 計算ノードで実行し、runs/ の生データから
# extract で軽量 CSV を切り出して git にコミットする、という分業。
#
#   make          … extract → aggregate → paper + slides(必要な分だけ)
#   make extract  … 新規 run の CSV 抽出(追記専用・既存はスキップ)
#   make freeze TAG=v1.0-submitted … 投稿時点の集約データを凍結

PY        := uv run python
PYDOCS    := uv run --group docs python
QUARTO    := uv run --group docs quarto

RUNS_CSV  := $(wildcard results/runs/*.csv)
AGG       := results/aggregated/iterations.parquet
FIGLIB    := $(wildcard src/paperfigs/*.py)

.PHONY: all extract aggregate paper slides figures freeze clean

all: paper slides

# 生 runs/ から新規 run だけを抽出(標準ライブラリのみ・どのマシンでも動く)
extract:
	$(PY) scripts/extract_runs.py

$(AGG): $(RUNS_CSV) scripts/aggregate.py
	$(PYDOCS) scripts/aggregate.py

aggregate: $(AGG)

paper/paper.html: paper/paper.qmd $(AGG) $(FIGLIB)
	$(QUARTO) render paper/paper.qmd --to html

paper: paper/paper.html

slides/sst-progress.html: slides/sst-progress.qmd $(AGG) $(FIGLIB)
	$(QUARTO) render slides/sst-progress.qmd

slides: slides/sst-progress.html

# 投稿用: 所定名の図(paper/submission_figures/)と .tex(keep-tex)
figures: $(AGG) $(FIGLIB)
	$(PYDOCS) scripts/export_figures.py

# 投稿時点の集約データを凍結する。以後この原稿は
#   SST_DATA_DIR=results/frozen/$(TAG) make paper
# でレンダリングすれば凍結データに固定される。あわせて git tag $(TAG) を打つこと。
freeze:
	@test -n "$(TAG)" || { echo "usage: make freeze TAG=v1.0-submitted"; exit 1; }
	@test ! -d results/frozen/$(TAG) || { echo "results/frozen/$(TAG) は既に存在する(凍結は上書きしない)"; exit 1; }
	mkdir -p results/frozen/$(TAG)
	cp results/aggregated/* results/frozen/$(TAG)/
	@echo "frozen -> results/frozen/$(TAG)。次: git add + commit + git tag $(TAG)"

clean:
	rm -rf paper/paper.html paper/paper_files slides/sst-progress.html slides/sst-progress_files .quarto
