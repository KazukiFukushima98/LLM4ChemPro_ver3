# run 間ブラインドの隔離プロトコル（2026-09-09 策定）

SST の各 run は「この手法が自力で何を発見できるか」の独立試行であり、run 同士は互いの
結果を知ってはならない（run-blind 原則、ユーザ決定 2026-07-09）。本書は、その原則を
**どう保証するか**を 2 段階で定義する。

| 呼称 | 内容 | 適用 run |
|---|---|---|
| **指示による隔離**（instruction-only isolation） | 指示書に「他 run を読むな」と書く。遵守はエージェント任せ | Run 1（run30）、Run 4（run35）。Run 2（run33）・Run 3（run34）はこの下で汚染が判明し破棄 |
| **物理的隔離**（physical isolation） | 他 run の情報が環境に存在しない状態で回し、事後に監査で証明する | 再実行の 2 本（rb01・rb02） |

「知識ポリシー v1/v2」（指示書の中身＝手法）とは別の軸である。再実行は **知識ポリシー v1 のまま、隔離だけを物理的隔離に上げる**。手法は 1 文字も変えない。本書は論文 §2.1 と SI の元原稿を兼ねる。

---

## 1. 指示による隔離が破れた原因

run-blind 原則（`algorithm/CLAUDE.md`「守ること」冒頭）は **「他 run のファイルを読まない」**
という読み取り禁止として書かれていた。エージェントは実際にこれを守った（run34 は
「run30 は使えない、自分はブラインドだ」と繰り返し明言し、他 run のディレクトリを一度も
開いていない）。

破れたのは **読み取りではなく、セッションをまたぐ永続メモリ** である。
`~/.claude/projects/<project>/memory/MEMORY.md` の索引行が毎セッション自動で
文脈に入り、そこに

    - [run30 完走・論文フェーズへ] — 新仕様 67.15$/t・4膜で収束。論文の主結果

が含まれていた。エージェントは**何も読まずに最初から知っていた**。
run34 の thinking はこれを自分で名指ししている
（`my memory notes say run30 hit feasibility at 67.15$/t with four membranes using a "new spec"`。
"new spec" は「新仕様」の直訳）。

脅威モデルに「文脈に自動注入される永続状態」が入っていなかった、というのが一次的な失敗である。
**LLM エージェントに run-blind を敷くとき、永続メモリが盲点になる**——これは報告する価値のある知見である。

## 2. 漏洩経路の棚卸し（実測）

| # | 経路 | 実在の証拠 | 物理的隔離での手当て |
|---|---|---|---|
| 1 | 永続メモリの索引・本文 | run33/run34 の thinking（§5） | メモリディレクトリを空にして起動（§3-A） |
| 2 | `git log` のコミットメッセージ | `81028d3 docs: run30 完走を記録（…67.15$/t…）` | 履歴のないワークスペースで実行（§3-B） |
| 3 | `ls runs/` の兄弟 run 名 | run34 の 22 行目に run25〜run30 が出力 | runs/ に当該 run のみ（§3-B） |
| 4 | `../docs/` の実験記録・図データ | `experiment_log.md`, `run30_time_breakdown.json` 等 | ワークスペースから除外（§3-B） |
| 5 | セッションの再開・使い回し | （今回は該当なし） | 1 run = 1 新規セッション（§3-C） |

指示による隔離にも「終了 run はルート `runs/` へ移す」という物理的な条項が 1 つあったが、
実行されていなかった（run33/34 の実行時、run30 は `algorithm/runs/` に同居していた）。
物理的隔離が「移動」でなく「クリーンツリーへのコピー」なのは、忘れうる手順を無くすためである。

## 3. 手順（`tools/setup_blind_run.ps1` が A・B・D を自動化する）

### A. メモリの隔離（実行機で、run 開始前）

**仕組み（公式ドキュメントで確認、2026-09-09）**: Claude Code の auto memory は
`~/.claude/projects/<project>/memory/` に置かれ、`<project>` は **cwd ではなく git リポジトリのルート**から
決まる。同じリポジトリのサブディレクトリ・worktree はすべて 1 つの memory を共有する。
索引 `MEMORY.md` の先頭 200 行は**毎セッション自動で文脈に入る**（本文ファイルは必要時に読む）。

