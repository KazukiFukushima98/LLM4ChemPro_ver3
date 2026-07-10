---
description: SST 外側ループを自律的に回す（analyze → apply_ss → run_iteration を停止条件まで反復）
argument-hint: <run dir, 例: runs/run24> [最大反復数] [--dry]
---
あなたは SST エージェント（`CLAUDE.md`＝この algorithm ディレクトリの指示書）として振る舞い、外側ループを自律的に回す。**各反復でユーザに確認は取らない**（`CLAUDE.md` の方針）。
**セッションは `algorithm/` で開かれている前提。コマンドはすべてここから実行する**（スクリプトは `src/...`、run データは `runs/`＝gitignore 済み）。

**引数の解釈**：`$ARGUMENTS`（渡された引数の全文）を空白区切りで読む。`$1`/`$2` の位置指定は 0/1 始まりが環境で揺れるため使わない——必ず `$ARGUMENTS` を自分で分解する。
- 1個目のトークン＝**対象 run ディレクトリ**（例 `runs/run24`）。以降 **RUN** と呼ぶ。空ならどの run か尋ねて止まる。
- 2個目の整数トークン（あれば）＝**最大反復数**。以降 **N** と呼ぶ。無ければ停止条件まで回す。
- `--dry` が含まれていれば検証モード。

`--dry`：検証モード。step 4 に縮小フラグ＋`--no-commit` を付けて実行し（小規模・`case.yaml` 不変・コミットなし）、停止時の記録もスキップする。**縮小フラグは `case.yaml` の `optimizer` を見て選ぶ**——`bo` なら `--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2`、`ga`（または未指定）なら `--pop 4 --gen 3`。GA 用フラグは BO では読まれない（逆も同様。run_iteration が警告を出す）。本番は無指定。

**ハンズオフのための注意（無人運用で途中停止しないために）**。許可は「コマンドの形」で判定される（`.claude/settings.json`）ので、次を守る：
- results.json・ログは **Read ツールで読む**（`cat`/素の `python -c` でシェルに出さない）。計算が要るなら **`uv run python -c '…'`**（`cd` を使わず・`uv run` 一本）。
- **パイプ・`while`/`for` ループ・`$(…)`・変数展開を含む複合シェルを組まない**——この形は許可リストがあっても安全判定できず必ず確認ダイアログで止まる（無人運用が死ぬ）。複数ファイルを見たいときは Glob＋Read ツールを繰り返すか、`uv run python -c '…'` 1本にまとめる。
- ロールバックは **逆 `ss_change` を書いて `apply_ss`**、または `ss_before_change.json` の内容を Write ツールで `RUN/ss_current.json` に書き戻す（`runs/` への Write は素通し。同じ iter へ再 apply する場合は `--force`）。シェルの `copy`/`cp`/`mv` は使わない。
- auto-commit の確認は run_iteration の出力で行い、別途 `git status`/`git log` を叩かない。
- コマンドは **Bash ツール**で投げる（PowerShell ツールだと許可ルールが効かず全部が確認ダイアログになる）。
- `case.yaml` / `ss_seed.json` は deny 設定で書けない（書けないのは仕様。変更は人間に依頼する）。
- **RUN 以外の run ディレクトリを読まない**（run 間ブラインドの原則。`CLAUDE.md` 守ること参照）。ss_change の書式が知りたければ `CLAUDE.md`/`ARCHITECTURE.md` 6.2節の例を見る。

`CLAUDE.md` の「1反復の手順」を次の通り繰り返す：

0. **新規 run のブートストラップ**：`RUN/ss_current.json` が無ければ、`ss_seed.json` の内容をそのまま Write ツールで `RUN/ss_current.json` にコピーし、`uv run python -u src/run_iteration.py --base-dir RUN > RUN/iter_001_log.txt 2>&1` でベースライン（iter_001・ss_change なし）を取ってから 1 へ。
1. `RUN/iterations/` で `results.json` を持つ最大番号の `iter_MMM/` を最新とする（番号を M とする）。その `results.json` を読む。
2. `iter_MMM/ss_change.json` が**既に存在すれば**（手書き or `/sst-analyze` で用意済み）それを使う。**無ければ**シグナルを抽出して「削除＋追加のセット」の `ss_change.json` を `iter_MMM/` に書く（数値を `reason` に引用、リサイクルは戻す残渣の CO₂ を確認、変数上限＝`case.yaml` の `max_variables`/`max_binary_variables` を自己点検）。
3. `uv run python src/apply_ss.py --base-dir RUN --iter M`
4. `uv run python -u src/run_iteration.py --base-dir RUN > RUN/iter_(M+1)_log.txt 2>&1`（run_iteration が次番号を max+1 で自動採番）。**`--dry` 指定時は optimizer に応じた縮小フラグ＋`--no-commit` を付けて実行**（BO: `--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2`、GA: `--pop 4 --gen 3`）。
5. 生成された新しい `results.json` を読み、**簡潔に報告**：読んだシグナル（数値）／書いた ss_change と根拠／新 results の純度・回収率・比エネルギー。
6. **停止判定**：`CLAUDE.md` の停止条件（ユーザ停止指示／`performance` 基準で連続3反復改善なし〔制約未達中は未達量、充足後は比エネルギー。**best_fitness は判定に使わない**〕／Aspen クラッシュ×2連・収束失敗×3連）、または **N** 反復に達したら止める。いずれでもなければ 1 に戻る。

停止したら引き継ぎを **`RUN/HANDOFF.md`** に記録する（最終 performance・走った反復数・止めた理由・次にやりたいこと・注目シグナル）。`../docs/` への転記は開発セッション（リポジトリルート）が行うので書かなくてよい。run_iteration の auto-commit は任せる。失敗は記録の上で続行可否を判断（`CLAUDE.md` のロールバック判断）。

**`--dry` 検証では** step 6 の auto-commit 確認をスキップし、停止時の `HANDOFF.md` 記録も**行わない**（検証結果はチャットで報告する）。
