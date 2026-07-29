# COORDINATION.md — 作業引き継ぎ・未決事項（ver3）

直近の作業状態と未決確認事項を記録する。`ARCHITECTURE.md`（設計の真実）には書かない、
暫定／要確認のメモはここに置く。ver2 までの経緯は ver2 リポジトリの docs を参照（凍結・参照専用）。

---

## 直近の状態（2026-07-29 午後）— run35 完走・67.67 $/tCO2（最速 feasible 3.7h）。SST seed 収集完了（n=4）。次は baseline 延長（T_max=10h）

**run35 の結末**：best **67.67 $/tCO2**（iter_007、4膜=直列2段回収、purity 96.4 / recovery 90.1）。
10 反復・6.9h・初 feasible 3.7h（全 run 最速）、コスト3連続無改善の正規収束停止。
詳細と確定知見は experiment_log.md の run35 節。**アーカイブは SST セッション終了後**（未実施）。

**v1・Opus 4.8 標本の完成（n=4）**：
| run | best $/t | 初 feasible | 総時間 |
|---|---|---|---|
| run30 | 67.15 | 4.5h | 6.1h |
| run33 | 66.44 | 7.7h | 9.0h |
| run34 | 未達（recovery 87%） | — | 7.8h |
| run35 | 67.67 | 3.7h | 6.9h |

成功 3 / 未達 1。成功帯 66.44〜67.67（幅 1.8%）、3 本とも同型骨格（4膜・直列回収・V9 集約）。
run32（Opus 5、68.79）はモデル汚染により分布外・頑健性傍証のみ。

**ユーザ決定（2026-07-29）**：
1. **seed 収集完了、5本目は追加しない**（帯が固定済み・成功率精度は n=5 でも不変・必要時は後日追加可）
2. **T_max = 10h**（最長 SST 9.0h ＋マージン、GA 世代境界の食い込み吸収、「SST 有利の打ち切り」批判の封じ込め）

**次の作業**：
1. ~~run35 をルート `runs/` へアーカイブ~~（済 2026-07-29。runs/ = run24, 31〜35）
2. **baseline 延長**：wall-clock 10h（GA=世代境界、BO=バッチ境界＋ハードタイムアウト）で
   GA-Deb ×3 seed・2相 CBO ×3 seed（計 ~60h・2日強）
3. 論文図版（anytime 曲線は T_max=10h 軸で再構成）・内側/詳細評価乖離の調査（run33 で2回発生）

---

## 前段の状態（2026-07-29）— run34 は feasible 未達のまま正規収束停止（recovery 87%）。有効標本 = 成功2・未達1。次は run35

**run34 の結末**：feasible 未達。best = iter_005（purity 95.7 / recovery 87.0 / 74.39 $/t）、
7反復・約7.8h、shortfall 3連続無改善の正規停止。HANDOFF の「熱力学的天井 ~0.87」結論は
他 run（30/32/33 が同 campaign で 90%+ 達成）により誤りと判明 — 敗着は勝ち筋の直列2段回収
（MEMB5）を binary 希釈下で never-engaged のまま撤去した iter_005。**手法の成功率の正直な
標本として採用**（ユーザ承認済みの推奨。詳細と知見4件は experiment_log.md の run34 節）。
ルート `runs/` へアーカイブ済み。

**有効標本の現況（v1・Opus 4.8）**：run30 67.15（6.1h）/ run33 66.44（9.0h）/ run34 未達（7.8h）。
T_max は依然 run33 の ~9h。

**次の作業**：
1. **run35（SST seed、v1・Opus 4.8）** — 次に実行
2. run35 完了 → T_max 確定 → baseline 延長（GA-Deb ×3・CBO ×3）
3. 論文図版・残りの baseline seed

---

## 前段の状態（2026-07-28 夜）— run33（Opus 4.8）完走・66.44 $/tCO2（3標本の最良）。run32 はモデル汚染で分布除外、6.5h→T_max 方式へ。run34 開始

