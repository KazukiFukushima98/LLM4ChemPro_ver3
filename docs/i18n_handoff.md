# 引き継ぎ: algorithm/ の英訳（国際誌投稿向け）

作成 2026-07-16 / ブランチ `i18n/english-algorithm` / コミット `30319cc`

このセッションで `algorithm/` を英語化した。**コードは1行も変更していない**（＝挙動不変）。
本ドキュメントは次セッションへの引き継ぎであり、~~**未了の検証が2件ある**~~
→ **2026-07-16 追記: 残課題2件（実機 smoke・SST 挙動確認 run）は完了した（§2 参照）。英訳の検証はすべて閉じた。**

---

## 1. やったこと

`algorithm/` 配下の日本語 2,128 行のうち **2,127 行を英訳**した。訳した対象は次のみ:

- コメント / docstring
- 人間向けメッセージ（`print` / `raise` / `assert` の msg / `argparse` の `help=`）
- matplotlib の図中ラベル（投稿図に載るため）
- `case.yaml` のコメント

**訳していないもの**（＝データであってプロセではない）:
辞書キー、比較値、Aspen のブロック名/ストリーム名/COM パス、argparse のフラグ文字列、
正規表現、JSON フィールド名、ワーカのプロトコル文字列。

対象外（当初合意）: `algorithm/runs/`（gitignore 済みの実験データ。日本語が大量に残っているが公開物に含まれない）、`src/YAspen/`。

### コミット状況

| 対象 | 状態 |
|---|---|
| 追跡ファイル 33 件（`src/` `tests/` `baseline/` `case.yaml` `CLAUDE.md` `.claude/commands/`） | `30319cc` にコミット済み（+2421 / -2072） |
| `algorithm/scratch/` 12 件 | **コミット不可**。`.gitignore:5` で除外されており git 管理外。ディスク上にのみ存在する |

`scratch/` が gitignored ＝ **公開物には含まれない**。訳したが、差分も巻き戻しも git では取れない。
翻訳前の版は `algorithm/scratch/i18n_orig_before/` に手動バックアップしてある（2ファイルのみ）。

> **注意**: `docs/run26_best_structure.json` / `.png` の未コミット変更は**このセッションのものではない**。
> セッション開始時点で既に modified だった。触っていないので、そのまま扱ってよい。

---

## 2. 検証の状態

### 担保できていること

1. **AST 比較（全 `.py`）** — docstring を除去し文字列定数をプレースホルダ化した `ast.dump` が
   翻訳前と**完全一致**。制御フロー・変数名・数値・辞書キー・演算子が1つも変わっていないことの機械的証明。
2. **`case.yaml`** — `yaml.safe_load` の結果が翻訳前と**完全一致**（コメントのみ変更）。
3. **Aspen 非依存テスト** — **267 件パス（OK, skipped=1）**、実 exit code 0 で確認。
   （`| tail` を挟むと exit code が tail のものになり常に 0 になる。判定に使わないこと。）

### 担保できていないこと ＝ 次セッションの残課題 → **両方完了（2026-07-16 検証済み）**

| # | 課題 | 結果 |
|---|---|---|
| A | **Aspen 実機 smoke** | **完了・全項目 PASS**。`uv run python scratch/smoke_ver3.py` を実機で1回実行（exit 0）。[1] Lee スケール feed 収束 / [2] permeance 書き込み反映 / [3] VP1・VP2 両方の WNET>0 / [5] 再現性（E 差 <1%）すべて PASS。[4] Mixer PRES は全 MIXV で 0.0（最小圧追従）を確認。 |
| B | **SST エージェントの挙動確認 run** | **完了・挙動正常**。`runs/run27` を新規作成し、dry 設定（`--bo-n-init 4 --bo-n-iter 3 --bo-q-batch 2 --no-commit`）で iter_001（ベースライン）→ SST 提案 → apply_ss → iter_002 の1サイクルを実施。英語プロンプト（英訳後 `CLAUDE.md` / `sst-loop.md`）下で、シグナル抽出（bounds 張り付き・residue CO2 損失 35.3%）→ 定量根拠つき `ss_change.json`（gated COMP3+MEMB3 の第3段追加、V6 の residue⇄recycle トグルペア、binary 4 ≤ 8）→ apply_ss 一発通過（validate OK、頂点採番 V13–V16 予測どおり）→ iter_002 で BO が候補を実際にトグル（q_1=1/q_2=0 で第3段 ON、相互排他も正常）まで従来同様に動作した。iter_002: purity 66.0→87.1%（第3段が機能）。recovery 20.2% は縮小バジェット（12評価）のノイズ域であり、検証目的（提案挙動の確認）には影響しない。 |

（run27 は検証専用の dry run。`--no-commit` のため git 履歴には残らない。）

---

## 3. 意図的に残した / 変更した例外（3件）

### (a) `src/aspen_watchdog.py:76` — 日本語のまま残す【重要】

```python
button_texts = {"OK", "はい", "Yes", "Close", "閉じる", "終了"}
```

**訳してはいけない。** これは散文ではなく、日本語ロケール Windows がダイアログのボタンに描く
キャプションそのもの。`win32gui.GetWindowText()` の戻り値とこの集合を文字列一致させて
クラッシュダイアログを閉じている。英訳すると一致しなくなり**ウォッチドッグが機能しなくなる**。
バイト単位で不変とし、上に「訳すな」という英語コメントを付けてある。
`algorithm/` に残る日本語はこの1行のみ。

