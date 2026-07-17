# COORDINATION.md — 作業引き継ぎ・未決事項（ver3）

直近の作業状態と未決確認事項を記録する。`ARCHITECTURE.md`（設計の真実）には書かない、
暫定／要確認のメモはここに置く。ver2 までの経緯は ver2 リポジトリの docs を参照（凍結・参照専用）。

---

## 直近の状態（2026-07-16 深夜）— run30 完了（新仕様ブロワー4膜 67.15 $/t・feasible 収束）。論文執筆へ、次は baseline（GA）比較

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