**run33 の結末**：best **66.44 $/tCO2**（iter_008、4膜=回収側直列ストリップ変種、
purity 95.4 / recovery 90.2）。11 反復・約 9.0h、コスト3連続無改善の正規収束停止。
初 feasible は 7.7h。詳細と確定知見は experiment_log.md の run33 節。ルート `runs/` へアーカイブ済み。

**ユーザ決定（2026-07-28、run33 完了後）**：
1. **run32 は Opus 5 実行のためモデル汚染 → 論文1の SST 標本分布から除外**（頑健性の傍証として
   補足利用は可）。有効標本 = run30・run33（いずれも Opus 4.8）。
2. **6.5h wall-clock プロトコルを廃止し T_max 方式へ**。T_max = 有効 SST 標本の最長 wall-clock
   （現状 run33 の ~9h）。baseline（GA-Deb ×3・2相 CBO ×3 を優先）を T_max まで回して全手法の
   anytime 曲線で比較、SST の自己停止点は曲線上のマーカー。
   → 「SST だけ停止基準が2つ（3反復無改善 or 6.5h）になっている」不公平の解消。
3. **実行順序：SST 残り seed（run34, 35、Opus 4.8）を先に完了 → T_max 確定 → baseline 延長を
   1回で実施**。既存 6.5h baseline は補助表に残す（BO 全滅・GA-penalty 全滅の傾向は 6.5h で十分）。

**開発側の調査候補（seed 収集完了後）**：run33 で**内側（topology モード）と詳細評価の乖離が2回**
（iter_002 退化解、iter_005 内側 feasible 66.0 → 詳細 recovery 63.4%）。リサイクル絡みの収束モード差の
疑い。run30/32 には無かった規模。

**次の作業**：
1. **run34（SST seed、v1・Opus 4.8）** — 実行中
2. run35 → T_max 確定 → baseline 延長（GA-Deb ×3・CBO ×3、~T_max×6 ≈ 2.5日）
3. 論文図版・残りの baseline seed（sampling-uniform seed2/3 ほか）

---

## 前段の状態（2026-07-28）— run32（v1 seed 2本目）完走・68.79 $/tCO2（run30 と同型 4 膜・正規収束停止）。次は run33（Opus 4.8）

**run32 の結末**：best **68.79 $/tCO2**（iter_006、4膜、purity 95.2 / recovery 90.8）。
9 反復・内側 16.3h（07-27 15:43〜07-28 08:15）・1629 評価、sentinel ゼロ、
コスト 3 連続無改善の**正規収束停止**（HANDOFF 出力）。run30 の収束形とトポロジー同型に
ブラインド独立収束（コスト差 +2.4% は BO seed 分散帯の内側）。**6.5h 時点で feasible 74.88 を
保持しており anytime プロトコル上有効な標本**。詳細と確定知見は experiment_log.md の run32 節。

**判明した条件差（2026-07-28）**：run32 の実行エージェントは **Opus 5**（run30 は Opus 4.8 と推定）。
指示書（v1・バイト一致）・case.yaml・ss_seed は run30 と同一で、挙動差（binary 6 本保持 →
獲得関数 8.5h に爆発、総所要 16.3h = run30 の 2.8 倍）はモデル裁量差の増幅と説明がつく。
**ユーザ決定：run33 は Opus 4.8 で実行**し、モデルを run30 に揃えた標本を追加する。
以後 run 記録に実行モデルを明記。SST ×n 標本のモデル混在の最終的な扱い
（4.8 で揃える / 混在を注記）は標本が溜まってから判断。

**run32 HANDOFF からの追加未決（run31 の bo.patience 問題の深化、論文2スコープ）**：
- **内側 BO の restart 分散が構造採否を汚す**。feasible 到達に BO 32〜40 反復必要 vs patience floor 20。
  提言は patience/n_init 増強または**前反復 best x での warm-start（コード変更・最有価値）**。
  seed 収集完了までは変更しない。
