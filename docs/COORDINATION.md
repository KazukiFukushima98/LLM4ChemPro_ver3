# COORDINATION.md — 作業引き継ぎ・未決事項（ver3）

直近の作業状態と未決確認事項を記録する。`ARCHITECTURE.md`（設計の真実）には書かない、
暫定／要確認のメモはここに置く。ver2 までの経緯は ver2 リポジトリの docs を参照（凍結・参照専用）。

---

## 直近の状態（2026-07-10 時点）— 12.5(a)(b) 実装済み・(c) から再開

ver2（完成・`v2.0-complete`）の追跡ツリーを継承して本リポジトリを作成。
**ver3 のスコープと実装順は `ARCHITECTURE.md` 12節が正本**（このファイルには進捗と未決だけを書く）。

**12.5(a)(b) 実装済み（2026-07-10、テスト 191 件緑）**：
- (a) `978fc9e`：全 infeasible 終了時の best 返却を min-shortfall（同率 fitness tie-break）に。
  `bootstrap: off` で旧 penalty-min。あわせて YAML 1.1 が `off` を bool False にパースして
  ロールバック口が効かない潜在バグを `_is_off` で吸収（`bo:` の全フラグ共通）。
- (b) `931d892`：bounds 比 50 倍超の正の連続変数を GP/acqf/Sobol 内部で log 変換
  （`log_scale_inputs`、既定 on）。現行 bounds では area（比5000）に加え p_permeate（比99）も
  対象になる点に注意（12.3 で下限 0.1 になると p_permeate は比9.9で対象外に戻る）。
  `bo: log_scale_inputs: off` で線形復帰。

### 実装キュー（ARCHITECTURE 12節の要約・推奨順）

1. **12.5 アルゴリズム持ち越し**（bo.py・run24 の前に）：
   ~~(a) bootstrap 相の best 返却を min-shortfall に修正~~ 済
   ~~(b) 対数スケール化 `log_scale_inputs`~~ 済
   (c) フェーズ対応 patience（素朴な連続無改善カウントは棄却済み・要件は 12.5 の表）
2. **12.1 Robeson 膜モデル**（permeance 変数化・α 導出・同一膜/段別の2シナリオ）
3. **12.2 コスト目的関数**（$/tCO2。economics: セクションは人間承認）
4. **12.3 bounds 現実化**（p_permeate 下限 0.1・COMP 1〜4 bar。人間承認）
5. **12.4 圧力アーキテクチャ**（★Mixer PRES=1.0 の罠→Aspen 実機 smoke 必須・
   ★VP2 WNET 欠落疑義の検証・リサイクル圧整合・膨張機）
6. run24 系開始 → **12.6 ablation**（3段全結合 vs SST／probe あり vs なし）

### 未決（人間の判断・承認が要るもの）

- case.yaml への新セクション追加の承認：`membrane_model:`（12.1）・`economics:`（12.2）
- UNIT_BOUNDS の変更承認：p_permeate [0.1, 0.99]・COMP [1.0, 4.0]（12.3）
- 全結合 ablation 用の seed（`ss_seed_fullyconnected.json` 相当）の承認（12.6）

### 検証項目（ver2 からの持ち越し疑義）

- **VP2 の WNET 読み取り欠落**：run23 最適解の energy_breakdown に VP2 が無い（読み取り0）。
  0.01 bar からの再圧縮で物理的にゼロはあり得ず、記録エネルギーが過小の可能性。
  ver3 最初の Aspen smoke で VP{n} 全部の WNET ノードを確認する。

### 運用の原則（ver2 で確立・継続）

- SST 実行セッションは `algorithm/` で開き `/sst-loop runs/runN <max_iter>`。開発はリポジトリルート
- run 間ブラインド（他 run を読まない・終了 run はルート `runs/` へアーカイブ）／probe 禁止／
  複合シェル禁止／判定は performance 基準（best_fitness は使わない）
- `case.yaml`・`ss_seed.json`・bounds は人間管理（エージェント/開発とも勝手に変えず提案・承認）