### (b) `tests/test_apply_ss.py:696,707` — 唯一のデータリテラル変更

`assertRaisesRegex(ApplyError, "固定値")` → `"fixed value"`。
`src/apply_ss.py` が持つ**エラーメッセージの部分文字列**を照合していたため、src 側の英訳と
**同時に変えないとテストが落ちる**。テストの意図（固定パラメータのガードが発火する）は不変。

> **これが今回いちばん重要な発見。** ファイルをまたぐ結合なので AST 比較では検出不能で、
> テストを実際に走らせて初めて分かった。同種の結合を全走査した結果、この2箇所のみだった。
> 今後 src のメッセージを変えるときは、同じ走査を回すこと（下記 §5）。

### (c) `tests/test_cost.py` — `_DESIGNS` ラベル

`"Fig3a 2段 no-recycle"` → `"Fig3a 2-stage no-recycle"`。
`name` は 189 行で unpack され、195/196 行の assert メッセージの f-string でのみ使用。
キーにも比較にも使われていないことを確認済みの表示専用文字列。

---

## 4. 未修正のまま残した既存バグ（4件）

今回の趣旨は「挙動を変えない」ことなので**すべて未修正**。別タスクとして扱うか要判断。
コミット `30319cc` のメッセージにも記録してある。

| 箇所 | 内容 | 深刻度 |
|---|---|---|
| `src/simulator.py` `_build` | 成功時は3-tuple `(aspen, energy_blocks, coolers)`、失敗時は2-tuple `None, None` を返す。呼び出し側は3値展開しているので、**ビルド失敗時に `Metrics.bad()` へ落ちず `ValueError` になる**＝意図したクラッシュ回復経路が働かない | 高 |
| `src/ga.py` `_evaluate_population` | `topology` が `if reason is not None` の else 側でしか束縛されない。**最初のグループが `is_buildable` で弾かれると `NameError`**、以降のグループでは前のグループの topology を参照しうる | 高 |
| `src/run_iteration.py` `evaluate_detailed_with_retry` | 試行回数がドキュメント記載と off-by-one。既定 `retries=2` では実害なし | 低 |
| `src/evaluator.py` `ECONOMICS_DEFAULTS` | コメントが「HX コストは省略（2026-07-10 ユーザ決定）」と述べるが、2026-07-16 の改定で `hx_cost` は実際に加算されている。**英訳した結果、矛盾が英文としてそのまま残っている**ので投稿前に要判断 | 低（ただし投稿物） |

その他の軽微な指摘（未使用 import、`repro_lee_fig3.py:139` の `w_ours == 0.0` で比が `-` になる、
`make_run26_best_figure.py` の `active4=False` 時に未描画ブロックへ矢印が伸びる 等）は
`scratch/` 中心で実害が薄いため放置。

---

## 5. 再検証の方法（ツールを残してある）

翻訳の「挙動不変」を機械的に検証したツールを **`algorithm/scratch/i18n_astcheck.py`** に置いた
（`scratch/` は gitignored なのでディスク上のみ。消さないこと）。

```bash
cd C:/Users/inukai/PycharmProjects/LLM4ChemPro_ver3
uv run python algorithm/scratch/i18n_astcheck.py algorithm/src/foo.py ...
```

- 各ファイルを **git HEAD 版と比較**し、①AST 構造が一致するか ②変わった文字列リテラルが
  メッセージ位置（print/raise/assert msg/help=）に居るか を判定する。
  データ位置（辞書キー・比較値・添字・その他 kwarg）のリテラルが変わっていたら **FAIL** する。
- **今は HEAD＝翻訳後なので、翻訳自体を再検証したいなら比較先を `681fa27`（翻訳前）に変える**こと
  （`head_version()` の `HEAD:` を書き換える）。
- ゲートが正しく落ちることは negative control で確認済み（辞書キー変更・watchdog キャプション
  英訳・実際のコード変更の3種すべてで FAIL する）。
- **限界**: ファイルをまたぐ結合（§3(b)）は検出できない。**必ずテストも走らせること。**

ファイルまたぎの結合の全走査（日本語メッセージに依存した assert を探す）:

```bash
grep -rn "assertRaisesRegex\|assertRegex\|assertIn" algorithm/tests/
# → 日本語を含むパターンが出たら、それは src のメッセージへの結合
```

### 全体の再検証コマンド

```bash
# テスト（~12分。BO のテストが重い。実 exit code を見ること）
cd algorithm && uv run python -m unittest discover -s tests > /tmp/suite.txt 2>&1; echo $?

# 残存日本語の確認（期待値: aspen_watchdog.py:76 の1行のみ）
```

---

## 6. スコープ外だが判断が要る点

`algorithm/CLAUDE.md`（英訳済み）は本文中でリポジトリルートの **`ARCHITECTURE.md`（日本語のまま）**と
ルートの `CLAUDE.md`（日本語のまま）を参照している。いま `algorithm/` だけ英語なので、
**英語のプロンプトが日本語ドキュメントを指す**状態。公開時に `algorithm/` のみを出す方針
（`memory: publication-deliverable`）なら、参照先を切り離すか `ARCHITECTURE.md` の必要部分も
英訳するかの判断が要る。今回は `algorithm/` 内に閉じ、参照はそのまま残した。