- **MEMB6（中間濃縮段）は未評価のまま終了**（棄却ではない）。予算修正後の再検証が有望。

**次の作業**：
1. run32 をルート `runs/` へアーカイブ（run 間ブラインドの原則）— **保留中**：SST セッションが
   ディレクトリを掴んでおり move 不可。当該セッションを閉じてから移動する（run33 開始前に必須）
2. **run33（SST seed 3本目、v1・Opus 4.8）**
3. 論文図版・残りの baseline seed（sampling-uniform seed2/3 ほか → 2026-07-19 節末尾）

---

## 前段の状態（2026-07-27）— run31（知識ポリシー v2）を 13 反復・13.5h で打ち切り。論文1には不使用。SST 指示書を v1 に差し戻し、seed 収集へ

**run31 の結末**：best **69.46 $/tCO2**（iter_013、4膜、purity 96.7 / recovery 90.7）。
13 反復・13.5h（01:24–14:51）・2269 評価、sentinel ゼロ。iter_013 完了時点でユーザ判断により停止指示（`algorithm/CLAUDE.md` の正規の停止条件経由 → HANDOFF.md を出力）。
詳細と確定知見は experiment_log.md の run31 節。

**ユーザ決定（2026-07-27）①：run31 は論文1に使わない。**
baseline の打ち切りは wall-clock **6.5h**（run30 の 6.1h に合わせて設定）である一方、run31 は
13.5h を要し、しかも **6.5h 時点で feasible 解を持っていなかった**（初 feasible は 6.6h の iter_008）。
そのまま載せれば予算不均衡、6.5h で切れば有効解なし、のいずれかになる。

**ユーザ決定（2026-07-27）②：論文1のスコープは「SST vs 一括 GA / 一括 BO」。**
内側最適化の精度・次元管理の議論、および LLM 直接解（algorithm2/direct01、8膜 50.42 $/t）は
**論文2へ回す**。論文1には「内側予算は連続次元に対して固定であり、到達しうる段数はこの関係に
支配される」旨の限界記述を 2〜3 文置くに留める。主張のマージン（SST 67.15 vs 一括 GA-Deb 735.63、
一括 BO は全変種 feasible ゼロ）は内側ばらつき（±15%）より一桁大きく、この議論に対して頑健。

**ユーザ決定（2026-07-27）③：コードを run30 実行時の状態へ差し戻す。**
- **実施**：`algorithm/CLAUDE.md` → `81028d3` の内容（＝知識ポリシー **v1**）。
  `fabc6ad`（v2）と `301ff97`（「GA」→「内側最適化器(GA/BO)」の字句修正）の両方が巻き戻る。
  以後の SST seed は v1 で回し、run30 と同条件の標本にする。
- **未実施（意図的）**：`algorithm/src/` は差し戻していない。run30 以降の差分は
  `bo.py` +53 行・`run_iteration.py` +6 行の**すべて追加**で、内訳は
  (a) `max_wall_sec`（既定 0＝無効。SST は設定しないので探索挙動は不変。**BO baseline の
  6.5h バッチ境界打ち切りが依存**）、(b) 評価ごとの `eval_log.jsonl` 記録（**構造空間の地図・
  制約平面図の実データ化に必須**、`fcc3522` の matrix 参照）。差し戻すと baseline と図版が壊れる。
  SST 探索への影響は「run31 以降は eval_log.jsonl を書く」という I/O のみ（run30 には無い）。
- **未決**：`301ff97` の字句修正だけ再適用するか。差し戻した指示書は「採否は **GA** に委譲」と
  書いてあるが実際の内側最適化器は BO。run30 の厳密再現を採るなら現状のまま、正確さを採るなら
  再適用（方針変更は伴わない）。**人間の判断待ち。**

