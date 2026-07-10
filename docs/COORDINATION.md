# COORDINATION.md — 作業引き継ぎ・未決事項（ver3）

直近の作業状態と未決確認事項を記録する。`ARCHITECTURE.md`（設計の真実）には書かない、
暫定／要確認のメモはここに置く。ver2 までの経緯は ver2 リポジトリの docs を参照（凍結・参照専用）。

---

## 直近の状態（2026-07-10 時点）— 12.5(a)(b)・12.3・12.1・12.2 実装済み。次は 12.5(c) → 12.4 → smoke → run24

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

1. **run24 開始**（`algorithm/` でセッションを開き `/sst-loop runs/run24 <max_iter>`）→ **12.6 ablation**

（済 2026-07-10：12.5(c) フェーズ対応 patience `057444b`／12.4 Mixer PRES=0＋膨張機 EXP
`3111236`（実機 smoke 済み）／feed 基準の実測確定＝TOTFLOW はモル kmol/h `9925e10`／
playbook（CLAUDE.md・sst-loop.md）を run24 向けに更新（判定軸コスト・permeance 直指定禁止・
リサイクル再昇圧・EXP の使い方）。変数上限はバイナリのみ（max_binary_variables=8）・
tie: false＝段別独立膜（seed の連続変数6本＋COMP/EXP 追加分）。）

（済 `7b2d305`：ss_seed の膜 area を 500000 m² に更新（ユーザ指示）。reference/（参照論文
PDF）を gitignore 化。）

### 未決（人間の判断・承認が要るもの）

- 全結合 ablation 用の seed（`ss_seed_fullyconnected.json` 相当）の承認（12.6）

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
