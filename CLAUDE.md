# CLAUDE.md

このファイルは**開発モード**の指示書である。あなた（Claude Code）はこのプロジェクト（**ver3**）のコードを書き・修正する開発者として振る舞う。ver3 の新規スコープ（膜モデル・コスト目的関数・bounds・圧力アーキテクチャ）は `ARCHITECTURE.md` **12節**に定義されている。

> 結果を読んで新しい SS を提案し、外側ループを回す「SST エージェント」の役割は `algorithm/CLAUDE.md` に分離されている。混同しないこと。

---

## 最初に必ず読むもの

**実装・修正に着手する前に、必ず `ARCHITECTURE.md` を最初から最後まで読むこと。**
設計判断（トポロジー表現、二層最適化、評価境界、ファイル役割、実装順序、Aspen制約、旧コードの流用方針）はすべてそこにある。

設計に迷ったら推測せず `ARCHITECTURE.md` に従う。記載のない判断が必要なら、**勝手に決めず人間に確認**する。

---

## 旧リポジトリ（参照専用）

いずれも**書き込み禁止**（編集・作成・削除しない）。

- **ver2**: `C:\Users\inukai\PycharmProjects\LLM4ChemPro_ver2` — 完成・凍結（タグ `v2.0-complete`）。
  ver3 のコードはここの追跡ツリーを継承した。過去 run（run5〜23）・実験記録（`docs/experiment_log.md`）・
  git 履歴の正本。経緯や過去の実験結果を調べるときはここを読む。
- **ver1**: `C:\Users\inukai\PycharmProjects\LLM4ChemPro` — 最初の移植元（歴史的参照のみ）。

- Aspen COM 周り（`aspen_builder.py` / `simulator.py`）は動作実績のある資産。ロジックは書き直さず、
  インターフェース・堅牢性レベルの調整のみ（ver3 の Mixer 圧力規則の変更は ARCHITECTURE 12.4 の
  手順＝実機 smoke 必須に従う）。

---

## 進め方のルール

1. **フェーズごとに進める。** `ARCHITECTURE.md` 第9節の順序（① topology → ⑦）に従う。一度に複数フェーズを実装しない。指示されたファイル以外を勝手に作らない。
2. **各フェーズはテストが通ってから次へ。** `topology.py`（フェーズ1）と `apply_ss.py`（フェーズ5）は Aspen 不要で単体テストできる。ここを最優先で固める。
3. **「○○を実装して、できたら止まって」と区切られたら、その範囲だけ実装して報告する。** 先回りしない。
4. **旧コードはコピーせず移植する。** 着手前に移植マップを確認し、旧リポジトリの該当ファイルを読んでから書く。

---

## 守るべき設計上の制約（やってはいけないこと）

- **`ss_seed.json` / `case.yaml` を書き換えない。** 人間が与えるデータ・設定である。依存追加が必要なら `pyproject.toml` を勝手に変えず人間に提案する。
- **トポロジーを隣接行列として実装しない。** 実装は一貫してアーク辞書（安定文字列IDキーの辞書操作）。行列形式は論文用で、必要なら `to_matrix` で生成する。
- **頂点IDを削除時に再付番しない。** 欠番のままにし、再利用しない。
- **依存方向を破らない。** `topology.py` / `ga.py` / 外側ロジックから `aspen_builder.py` / `simulator.py` を直接 import しない。評価は必ず `evaluator.py` の境界を経由する。
- **`orchestrate.py` を作らない。** 外側ループの司令塔は Claude（SST エージェント）であり、Python スクリプトではない。
- **Aspen COM のロジックを「改善」と称して書き直さない。** ロジックは移植、インターフェースのみ調整。
- **Aspen の命名規則を守る。** ブロック名・ストリーム名にアンダースコアを使わない（`V{i}`, `MEMB{n}`, `VP{n}`, `VS{i}T{j}`, `DV{i}`）。
- **`YAspen/` を再生成しない。** 旧 repo からそのままコピーする資産。
- **`runs/` にコードを置かない。** 結果データ専用。

---

## コーディング規約

- Python 3.12+。パッケージ管理は **uv**（`uv run python ...`、`uv add ...`）。
- 型ヒントを付ける。特に `evaluator.py` の境界は Protocol で明示する。
- 1ファイル1責務。`ARCHITECTURE.md` 第8節のファイル役割を超える機能を詰め込まない。
- Aspen 非依存のロジック（topology / ga の符号化 / signals / apply_ss）は Aspen 抜きでテスト可能に保つ。
- スクリプトは `--base-dir` で run ディレクトリを受け取り、`runs/` の外出しを可能にする。

---

## テスト

- `tests/` に置く。最優先は `tests/test_topology.py`、次に `tests/test_apply_ss.py`。
- Aspen を要するテストと要さないテストを分け、日常の確認は Aspen 不要分だけで回せるようにする。
- 検証すべき決定論的ロジック：`active_topology`（q→具体トポロジー）、`validate`（連結性・孤立頂点）、補助ユニット導出、`load_ss`/`save_ss` の往復、`apply_ss`（提案→SS更新）、ユニット削除（流入の residue 行き・出力削除・採番据え置き）。

---

## 論文・スライドのビルド層(2026-07 追加)

設計方針は `docs/design-brief.md`。計算(Windows 機の Aspen)と原稿ビルド(Mac)は完全分離。

```
algorithm/runs/            生 run データ(gitignore・計算ノード上が正本)
    ↓ scripts/extract_runs.py   (標準ライブラリのみ・追記専用・既存runはスキップ)
results/runs/              run単位の軽量CSV + meta.json(git追跡。上書き禁止)
    ↓ scripts/aggregate.py
results/aggregated/        iterations.parquet / progress.parquet / runs.csv(原稿が読む唯一の場所)
    ↓
paper/paper.qmd            論文(マスター本文は Overleaf。図表・数値の生成がここの役割)
slides/*.qmd               スライド(revealjs・単一HTML)
src/paperfigs/             共用作図関数(DataFrame in → Figure out。ファイル保存禁止)
```

守るべき規約:

- **図は必ず `src/paperfigs` の関数で描く。** `.qmd` 内に matplotlib を直書きしない
  (論文とスライドの図が食い違う事故を構造的に防ぐため)
- **`results/runs/` の既存ファイルを書き換えない。** 追記専用。再抽出は `--force` を明示
- **原稿は `results/aggregated/` だけを読む**(`paperfigs.data` 経由)。個別 run のファイルを直接読まない
- ビルドは `make`(extract / aggregate / paper / slides / figures / freeze)。
  依存解決済みなので必要な工程しか走らない
- 投稿用の図は `scripts/export_figures.py` で所定名(fig1.pdf…)出力。チャンク生成名は使わない
- 投稿時は `make freeze TAG=<tag>` → `SST_DATA_DIR=results/frozen/<tag>` でレンダリング → `git tag`
- Mac では `uv sync --group docs`(ソルバー系 pywin32/torch はマーカーで自動除外)。
  計算ノード(Windows)は従来どおり `uv sync`
- Overleaf とはラウンド制(design-brief 6節)。レビュー期間中に .tex を送らない

---

## 困ったときの優先順位

1. `ARCHITECTURE.md` に答えがあるか確認する。
2. なければ旧リポジトリの該当コードを読む（移植元として）。
3. それでも設計判断が必要なら、勝手に決めず人間に質問する。

推測で設計を埋めて先に進むより、止まって確認する方が望ましい。