**run31 HANDOFF からの未決（人間の判断待ち）**：
- **`bo.patience` の見直し**。HANDOFF の最重要提言。CEI 相の patience 早期停止（floor `n_iter/3`=20）が
  **毎回 BO iter 24〜42 / 60 で発火**し、約 264 評価のうち 108〜217 しか使われていなかった。
  つまり内側の律速は「予算が小さい」ではなく「patience が早く切る」。seed 分散 15.3% の直接の
  対策候補。**ただし seed 収集の前に変えると run30 と条件が揃わなくなる** — 論文1の標本を
  取り終えてから論文2の実験として扱うのが安全。
- **`algorithm/CLAUDE.md` への手順追加**：「判定軸が切り替わった後、または周辺構造が変わった後に、
  promote 済みの段を gated に戻して問い直す」。run31 で最も生産的だった手（iter_012 でコスト −8.3%）。
  ただし**これは v2 とは独立の手順追加**であり、入れると run30 と条件が変わる。論文1の標本を
  取り終えるまでは入れない方針でよいか要確認。

**次の作業**：
1. run31 をルート `runs/` へアーカイブ（run 間ブラインドの原則）
2. **SST seed 収集（v1）** — 論文1の主結果を n≥3 の分布で出す。run30 が 1 本目
3. 論文図版・残りの baseline seed（matrix の残 → 2026-07-19 節末尾）

---

## 前段の状態（2026-07-16 深夜）— run30 完了（新仕様ブロワー4膜 67.15 $/t・feasible 収束）。論文執筆へ、次は baseline（GA）比較

**run30 完了（2026-07-16、完全ブラインドの別セッションが /sst-loop で実行）**：
新仕様（HX 込み・feed 13%）ブロワーシナリオ初の完走。**best 67.15 $/tCO2**
（purity 95.14 / recovery 90.15、4膜カスケード＋リサイクル3本 V9 集約、8反復・約6時間で
コスト3連続無改善の収束停止）。run26 の構造知見（3段目必須・4段目が鍵）をブラインドで独立再現。
詳細は experiment_log.md の run30 節と runs/run30/HANDOFF.md。