これで汚染経路が完全に説明できる: セッションの cwd は `…\ver3\algorithm` だったが、git ルートは
`…\ver3` なので、`…-ver3\memory\MEMORY.md` が読み込まれた。指示による隔離の「他 run のファイルを
読むな」は、エージェントが自分で読むファイルにしか効かない。

手当ては 3 層（`setup_blind_run.ps1` が自動化）:

1. **ワークスペースを独立した git リポジトリにする**（§3-B の `git init`）。これで memory の `<project>` が
   新しくなり、空の memory から始まる。これが本質的な手当て。
2. **auto memory を設定で OFF にする**: `~/.claude/settings.json` に `"autoMemoryEnabled": false`
   （デスクトップアプリからの起動にも効く。環境変数 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` は
   ターミナル起動にしか効かないので使わない）。
3. **既存の memory をすべて退避**: `~/.claude/projects/*/memory` → `memory_HOLD_<日付>`。
   1 と 2 が正しく働けば不要だが、検証可能な状態にするための保険。2 本が終わるまで戻さない。

**部分削除はしない。** 結果を含まないつもりの記憶（例 `pressurized-campaign-restart.md`）にも
`52.12 $/t` `61.29 $/t` `73.66 $/t` `2膜/3膜/4膜/5膜` が入っている。全消しが唯一検証可能な状態。

### B. ワークスペースの物理隔離

再実行専用のクリーンなツリーを作る。

    workspace_blind_<run>/
      algorithm/          … src/, tests/, .claude/, case.yaml, ss_seed.json, CLAUDE.md
      ARCHITECTURE.md
      CLAUDE.md           … ルート（開発者向け）
      pyproject.toml, uv.lock
      algorithm/runs/     … 空。エージェントが当該 run 1 本だけを作る

- **`.git` を含めない**（経路 2）。`run_iteration.py` の自動コミットは、この run 専用の新規 `git init`
  （コミット 1 つ）に対して行う。
- **`docs/` を含めない**（経路 4）。
- **他の run・baseline を含めない**（経路 3）。
- `algorithm/baseline/` は SST ループが import しておらず（確認済み）、`run_baseline.py` の
  「SST run30 の 6.1 h に合わせた」が corpus 内で唯一 run30 の実測値に触れる箇所なので持ち込まない。
  `tests/test_baseline.py` も同じ理由で外す。除くだけなので情報は減る方向にしか変わらない。

### C. セッション

- **1 run = 1 新規セッション。** `--continue` / `--resume` を使わない。2 本を同一セッションで連続実行しない。
- 起動は `claude --model claude-opus-4-8`、起動後に auto mode（Shift+Tab）。auto mode でないと複合シェルコマンドの
  権限確認で止まる（rb01 の 1 回目）。
- **エージェントが読む corpus = `algorithm/` の中だけ**（指示書 `algorithm/CLAUDE.md`・`ss_seed.json`・`case.yaml`・`src/`・
  `.claude/`）。`ARCHITECTURE.md` とルート `CLAUDE.md` はワークスペースに置かない（rb01 は読まず、rb02 は読めず、rb03 は読んだが、
  3 本とも同じ構造に到達した。指示書は自己完結）。`algorithm/` の外は `blockReadsOutsideWorkingDirectories` でブロック。
  corpus からは過去 run・日付・commit・「GA」呼称を除去済み（commit d048e4b 以降）。
- モデルは **Opus 4.8** を明示指定し、起動直後に `/model` で表示された ID を `runs/<run>/MODEL.txt` に記録する
  （run30 と同条件であることの一次データ）。
- 実行中は人間が話しかけない（会話経由の汚染を作らない）。

### D. run 名

`run36` のような連番は「以前に 35 本あった」という情報そのもの。不透明名（`rb01` `rb02`）を使い、
論文側の対応表は人間が別管理する。

### E. corpus は凍結する（書き換えない）

`src/` `tests/` `case.yaml` `ARCHITECTURE.md` `algorithm/CLAUDE.md` のコメントには run2〜run28 への
言及がある（旧 feed 値の由来、獲得関数の経緯、評価回数の実測など）。これらは **run30 が使った版と
1 バイトも違わず**（`git diff a463079 HEAD -- algorithm/CLAUDE.md` は空）、Run 1 と Run 4 が読んだものと
同一である。書き換えれば清潔になるどころか比較可能性が壊れる。よって corpus は凍結し、
`setup_blind_run.ps1` は「repo と byte 一致」を検査する。実際に漏れた 4 経路（§2）はすべて corpus の**外**にあった。

## 4. 事後監査（必須・採用ゲート）

    uv run python tools/audit_blind.py <transcript.jsonl> --own-run rb01 --corpus-dir <workspace>

を各 run の完了後に実行する。`--corpus-dir` にワークスペースを渡すと、corpus が正当に言及する
run 番号（run2〜run28）が自動的に除外され、それ以外の run への言及（campaign の run30/32/33/34/35 や、
消したはずのメモリ由来のもの）だけが残る。**`--corpus-dir` に repo 全体を渡してはならない**
（`docs/` の run 番号まで正当扱いになり、監査が骨抜きになる）。

判定は 3 段階:

- **CRITICAL**: エージェント自身の出力（thinking / text / tool_use）に他 run の識別子、またはそれと同居する
  コスト値が現れた。独立性が破れている。
- **WARN**: tool_result（環境が見せたもの）にだけ現れた。使ったとは限らないが経路が開いていた。
- **PASS**: どちらもなし。

**採用ゲート = PASS（エージェント出力 0 件）。** PASS しなければ破棄して条件を直し、やり直す。
監査出力は `runs/<run>/AUDIT.txt` として保存し、SI で公開する。

**注記（Claude Code v2.1.266 以降）**: transcript に thinking の中身が記録されなくなった（ブロックはあるが 0 文字）。
7 月の汚染は thinking に現れたが、物理的隔離の下の run ではそこを見られない。監査が見ているのは
(i) エージェントが環境から受け取ったすべて（tool_result）と (ii) 書いたもの（text / tool_use）である。
したがって保証の論理は「汚染の形跡がない」ではなく、**「他 run の情報が届きうる経路が、記録されている（tool_result 0 件）か
物理的に存在しない（memory 不在・settings OFF・クリーンツリー・Read の外部ブロック）かのどちらかである」**。
実績: rbtest / rb01 / rb02 とも PASS（0 / 0）。

既存 4 run での較正（同じ基準・同じ corpus 除外で実行。記録は `docs/audit_blind_existing_runs.txt`）:

| run | エージェント出力 | 判定 | 内容 |
|---|---|---|---|
| Run 1 (run30) | 0 | PASS | — |
| Run 2 (run33) | 51 | CRITICAL | run30 の 4 膜 feasible を提案根拠に使用 |
| Run 3 (run34) | 21（うち 16 が「記憶」言い回し付き） | CRITICAL | 停止判断に使用。メモリ経路を自動検出 |
| Run 4 (run35) | 1 | CRITICAL | run31 の知識ポリシー版数への言及のみ（結果値なし）。メモリ由来なので物理的隔離では消える |

Run 4 の 1 件は人が判定して清潔と扱う（結果でも構造でもない）。

## 5. 指示による隔離の下の 4 run の判定（既存）

| run | 論文での呼称 | セッション | 判定 |
|---|---|---|---|
| run30 | Run 1 | cf5a3f3b | clean（run24-26 の feed 組成のみ＝ケース事実） |
| run33 | Run 2 | f0556aff | **汚染**（iter7 で run30 の 4 膜 feasible を提案根拠に使用） |
| run34 | Run 3 | 6446c9b3 | **汚染**（停止判断に使用。構造提案には未使用） |
| run35 | Run 4 | 5ec47779 | clean（run31 の知識ポリシー版数のみ） |

汚染 2 本は破棄し、物理的隔離の下で独立標本を 2 本追加する（Run 2/3 の「やり直し」ではなく新標本）。
Run 1 と Run 4 は指示による隔離のまま据え置くため、**4 run は同一隔離条件ではない**
（物理的隔離は情報を減らす方向にのみ違う）。この非対称は論文に明記する。
