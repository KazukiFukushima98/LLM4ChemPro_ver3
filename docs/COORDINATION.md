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

1. **12.5(c) フェーズ対応 patience**（素朴な連続無改善カウントは棄却済み・要件は 12.5 の表）
2. **12.4 圧力アーキテクチャ**（★Mixer PRES=1.0 の罠→Aspen 実機 smoke 必須・
   ★VP2 WNET 欠落疑義の検証・リサイクル圧整合・膨張機）
3. **run24 前の Aspen 実機 smoke**（下記の検証項目を一括確認）
4. **run24 準備**：`algorithm/CLAUDE.md` playbook 更新（新ユニット params の permeance
   直指定廃止・改善判定軸を cost に・membrane_model/economics の説明）→ run24 開始
   （membrane_model.tie: **false**＝段別独立。2026-07-10 ユーザ決定）→ **12.6 ablation**

（済 2026-07-10 追加決定：SST の変数上限を**バイナリのみ**（max_binary_variables=8）に変更、
旧 max_variables=20 は撤廃。tie: false へ切替（seed の変数は連続6＝面積2・p_perm2・perm2）。
CLAUDE.md/sst-loop.md の自己点検ルールも更新済み。）

（済 `7b2d305`：ss_seed の膜 area を 500000 m² に更新（ユーザ指示）。reference/（参照論文
PDF）を gitignore 化。）

### 未決（人間の判断・承認が要るもの）

- 全結合 ablation 用の seed（`ss_seed_fullyconnected.json` 相当）の承認（12.6）

### 検証項目（run24 前の Aspen 実機 smoke で一括確認）

- **VP2 の WNET 読み取り欠落**（ver2 持ち越し）：run23 最適解の energy_breakdown に VP2 が
  無い（読み取り0）。VP{n} 全部の WNET ノードを確認する。コスト目的では CAPEX も汚染する
  ため 12.2 の前提でもある。
- **feed 3桁スケールアップ（1000 → 2.44e6 kg/h）での Aspen 収束**（12.3 の変更）。
- **permeance の GA 変数書き込み**（`L("CARBO-01")`/`L("NITRO-01")` ノードへの実機書き込み
  が反映されるか。12.1 の変更）。
- **圧縮機の実効効率の確認**：CAPEX 式の η=0.72 は「Aspen Compr 既定の等エントロピー効率
  0.72」を前提に統一した（2026-07-10 決定・効率非対称の解消）。実機で VP/COMP ブロックの
  効率ノードを読み、既定値が本当に 0.72 かを確認する（違えば economics.pressure_unit_efficiency
  を実値に合わせる）。

### 運用の原則（ver2 で確立・継続）

- SST 実行セッションは `algorithm/` で開き `/sst-loop runs/runN <max_iter>`。開発はリポジトリルート
- run 間ブラインド（他 run を読まない・終了 run はルート `runs/` へアーカイブ）／probe 禁止／
  複合シェル禁止／判定は performance 基準（best_fitness は使わない）
- `case.yaml`・`ss_seed.json`・bounds は人間管理（エージェント/開発とも勝手に変えず提案・承認）