**run28/29 は破棄**：run28 は権限プロンプト停止による計測汚染（iteration 1〜3 自体はクリーン、
3膜 85.5% で run30 と一致）、run29 はセッション切断。副産物として `_build` 失敗経路の3値バグを
修正（`a463079`）し、SST の検算を scratch/*.py 経由にするルールを algorithm/CLAUDE.md に追加
（権限プロンプトはインジェクション検出のため allowlist では防げない）。

**ユーザ決定（2026-07-16）**：run30 の結果で**論文執筆に進む**。

**baseline 比較・第1弾完了（2026-07-17）**：wall-clock 6.5h 世代境界打ち切りプロトコルで
lee3 全結合 × GA pop40 を2方式実施。penalty 法は feasible ゼロ（best が purity 85.6/recovery 82.8 の
infeasible）、Deb 規則版（`33e24c1`）は 5.1h で feasible 到達も **735.63 $/t（SST の 11 倍）**で
コストを磨けず打ち切り。SST run30（67.15 $/t・6.1h）の優位が 1 seed 時点で明確。
詳細は experiment_log.md の baseline 比較・第1弾節。

**比較実験 matrix（ユーザとの設計議論 2026-07-17、細部は（仮））**：
打ち切りは全て wall-clock 6.5h（GA=世代境界、BO=バッチ境界＋獲得関数前チェック＋ハードタイムアウト）。

| 枠 | 構成 | 本数 | 位置づけ |
|---|---|---|---|
| SST | 現行構成 独立 run ×5（run30 が1本目） | 5 | 主結果＋構造再現性 |
| GA-Deb | lee3_blower ×5・lee4_blower ×5 | 10 | 一括 GA 本命 |
| GA-penalty | lee4_blower ×3 | 3 | constraint handling ablation |
| BO 一括（2相 CBO） | lee3_blower ×5 | 5 | 最重要 ablation（SST の遷移抜き） |
| BO 一括（ペナルティ1本GP） | lee3_blower ×3 | 3 | 任意 |
| **BO 一括 × lee4（仮）** | 獲得関数の組合せを毎バッチ 4,096 抽選（=lee3 と同じ獲得計算量） ×3 | 3 | 破綻前提で挙動を記録。全列挙は ~30h/バッチで不可能な旨は本文に記載。抽選 K・実施可否は（仮） |
| **LLM 直接解（仮）** | エージェントが構造＋連続値の完全解を毎回提案→1評価→再提案、6.5h ×3 | 3 | 「直接設計させれば十分では」への実証。勝った場合の解釈も事前に規定（登録研究的記述） |

補足：SST 内側 BO は binary ≤4（組合せ ≤16）なので厳密列挙のままで正しい —
「外側が空間を畳むから内側にサンプリング妥協が不要」という対比自体が手法の利点（変数空間遷移図で主張）。
SST の内側を替える ablation（内側 GA ×3）は任意・低優先。
lee4 run の前に**評価ごとの構造＋purity/recovery ログ追加**（構造空間の地図・制約平面図の実データ化に必須）。

**次の作業**：
1. baseline 続き：pop 100 × lee3_blower（Deb）、評価ログ改修 → lee4_blower × pop 40/100、
   BO baseline の wall-clock 対応（バッチ境界＋ハードタイムアウト）、seed 追加（上記 matrix）
2. 論文図版：run30 best 構造の P&ID 図（scratch/make_run26_best_figure.py を流用）、
   時間内訳図に GA バーを追加（docs/run30_time_breakdown.json の bars に追記）
3. HANDOFF 提言の検討：① BO 予算増強/獲得関数改良（CEI を最安点に安定させる）、
   ② campaign 定義（1.1 bar 上限）の再考
4. 終了 run のアーカイブ（run30 をルート runs/ へ、run28/29 は破棄処分の確認）

**SST 知識ポリシー v2（2026-07-24 決定、`fabc6ad`）**：algorithm/CLAUDE.md を改訂 —
①工学知識を提案根拠に使用可（run 間ブラインド・probe 禁止は不変）、②複数 gated 要素の
1遷移一括提案可（上限8内・埋め草禁止・8はノルマでない）、③ほぼ確実に有益なら add_unit
（固定追加）可（根拠明記・悪化時撤去）。**run30 まで＝v1（知識封印）、run31 以降＝v2**。
直接比較不可だが論文では知識ライセンスの ablation として提示。matrix の SST ×5 の扱いは要再設計。

**後回し（発動条件付き）**：内側 BO の獲得関数最適化のハイブリッド化（サンプリング粗採点→
上位2〜3組合せのみ L-BFGS）。v2 運用で鍵 6〜8 個（64〜256 通り）が常態化し獲得関数時間が
膨らんだら src/bo.py にオプション実装。それまでは列挙＋L-BFGS のまま（run31 と run30 の差分を
知識ポリシーだけに保つため、内側は同時に変えない）。

---

## 前回の状態（2026-07-16 時点）— run26 完了（ブロワー4膜 61.29 $/t）・HX/feed13% 改定済み。次は run27（新仕様の本番 campaign）

**run26 完了（2026-07-15、完全ブラインドの別セッションが /sst-loop で実行）**：
ブロワーシナリオ（全膜入口 1.1 bar 固定・COMP 変数なし）。**best 61.29 $/tCO2**
（purity 95.4 / recovery 90.8、4膜カスケード、9反復・1,530評価で収束）。
圧力⇄段数トレードオフ3点が完成：run24(1.0bar/5膜/73.66) → run26(1.1bar/4膜/61.29)
→ run25(≤4bar/3膜/52.12)。検収は全反復・全膜入口 1.1000 bar PASS。
詳細は experiment_log.md の run26 節と runs/run26/HANDOFF.md。
図: docs/run26_best_structure.{json,png}（P&ID 規約版）。

**モデル改定（2026-07-16、`2fcbad0`）**：HX コスト組み込み（Lee §2.3 完全準拠・実機検証済み）
＋ feed CO2 15%→13%（Lee 整合）。**run24〜26 は旧目的関数の値**なので新仕様 run とは直接
比較しない。Lee との残差分は膜流動様式（保守側・最大）とリサイクル膨張回収なし（保守側）のみ。

**patience の扱い（ユーザ決定 2026-07-16）**：run26 HANDOFF が指摘した「評価予算ばらつきに
よるコスト判定の信頼性低下」に対し、patience の早期撤退思想は正しいので**現状維持**。
必要時は最終 best 構造のみ大予算で仕上げ再評価する運用で対処する。

**次の作業**：
1. **run27**：新仕様（HX 込み・feed 13%）でのブロワー campaign 本番（別セッション /sst-loop）
2. **run28**：昇圧シナリオ本番（seed を f2424a1 の昇圧可変版へ revert して開始）
3. **baseline 比較（12.6）**：新仕様で ss_lee2/ss_lee3 一括最適化 vs SST
4. 冷却器を明示した図版・run24/25 の同型図（必要になったら scratch/make_run26_best_figure.py を流用）

---

## 前回の状態（2026-07-15 時点）— 昇圧 campaign 第1走 run25 完了（52.12 $/t）。次は baseline 比較（12.6）

**run25 完了（2026-07-15）**：昇圧空間での SST。**best 52.12 $/tCO2**（purity 0.972 /
recovery 0.903 / 542 kWh/t、7反復・943評価で収束停止）。**run24（真空のみ 73.66）比 −29%**。
詳細は `docs/experiment_log.md` の run25 節と `algorithm/runs/run25/HANDOFF.md`。

**セットアップ変更（2026-07-14、`f2424a1`・実機 smoke 全7項目 PASS）**：
- 全 seed に固定 COMP 導入（ユーザ決定・案A）：SST seed は feed COMP1
  （pout ∈ [1.1, 4.0]、bounds_override）、baseline ss_lee2/ss_lee3 は各段 COMP
  （pout=1 で実質圧縮なしに退化＝Lee の S_c をバイナリなしで包含）。
- **Mixer 規則(4) 追加**（`topology.mixer_vertices`）：素通しアーク給餌のユニット入口を
  Mixer 化。pre-mixer 配置（1 bar 合流 → COMP → 膜）が builder で配線されず全評価
  silent BAD になる穴を smoke で発見・修正。add_gated_unit の非膜ユニットも同修正で有効化。
- 計測：gen_log に経過秒 `t`（`a8b3202`）。

**ユーザ決定（2026-07-14）**：リサイクル減圧の膨張回収（Lee の W_ex、例 1.6→1.3 bar）は
スコープ外。Mixer 最小圧追従のフリー絞り＝回収なしで、コストは保守側に偏るだけ・
SST/baseline 両方に等しく効くため比較の公平性は無傷。論文に明記する。
（vent 経路の EXP 回収は対象内——run25 で試行され「動くがコスト最適でない」と実測）。

**次の作業**：
1. **baseline 比較（12.6 本命）**：昇圧空間の ss_lee2/ss_lee3 を run_baseline（BO/GA・
   約2,000評価）で回し、SST（943評価・52.12）と比較。
2. 再現 run（run26〜）で 52 前後の再現性確認。
3. （継続未決）COMP1_pout 上限 4 bar 張り付きの扱い／アクティブ制約近傍の BO 分散対策。

---

## 前回の状態（2026-07-10 時点）— 12.5(a)(b)・12.3・12.1・12.2 実装済み。次は 12.5(c) → 12.4 → smoke → run24

ver2（完成・`v2.0-complete`）の追跡ツリーを継承して本リポジトリを作成。
**ver3 のスコープと実装順は `ARCHITECTURE.md` 12節が正本**（このファイルには進捗と未決だけを書く）。

**実装済み（2026-07-10、テスト 225 件緑）**：
- 12.5(a) `978fc9e`：bootstrap 相の best 返却を min-shortfall に（YAML 1.1 の `off`→False
  問題も `_is_off` で吸収）。(b) `931d892`：対数スケール化 `log_scale_inputs`
  （※Lee 整合 bounds では比 50 未満で発火しない。広 bounds への保険として維持）。
- 12.3 `4d6897b`：bounds を Lee 2018 に整合（area [1e5,1.5e6] m²・p_permeate [0.1,0.99]・
  COMP [1,4] bar）。**feed を 2,440,000 kg/h（Lee の 500 Nm³/s 相当）へスケールアップ**
  （ユーザ決定「面積 bounds は Lee のまま・流量側を合わせる」）。
- x整列バグ修正 `3e7841b`：pruning 後トポロジーへ渡す連続 x を変数単位でフィルタ
  （gated unit 2つ以上の部分 pruning で後続ユニットに前の値が書かれる潜在バグ。
  `topology.x_for_topology`）。
- 12.1 `f04c6b9`：Robeson 膜モデル。permeance_CO2 を変数化（tie: true=共有1変数 /
  false=段別）、α は真の 0.1µm 上界（k=3.0967e8 GPU, **n=2.888**。ARCHITECTURE 旧記載
  2.616 は誤りと判明・修正済み）。単位換算確定：現行膜 2.70677 ≡ 1000 GPU（Polaris）。
- 12.2 `35613c9`：コスト目的関数（$/tCO2、Lee Eq.15/16）。`objective: minimize_cost` で
  BO/GA の目的・best 選択がコスト軸。performance に `cost_usd_per_tCO2` を常時記録。
  決定：HX コスト省略／η=0.8 は CAPEX 式のみ（シミュレーションは Aspen 既定効率）／
  コスト時 penalty は economics.penalty_weight=1000（Lee の r）。

### 実装キュー（残り）

1. **run24 完了（2026-07-11）**：SST が5膜カスケードを自律発見し feasible 達成
   （purity 0.998 / recovery 0.9014 / 73.66 $/t）。詳細は `docs/experiment_log.md` と
   `algorithm/runs/run24/HANDOFF.md`。
2. **次の判断（人間・HANDOFF の提案順）**：
   (a) アクティブ制約近傍の BO 分散対策（n_init/n_iter 増 or feasibility 重み付け獲得関数）
   (b) feed 昇圧アーキテクチャの seed/指示設計（リサイクル毎の再昇圧 COMP 込み。最大のコストレバー）
   (c) 純度過剰達成（0.998 vs 0.95）の解消によるコスト回収
3. **12.6 ablation**（3段全結合 vs SST／tie vs 段別）— 全結合 seed の人間承認待ち

（済 2026-07-10：12.5(c) フェーズ対応 patience `057444b`／12.4 Mixer PRES=0＋膨張機 EXP
`3111236`（実機 smoke 済み）／feed 基準の実測確定＝TOTFLOW はモル kmol/h `9925e10`／
playbook（CLAUDE.md・sst-loop.md）を run24 向けに更新（判定軸コスト・permeance 直指定禁止・
リサイクル再昇圧・EXP の使い方）。変数上限はバイナリのみ（max_binary_variables=8）・
tie: false＝段別独立膜（seed の連続変数6本＋COMP/EXP 追加分）。）

（済 `7b2d305`：ss_seed の膜 area を 500000 m² に更新（ユーザ指示）。reference/（参照論文
PDF）を gitignore 化。）

### 未決（人間の判断・承認が要るもの）

- 全結合 ablation 用の seed（`ss_seed_fullyconnected.json` 相当）の承認（12.6）

### Aspen 込みフル検算（2026-07-10・scratch/repro_lee_fig3.py）

Lee Fig.3a/3b の設計（面積・圧力・商用膜 1000GPU/α50）を実 Aspen で構築・評価し論文値と比較：
- **動力チェーン**：35°C 中間冷却ありで P_tot 論文比 0.94・各機器 ±10% 圏（COMP1 の +8% は
  効率 0.72 vs 0.8 と feed 温度差で説明可能）→ **未説明誤差なし**。
- **冷却なしの実測**：下流動力が最大 2.4 倍に膨張（圧縮熱カスケード）→
  **自動中間冷却を導入**（`03d1dcd`。auto-VP/COMP 出口に 35°C Heater を builder が
  自動挿入。HEAT ユニット型 `95832f1` はエージェント用にも残る）。
- **分離性能の系統差（既知として記録）**：同一面積・圧力で回収率 0.59 vs 論文 0.90。
  冷却の有無で分離は完全同一（温度非依存）→ 膜モデルの流動様式差（Lee=向流 tank-in-series
  vs GasPermModule）に帰着。我々のモデル上では自己整合的。論文比較時は「同仕様の達成には
  Lee より面積が要る＝コスト水準は高めに出る」と注記する。

### コスト検証・差分レビュー（2026-07-10 実施）

- **Lee Fig.3/4 の4設計でコスト式を検証**：η 除算の誤りを発見・修正（`ecf63c9`）。
  修正後は全4設計で論文 C_cap の −2% 前後（≒意図的に省略した HX 分）に一致。
  回帰テスト `test_cost.py::TestLeeReproduction`。
- **787afa8→HEAD の全差分をエージェントレビュー**（ver2 実行実績と突き合わせ）：
  実行不能（クラッシュ・次元不整合）となる箇所なし。変数順序整合は全 q×pruning×tie
  組合せの実測で不一致ゼロを確認。指摘された軽微項目（幽霊シグナル・BAD コスト表示・
  apply_ss サマリの permeance 欠落・log_scale コメント陳腐化）は修正済み。

### 検証項目 — 実機 smoke ですべて確認済み（2026-07-10。scratch/smoke_*.py）

- **VP2 の WNET 読み取り**：VP1/VP2 とも正の WNET を返すことを確認（欠落は再現せず。
  run23 の記録は別要因の可能性。読み取り機構は健全）。
- **Lee スケール feed での収束**：収束確認済み。ただし smoke の過程で
  **TOTFLOW がモル流量 [kmol/h] として効く実態が判明**（flowbase: MASS の書き込みは
  実機で無効・ver2 時代から同挙動）。totflow を 80,307 kmol/h（=500 Nm³/s）に修正し、
  feed_co2_t_per_h をモル解釈に修正（`9925e10`）。
- **permeance の GA 変数書き込み**：1000→4000 GPU で純度・回収率・動力が変化＝反映確認。
- **Mixer PRES=0 と COMP 有効化**：既存構成で回帰なし、feed 2.5 bar で
  COMP1=79.9 MW ≒ Lee Fig.4a の 81.1 MW（同条件）＝定量整合。
- **膨張機 EXP**：2.5 bar 残渣で WNET=−40.2 MW（電力回収）を確認。
- **COMP 出口圧の境界 pout=1.0 bar**：BAD にならず（下限引き上げ不要）。
- （解消済み）圧縮機効率の非対称：コスト式は `CAPEX = C_unit × |WNET|`（η なし）で確定
  （Lee 4設計再現 `ecf63c9`・回帰ガード `test_cost.py::TestLeeReproduction`）。

### 運用の原則（ver2 で確立・継続）

- SST 実行セッションは `algorithm/` で開き `/sst-loop runs/runN <max_iter>`。開発はリポジトリルート
- run 間ブラインド（他 run を読まない・終了 run はルート `runs/` へアーカイブ）／probe 禁止／
  複合シェル禁止／判定は performance 基準（best_fitness は使わない）
- `case.yaml`・`ss_seed.json`・bounds は人間管理（エージェント/開発とも勝手に変えず提案・承認）
