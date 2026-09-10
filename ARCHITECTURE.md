# ARCHITECTURE.md

本ドキュメントは LLM4ChemPro の設計の単一の真実（single source of truth）である。
実装に着手する前に必ず全体を読むこと。設計に迷ったときは推測せず本ドキュメントに従う。
記載のない設計判断が必要になった場合は、勝手に決めず人間に確認すること。

構成：手法（2〜7節）、ディレクトリと Aspen 連携（8〜9節）、ケース固有の設計（10節：
Robeson 上限に沿った段別膜物性の変数化／コスト目的関数（$/tCO2）／変数境界／圧力アーキテクチャ）。

---

## 1. プロジェクトの目的

LLM駆動スーパーストラクチャ遷移（Superstructure Transition; SST）による化学プロセスの自動合成。
固定された超構造を仮定せず、最適化の結果を LLM が解釈して超構造そのものを反復的に組み替える。

ケーススタディは膜分離による CO₂ 分離プロセス（feed は post-combustion 相当の CO₂ 13%）。
**アルゴリズム本体はドメイン非依存**に保ち、膜・Aspen 固有の知識は
`aspen_builder.py` / `simulator.py` / `unit_registry.py` にのみ閉じ込める。

---

## 2. 二層最適化構造

```
外側（LLM / SST）        超構造そのものを変える     離散的・低頻度   ← Claude が回す
    ↑ 各SSの最適解と性能シグナルを見て次のSSを決める
内側（最適化器 / MINLP）  固定された超構造の中で解く   パラメトリック   ← run_iteration.py
```

- **内側**：ある反復で超構造は固定。バイナリ変数 q と連続変数 x を内側最適化器で同時最適化する MINLP。
  内側最適化器は Constrained BO（5.6節）。`case.yaml` の `optimizer` で Mixed GA（5.4節）にも切替えられる。
- **外側**：内側の最良解とシグナルを **Claude（SST agent）が読み、次の SS を決めて適用する**。

外側ループの司令塔は Python スクリプトではなく **Claude 自身**である（本研究の核心）。
Claude の運用ルールは `algorithm/CLAUDE.md` に定義する。

各反復で内側 MINLP の次元（変数の数）が変わる。つまり各超構造は事実上異なる MINLP インスタンスである。

---

## 3. トポロジー表現（最重要・設計の核）

### 3.1 基本方針

- ノードは**ストリーム位置**に対応させる。
- 接続関係のみに着目する（流量・組成・圧力などの物理量はグラフに保持しない。Aspen が計算し results に出力する）。
- 流れには方向があり、リサイクルのため `i→j` と `j→i` は区別する（**有向グラフ**）。
- トポロジーは**安定した文字列頂点ID（`V0`, `V1`, …）をキーとするアーク辞書**として保持する。
- **頂点IDは永続的**。削除しても再付番しない。削除した番号は欠番のまま、再利用しない。
- **新頂点の採番は「過去に払い出した最大番号＋1」**とする（頂点の個数＋1ではない）。
  例：`{V0..V8}` に追加 → V9。その後 V9,V10 を削除して再度追加 → V11（V9/V10 は再利用しない）。
  個数ベースで採番すると削除後に既存IDと衝突し、安定IDの前提が崩れるので**必ず最大値ベース**にする。
  頂点集合は連番ではなく飛び番を含む文字列IDの集合になりうる。
- 実装はアーク辞書（辞書操作）で行う。隣接行列・S·A·Sᵀ+ΔA は論文記述用の数学的形式であり、コードには持たない。
  論文用に行列が必要なときは辞書から生成する（`to_matrix`）。

### 3.2 用語：SSテンプレート と 具体トポロジー

- **SSテンプレート**：候補アーク（`q_k`）を含む超構造の定義。`ss_current.json` が保持するもの。
- **具体トポロジー**：バイナリ `q` を固定して候補を解決した、実際に Aspen で建てられる構造。
  `active_topology(q)` がテンプレートから生成する。**evaluator / aspen_builder が受け取るのは常に具体トポロジー**。
  具体トポロジーは「候補解決済みの3辞書 `{vertices, arcs, units}`」とする（3.3節と同じ形）。
  `aspen_builder` が必要とする `auto_vps`（自動VPの一覧）や energy_blocks（VP+COMP）は**ビルド時に builder/evaluator が内部で導出**するものであり、具体トポロジーには含めない。
  したがって評価境界の引数は具体トポロジーのみで足りる（5.2節）。
  **安定文字列ID（`V4` 等）をそのまま Aspen ストリーム名に使う**（IDにアンダースコアが無いので可）。
  **命名の責務境界**：`topology.py` が扱うのは頂点ID（`V7` 等）だけ。Aspen 固有の補助名（Mixer ブロック `MIXV{j}`、中間ストリーム `VS{i}T{j}`、VP の `VP{n}`/`VPI{n}`、FSplit 余剰 `DV{i}`）は **aspen_builder の内部に閉じる**。頂点ID＝Aspenストリーム名は同一に揃えるので、ID とストリームの混在は起きない。

### 3.3 メモリ上の3辞書

`topology.py` が保持する内部表現。

```python
vertices = {
    "V0": {"role": "feed",     "label": "Feed (CO2 13%)"},
    "V1": {"role": "internal", "label": "MEMB1 inlet"},
    "V2": {"role": "internal", "label": "MEMB1 permeate"},
    "V3": {"role": "internal", "label": "MEMB1 retentate"},
    "V7": {"role": "product",  "label": "Product (CO2)"},
    "V8": {"role": "residue",  "label": "Residue (waste)"},
}

arcs = {
    ("V0", "V1"): {"type": "feed",               "unit": None},
    ("V1", "V2"): {"type": "membrane_permeate",  "unit": "MEMB1"},
    ("V1", "V3"): {"type": "membrane_retentate", "unit": "MEMB1"},
    ("V5", "V7"): {"type": "product",            "unit": None},
    ("V6", "V1"): {"type": "recycle",            "unit": None, "candidate": "q_1"},
}

units = {
    "MEMB1": {"type": "MEMB", "inlet": "V1",
              "outlets": {"permeate": "V2", "retentate": "V3"},
              "params": {"area": 20000.0, "p_permeate": 0.2}},
}
```

ディスク上の JSON は別形式（タプルキー不可、3.7節）。`load_ss` / `save_ss` が相互変換する。

### 3.4 頂点の役割（role）

役割は **ss_seed で指定される固定属性**であり、次数からは決めない。

| role     | 定義                              | 備考 |
|----------|-----------------------------------|------|
| feed     | プロセスのソース（入アークなし）   | 入力スペック設定の対象はこれのみ |
| product  | 製品の終端 sink（出アークなし）    | **固定。ユニットに所有されないので削除されない** |
| residue  | 廃棄の終端 sink（出アークなし）    | 同上 |
| internal | 流れの途中（膜の透過・残渣を含む） | ユニットの入口・出口はすべて internal |

**重要**：膜の出口（透過・残渣）は internal であり product ではない。製品・廃棄は固定の sink 頂点。
これにより、どの膜を削除しても製品の定義（sink）は壊れない。

**測定点**：Aspen は「出アークなし・Mixerなし・ユニットなし」の頂点をストリーム化しない。
したがって **sink の手前に必ず Mixer を置いて sink をストリーム化**し（4節の Mixer 規則に sink を含める）、
**純度・回収率は product sink のストリームで測る**。複数の流れが product sink に合流しても Mixer が集約するので、
段を追加しても測定点は product sink で固定される。

> 入次数・出次数は role 判定には使わず、補助ユニット導出（4節）にのみ使う。

### 3.5 アークの3値（最適化変数との対応）

| 状態       | アーク辞書での表現        | MINLPでの役割 |
|------------|---------------------------|---------------|
| アークなし | キーが存在しない          | （なし） |
| 固定アーク | `candidate` キーなし       | 定数（常時アクティブ） |
| 候補アーク | `{"candidate": "q_k"}`     | バイナリ最適化変数の1つ |

加えて、ユニットの `params` のうち `unit_registry` で最適化変数に指定されたもの（膜なら area, p_permeate, permeance_CO2）が連続変数 x に対応する。

つまり**アーク辞書＋units は MINLP の記号的定義そのもの**。SS を書き下すと変数リストが確定する。

**候補ラベル `q_k` の採番**は頂点IDと同じ規則：**過去に払い出した `q` 番号の最大＋1**で振り、削除しても再付番・再利用しない。これにより、変数ベクトルのバイナリ部の並び順（5.1節「binary_variables の順」）も `q` ラベル昇順で決定論的に定まる。

### 3.6 アーク型の語彙（aspen_builder が参照）

`feed` / `membrane_permeate` / `membrane_retentate` / `product` / `residue` / `compressor` /
`expander`（膨張機 EXP の内部アーク） / `recycle` / `process`。
`aspen_builder` はこの `type` でブロック生成と接続を分岐する。`product`/`residue` 型は sink 行きを示すが、
役割は頂点の role が正であり、arc 型は builder 用の補助情報。

### 3.7 ディスク上の JSON スキーマ

JSON はタプルキーを持てないため、アークは**リスト形式**で保存する。

**ss_seed.json / ss_current.json**
```json
{
  "iteration": 0,
  "vertices": {
    "V0": {"role": "feed",     "label": "Feed (CO2 13%)"},
    "V1": {"role": "internal", "label": "MEMB1 inlet"},
    "V7": {"role": "product",  "label": "Product"},
    "V8": {"role": "residue",  "label": "Residue"}
  },
  "arcs": [
    {"from": "V0", "to": "V1", "type": "feed"},
    {"from": "V1", "to": "V2", "type": "membrane_permeate", "unit": "MEMB1"},
    {"from": "V6", "to": "V1", "type": "recycle", "candidate": "q_1"}
  ],
  "units": {
    "MEMB1": {"type": "MEMB", "inlet": "V1",
              "outlets": {"permeate": "V2", "retentate": "V3"},
              "params": {"area": 20000.0, "p_permeate": 0.2}}
  },
  "history": []
}
```

ケース定数（feed・目標・ペナルティ・最適化器のハイパラ・VP出口圧）は `ss_*.json` に**入れない**。`case.yaml`（3.8節）に置く。

**id_counters（採番カウンタ・任意キー）**：3.1節の「削除しても再付番・再利用しない」を削除後も保証するため、
`ss_current.json` は `"id_counters": {"vertex": <過去に払い出した最大番号>, "candidate": <同・q番号>}` を持つ。
現存要素の最大値ではなく**過去に払い出した最大値**から +1 で採番する（最大番号の頂点/候補を削除→再追加しても
同じ ID が別の流れに再割当されない）。キーが無いファイル（`ss_seed.json` 含む）は読み込み時に現存最大から
初期化されるので人間が書く必要はない（`topology.allocate_vertex_id` / `allocate_candidate_id`）。

**results.json**（run_iteration.py が書く・signals.py と Claude が読む正本）
```json
{
  "iteration": 1,
  "optimizer": "bo",
  "seed": 1,
  "performance": {"CO2_purity": 0.0, "CO2_recovery": 0.0,
                  "specific_energy_kWh_tCO2": 0.0, "total_compressor_kW": 0.0,
                  "cost_usd_per_tCO2": 0.0},
  "optimal_params": {"q_1": 0, "MEMB1_area": 0.0, "MEMB1_p_perm": 0.0, "MEMB1_perm": 0.0},
  "active_candidates": {"q_1": 0},
  "stream_results": {"V0": {"CO2_molfrac": 0.0, "CO2_moleflow": 0.0, "description": "..."}},
  "energy_breakdown": {"VP1": 0.0, "VP2": 0.0},
  "gen_log": [{"gen": 1, "best_fitness": 0.0, "phase": "bootstrap"}],
  "n_evaluations": 0
}
```

**ss_change.json**（Claude が書く・apply_ss.py が読む。ユニット単位。6.2節）

### 3.8 case.yaml（ケース定数・反復で不変）

```yaml
feed:
  flowbase: MASS            # 注: 書き込みは実機で無効・TOTFLOW はモル [kmol/h] として効く（10.3）
  totflow: 80307.0          # kmol/h（Lee の 500 Nm³/s）
  co2_frac: 0.13            # post-combustion。DAC にするならここを変える
  temp: 25.0                # °C
  pressure: 1.0             # bar
  basis: MOLE-FRAC          # 組成基準（FLOWBASE=MASS との組合せが動作実績）
optimization_targets:
  purity_min: 0.95
  recovery_min: 0.90
  objective: minimize_cost  # 10.2。minimize_specific_energy でエネルギー目的
penalty_weight: 100000.0    # PENALTY_W（YAML1.1 で 1.0e5 は文字列になるため整数表記）
economics: {...}            # 10.2 の経済定数
vp_outlet_pressure: 1.0     # bar。auto-VP の出口圧（固定）
membrane_model: {...}       # 10.1 の Robeson 膜モデル
aspen_timeout_eval: 60      # 秒。内側ループ中の per-x タイムアウト
aspen_timeout_detail: 120   # 秒。best 詳細抽出のタイムアウト
optimizer: bo               # 内側最適化器の切替: ga | bo
ga:
  pop_size: 20
  n_gen: 50
bo:
  n_init: 24
  n_iter: 60
  q_batch: 4
  patience: 10
max_binary_variables: 8     # バイナリ候補の上限（2^n 爆発の管理。総数上限はない・6.0節）
min_variables: 4            # 縮退防止の目安（ハード制約ではない）
# 任意キー（省略時はコード既定値）:
#   recovery_physical_max: 1.02   # 質量収支ガードの閾値（simulator.py）
#   subprocess_stall_sec: <秒>    # wedge 判定の手動上書き（subprocess_evaluator.py）
```

---

## 4. 補助ユニットの自動導出

Mixer・Splitter・昇圧機（VP）・冷却器は**アーク辞書に明示エントリを持たない**。具体トポロジーの構造から決定論的に導出する。

| 補助ユニット | 導出条件 |
|--------------|----------|
| Mixer        | 次のいずれか（具体トポロジー上で判定）：(1) **膜の入口頂点**（膜アークの src）、(2) **入次数 2 以上**の頂点、(3) **sink 頂点**（product/residue。ストリーム化＝測定点確保のため）、(4) **パススルーアーク（feed/process/recycle。ユニットなし）で給餌されるユニット入口頂点**（前混合器→COMP→膜入口の配置で入口ストリームが孤立しないため）。(1) があるので、リサイクル候補が OFF でも膜入口に着地点が常に在る。Aspen は1入力 Mixer を許容するため、入力1本でも問題ない |
| Splitter     | ある頂点からの出次数が 2 以上。**ただし builder は未実装**（FSplit を作れない）。非膜頂点の出次数 ≥2 の具体トポロジーは `is_buildable` が評価前に弾く。分岐は「相互排他な候補トグルペア」で表現する（`algorithm/CLAUDE.md` のリサイクル則）。`topology.splitter_vertices` は導出関数として存在するが将来 FSplit 実装時用 |
| 昇圧機 (VP)  | 各膜の permeate 側に自動挿入。透過側を膜の `p_permeate` から `case.yaml.vp_outlet_pressure`（1 bar）へ昇圧する操作。この昇圧仕事（WNET）が比エネルギーに算入される。**VP は最適化変数を持たない**（入口=p_permeate、出口=1bar固定）。**命名**：VP{n} の `n` は対応する膜の数値部分をそのまま流用（`MEMB3`→`VP3`）。膜を削除すると VP も連動して消え、残った膜が `MEMB1, MEMB5` であれば VP は `VP1, VP5`（欠番OK・再付番しない。頂点ID/候補IDの採番規則と一貫させる) |
| 冷却器（自動） | **auto-VP と明示 COMP の出口に 35°C 冷却器（Heater・圧損なし）を自動挿入**。中間冷却が無いと圧縮熱が下流へカスケードして動力が膨張し、高分子膜の許容温度も超えるため、工学的標準装備として builder が面倒を見る。最適化変数なし・duty は電力でないため energy 非算入（冷却水コストは Lee もモデル外）。命名 `HXV{n}`（VP 側）/`HXC{n}`（COMP 側）。膨張機（EXP）はガスが冷える方向なので冷却器なし |

- 導出・検査は **具体トポロジー**（q 固定後）に対して行う。
- COMP（圧縮機）は自動付与しない。必要なら Claude が `add_unit`/`add_gated_unit` で明示的に追加する（自前の `outlet_pressure` を持つ実ユニット。auto-VP とは別物）。
- feed 頂点のみが入力スペック設定の対象。

---

## 5. 内側問題：固定超構造上の MINLP

### 5.1 変数

- バイナリ `q ∈ {0,1}^m`：候補アークの ON/OFF。m = 候補アーク数。
- 連続 `x`：`unit_registry` が各ユニット種別から自動生成（膜は area, p_permeate, permeance_CO2）。

変数ベクトルは SS から動的生成：`[q_0..q_{m-1} | x_0..x_{n-1}]`。**並び順は決定論的**——
バイナリは `binary_variables` の順、連続は `continuous_variables` の順（`unit_registry.make_ga_variables` が生成する順）。

### 5.2 評価（ブラックボックス、抽象境界経由）

性能指標は Aspen がブラックボックスとして計算する。内側最適化器は Aspen を直接 import せず、`evaluator.py` の境界を経由する。

```python
# evaluator.py — バッチ評価を保つため「具体トポロジー単位」の境界にする
class Evaluator(Protocol):
    def evaluate_topology(self, topology, x_list) -> list[Metrics]: ...
    # 同一トポロジーを1回構築し、x_list を順に流して評価する
    def evaluate_detailed(self, topology, x) -> DetailedResult: ...
    # best解の詳細抽出（results.json 用）。stream_results まで返す
```

- `Metrics`（最適化中の各 x、fitness 計算に必要な分）＝ `(specific_energy, purity, recovery, energy_breakdown, hx_area_m2)`。
  `energy_breakdown`（VP/COMP ごとの WNET）まで含めるのは、ビルド時に auto_vps/energy_blocks を内部で確定できるから。
- `DetailedResult`（best 解1点のみ）＝ Metrics ＋ 各頂点の `stream_results`（CO₂ molfrac/moleflow/pressure）。`results.json` の元になる。
- 2経路に分ける理由：最適化ループ中は fitness 用に Metrics で足り、頂点ごとのストリーム抽出は best 解1点だけでよい。
- `AspenEvaluator`（simulator.py 側）が実装：`build_aspen_from_epnt` で1回構築し、各 x に `set_continuous_variables` して再収束。`auto_vps`・energy_blocks はこの内部で導出し、境界の外に出さない。
- 将来サロゲートに差し替える際は別実装を注入するだけ。テストはモック注入。

**本番経路はプロセス隔離**：`run_iteration.py` が実際に注入するのは
`SubprocessEvaluator`（subprocess_evaluator.py）。評価グループごとに使い捨て子プロセス
`aspen_worker.py` を spawn し、その中で `AspenEvaluator` が動く。子は結果を 1 件ずつ stdout に
JSON で流し、親は「最後の受信からの経過」で wedge（返らない in-flight COM 詰まり）を検知して
子ごと kill → 未受信分を `Metrics.bad()` で埋めて前進する。親プロセスは COM に一切触れない。
`aspen_watchdog.py` は子内の out-of-band ハング検知（本番経路ではスレッド未起動＝no-op のフォールバック）。
障害注入の耐久テストは `tests/test_soak_chaos.py`（`CHAOS_GROUPS` で規模指定）。

**質量収支ガード**：リサイクルの tear が「収束」と報告しても回収率 > 1 の非物理点に落ちることがある。
`recovery > recovery_physical_max`（既定 1.02）と、圧力機器があるのに総動力 ≤ 0 の点は BAD として弾く。

### 5.3 目的関数

```
f(q, x) = J + λ [ max(0, π_min − π)² + max(0, ρ_min − ρ)² ]
J = 回収コスト [$/tCO2]（10.2）または 比エネルギー [kWh/tCO2],  π = 純度,  ρ = 回収率
```

λ は記録用 fitness にのみ使う（CBO は獲得関数で制約を直接扱う）。

### 5.4 GA（ga.py・切替可能な内側最適化器）

- **Mixed GA（DEAP）**。交叉＝バイナリは一様交叉・連続は blend、変異＝バイナリはフリップ・連続はガウシアン＋境界クリップ。選択トーナメント。
- **バッチ評価（必須）**：集団をバイナリキーでグループ化し、トポロジー1個につき構築1回。連続 x はパラメータ更新で使い回す。
- **クラッシュ吸収**：Aspen 非収束・COMクラッシュ・タイムアウト・ビルド失敗は有限の `BAD_VALUE`（1e6）。
- 再現性：乱数 seed を反復番号で固定（使用した optimizer と seed は results.json に記録される）。

### 5.6 BO（bo.py・既定の内側最適化器）

`case.yaml` の `optimizer: bo` で **BoTorch Constrained BO** を使う。

- `run_bo(ss, case, evaluator, seed)` は `run_ga` と同一シグネチャ・同一の Evaluator 境界。
  バイナリキーでのグループ化・`is_buildable` ガード・`BAD_VALUE` 吸収も GA と共通の規律。
- 3 outcome（目的 J / purity / recovery）を別 GP で学習し、`qLogExpectedImprovement` に
  constraints（purity ≥ purity_min, recovery ≥ recovery_min）を渡す **Constrained EI**。
  bad 観測は目的値を有効観測最大の2倍にクリップして infeasible として学習させる。
- **best の選択は feasible 優先**：feasible 観測があればその中の J 最小。無ければ
  **制約不足量が最小の観測**を返す（同率は penalty 込み fitness で tie-break）。`bootstrap: off` のときのみ
  penalty 込み fitness 最小にフォールバックする。
- **二相式**：feasible 観測がゼロの間は CEI の P(feasible) が平坦化し実質目的最小化器に縮退するため
  （高エネルギー側の feasible 盆地へ到達しない）、**第1相＝制約不足量のみを単一 GP + qLogEI で最小化**
  （目的無視・λ 不要）し、初の feasible 観測から第2相＝CEI に切替える。`gen_log` 各要素の `phase` キー
  （"bootstrap"/"cei"）で診断可能。`bo: bootstrap: off` で常時 CEI。
- **実行時 bad のリトライ**：一時的 COM wedge が purity=0/recovery=0 の偽 infeasible として GP を汚染するのを防ぐため、
  ビルド可能なのに bad の x は同一トポロジーで `retry_bad` 回（既定1）再評価する。`bo: retry_bad: 0` で無効化。
- **ビルド不能な組合せの事前除外**：トグル両 ON 等の構造的 unbuildable なバイナリ組合せは
  `fixed_features_list` から除外し、探索予算を希釈しない。
- **フェーズ対応 patience**：判定軸をフェーズ別（bootstrap=min_shortfall／CEI=feasible J）に持ち、
  相切替でカウンタをリセットし、`n_iter/3` を床として、`patience` 回連続で無改善なら打ち切る。`patience: 0` で無効。
- **対数スケール化**：bounds 比が 50 倍を超える正の連続変数は GP/獲得関数/Sobol の内部で log 変換する
  （細い盆地が線形正規化で潰れるのを防ぐ）。現行 bounds（10.3）では比が 50 未満なので発火しない（保険）。
- **注意（既知の軸ずれ）**：`gen_log` の `best_fitness` は互換のため「全観測の penalty 込み最小」
  のままで、返される best（feasible 優先）と軸が異なる。停止判定の扱いは `algorithm/CLAUDE.md` 参照。
- GP fit / 獲得関数最適化が失敗した反復は Sobol サンプリングにフォールバックしてループを継続する。

---

## 6. 外側問題：超構造遷移（SST、Claude が回す）

### 6.0 設計原則：遷移は変数空間の「移動」であって「拡大」ではない（最重要）

SST の本質は探索空間を**広げる**ことではなく**動かす**ことである。素朴に反復ごとにユニット・候補を足すと変数が単調増加し、
「巨大 SS を一括で解く」のと同じ罠（変数空間の爆発）に陥る。これを避けるため、次を不変条件とする。

- **各遷移は削除と追加をセットで行う。** 役目を終えた変数（境界に張り付いた＝探索済み、収束して動かない、WNET≈0 で機能していない）を削り、未探索の構造方向を同程度足す。
- **上限は構造決定用のバイナリ変数（候補アーク）の数のみで規定する：`case.yaml.max_binary_variables`**。
  反復を通じて膨張トレンドを作らない。多少の増減は許容するが、単調増加させない。
  バイナリだけを絞るのは、候補が増えると探索すべきトポロジーが 2ⁿ で増えるため（連続は
  ユニットに付随して増減するだけで、1トポロジーあたりの内側最適化が重くなるのみ＝組合せ爆発
  しない、という非対称性）。連続増による BO の収束鈍化は `bo.n_init` 側で調整する。
- **下限 `case.yaml.min_variables`** は縮退防止の目安。遷移は続けてほしいので、変数を削りすぎて探索が死なないようにする。これは提案を弾くハード制約ではなく、削除と追加で「移動」させ続けるための方向性。
- **構造下限（膜1段必須などの最小骨格）は設けない。** プロセスとして破綻する提案（孤立流れ・feed→product 不達など）は `validate` が弾くので骨格固定は不要。骨格を固定しない方が「事前に想定していなかった構造を提案する」という手法の主張と整合する。
- 結果として、各反復で解く MINLP は小さいまま（解けるサイズ）でありながら、遷移を重ねることで探索が届く構造空間は広くなる（広い想定範囲）。「解けるサイズ」と「広い想定範囲」の両立が本手法の主張。

> この原則は `algorithm/CLAUDE.md` の運用ルールにも反映する。Claude は提案のたびに「変数を増やし続けていないか」を自己点検する。

### 6.1 1反復の手順（Claude の手番）

```
1. runs/<name>/iterations/iter_NNN/results.json を読む
2. シグナルを抽出（境界張り付き／エネルギー支配／不活性候補／制約違反）
3. ss_change.json を「削除＋追加のセット」として書く
4. uv run python src/apply_ss.py --base-dir runs/<name> --iter N
5. uv run python src/run_iteration.py --base-dir runs/<name> > runs/<name>/iter_(N+1)_log.txt 2>&1
6. run_iteration.py 末尾の auto-commit を確認し、1 に戻る
```
（SST セッションは `algorithm/` で開き、コマンドはすべてそこから実行する。run データは
`algorithm/runs/`＝gitignore 済み。algorithm/ は自己完結な実行単位で、`.claude/settings.json`（自動実行許可）と
`.claude/commands/` も同梱する。）

詳細な運用ルール（自律・停止条件・ロールバック判断）は `algorithm/CLAUDE.md` に定義する。

### 6.2 提案はユニット単位で記述する

アーク単位の差分は漏れ・事故が多いため、提案はユニット単位で書く。
`apply_ss.py` が `topology.py` のユニット所有権を使ってアークレベルへ展開する。

```json
{
  "reason": "MEMB2_p_perm が下限 0.1 に張り付き → 残渣側に回収段を追加",
  "operations": [
    {"op": "add_unit",          "unit_type": "MEMB", "unit": "MEMB3", "inlet": "V2", "permeate_to": "V4", "retentate_to": "V8", "params": {...}},
    {"op": "delete_unit",       "unit": "MEMB2"},
    {"op": "add_arc",           "from": "V6", "to": "V1", "type": "recycle", "candidate": true},
    {"op": "promote_candidate", "candidate": "q_1"},
    {"op": "set_bounds",        "unit": "MEMB1", "param": "area", "bounds": [100, 800000]}
  ]
}
```

操作の意味：
- `add_unit`：ユニットを追加する。**inlet（既存頂点）と各 outlet の接続先を明示する**。`apply_ss` が `unit_registry` の構造テンプレートに従って出力頂点を新規採番・生成し、所有権を登録する。
  膜の例：
  ```json
  {"op": "add_unit", "unit_type": "MEMB", "unit": "MEMB3", "inlet": "V2",
   "permeate_to": "V4", "retentate_to": "V8", "params": {...}}
  ```
  これは「V2 を入口とする膜 MEMB3 を追加。permeate 側出力を V4 へ、retentate 側出力を V8（residue sink）へ繋ぐ」。
  `apply_ss` の展開：新頂点 Vp, Vr（新ID, role=internal）を作り、アーク `(V2,Vp) membrane_permeate MEMB3`・`(V2,Vr) membrane_retentate MEMB3`・`(Vp,V4) process`・`(Vr,V8) process` を追加。V2 から V4 への既存直結アークがあれば削除する。`units["MEMB3"]={inlet:V2, outlets:{permeate:Vp, retentate:Vr}, params}`。
  出力数とポート名（permeate/retentate 等）はユニット種別ごとに `unit_registry` の構造テンプレートが定義する（膜=2出力、COMP=1出力）。**「アーク上に挿入」ではなく「inlet と各出力先を明示」**することで、1入力多出力ユニットの結線を曖昧さなく確定する。
- `add_gated_unit`：ユニットを**「内側最適化器が on/off を決めるトグル」**として追加する。`add_unit` が常時 engaged の固定追加なのに対し、本オペは engagement をバイナリ候補に帰属させる＝**構造を最適化変数にする**（6.0 の理想形）。
  ```json
  {"op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMB3",
   "feed_from": "V5", "bypass_to": "V7", "permeate_to": "V7", "retentate_to": "V8", "params": {...}}
  ```
  これは「MEMB2 透過 V5 の流れを『MEMB3 へ ⇄ そのまま V7 へ（バイパス）』のトグルペアにする」。`apply_ss` の展開：専用の新インレット頂点 Vin を作り、膜本体（`(Vin,Vp) membrane_permeate`・`(Vin,Vr) membrane_retentate`・`(Vp,V7) process`・`(Vr,V8) process`）を固定で張る。給餌 `(V5,Vin)` とバイパス `(V5,V7)` を**ともに候補化**（相互排他のトグルペア、バイナリ2本）。既存の固定 `(V5,V7)` は型を引き継いで候補化する。
  **要点**：給餌候補 OFF → Vin の入次数0 → `active_topology` の dead-unit pruning が MEMB3 を刈り取る＝「ユニットなしのクリーン下位構造」。これで内側最適化器が「段あり ⇄ 段なし」を公平評価できる。`feed_from` は「インターセプトする1本の流れ」の源であること（他に固定出アークが残ると候補 ON 時に出次数>1 で `is_buildable` が弾く＝リサイクルのトグルペアと同じ規律）。
- `delete_unit`：ユニット削除（6.3節のルール）。
- `add_arc`：アーク追加。`candidate:true` ならバイナリ変数を1つ増やす。
- `delete_arc`：`(from, to)` で指定したアークを1本削除する。unit 所有アーク（膜の permeate/retentate、COMP の内部アーク）は単体削除すると装置構造が壊れるので拒否し、`delete_unit` を使わせる。削除で頂点が孤立・行き止まりになる場合は適用後の `validate` が弾く。`promote_candidate` で固定昇格した非 unit アークもこれで削除でき、昇格は不可逆ではない（必要なら `delete_arc` → `add_arc(candidate:true)` で候補へ戻せる）。
- `promote_candidate`：常時アクティブだった候補を固定アークに昇格。
- `set_bounds`：連続変数の境界を上書き（人間・開発用。自律ループでは使わない）。**保存場所**：`units[name]` 内に `bounds_override: {param_name: [lo, hi]}` を持たせる（SSテンプレートの一部として永続化）。`unit_registry.make_ga_variables` は `bounds_override` があれば `UNIT_BOUNDS` の既定より優先して採用する。`delete_unit` 時に `bounds_override` ごと消える。

**固定パラメータ**：`UNIT_BOUNDS` で lo==hi のパラメータ（ブロワー campaign の `COMP.outlet_pressure` = 1.1 bar）は最適化変数にならず、
`params` の値がそのまま実機に届く。`apply_ss` は省略時に固定値を補い、異なる値の書き込みを拒否する。

### 6.2.1 適用後の検証と例外停止

`apply_ss.py` は操作を適用した直後に `topology.validate(new_ss)` を呼ぶ。違反（孤立頂点・feed→product 不達・ユニット所有権の不整合など）が見つかった場合は**例外で停止**し、**`ss_current.json` は更新しない**。`ss_before_change.json` は適用前に必ず退避済み（7節）なので、Claude はそこから差し戻すか別提案を出せる。半端な状態でライブSSが壊れることを防ぐ最終ゲート。

### 6.3 ユニット削除のルール

- 流入：削除ユニットの入口に入ってくる流れは **residue sink へ向ける**（別接続先が要るなら候補アークで導入）。
- 流出：削除ユニットの出力頂点とそれに触れるアークは**単純に削除**。バイパス再接続は不要。
- ユニット採番は減らさない。削除しても番号は欠番、次の追加は新番号。

### 6.4 ユニット所有権

`units` 辞書の `inlet` / `outlets` が各ユニットの所有頂点を記録し、`delete_unit` を曖昧さなく解決する。

---

## 7. 「止まらない」ための仕組み

**専用の orchestrate.py は作らない**。

| 仕組み | 実現方法 |
|--------|----------|
| プロセス使い捨て | `run_iteration.py` は毎回新プロセス。クラッシュ時は `taskkill` |
| **評価のプロセス隔離** | `SubprocessEvaluator` が評価グループごとに使い捨て子プロセス（`aspen_worker.py`）を spawn。返らない in-flight COM 詰まり（wedge）は「最後の結果受信からの経過」で検知し**子ごと kill**（`TerminateProcess` は COM の詰まり方に依存せず必ず効く）。受信済みの結果は保全、未受信分は `Metrics.bad()`（5.2節） |
| best 詳細評価のリトライ | best 1 点の詳細評価が一過性の wedge で失敗した場合、`run_iteration` が自動で再評価する（測定の回復であって probe ではない） |
| 途中再開 | run はスクラッチ前提。`iterations/` から次 iter を自動検出 |
| ロールバック | **事前 can_build テストはしない**。内側最適化の結果が全個体ペナルティ等で使い物にならなければ、Claude が前の SS に差し戻すか別提案を出す（`algorithm/CLAUDE.md` の判断ルール）。差し戻しのため、**`apply_ss.py` は適用前に必ず `ss_current.json` を `iterations/iter_NNN/ss_before_change.json` に退避する**。**退避先が既に存在する iter への再適用は既定で拒否**（リトライでロールバック起点が上書き消失するのを防ぐ。意図的な再適用は `--force`）。**さらに `apply_ss.py` は適用末尾で `validate` を呼び、違反時は例外で停止し `ss_current.json` を更新しない**（6.2.1） |
| 停止条件 | 連続3反復改善なし／Aspenクラッシュ2連・収束失敗3連／ユーザ停止指示（`algorithm/CLAUDE.md`） |

---

## 8. ディレクトリ構成とファイルの役割

```
LLM4ChemPro/
├── CLAUDE.md                  開発用：コードを書く・直すための指示
├── ARCHITECTURE.md            本ドキュメント（設計の単一の真実）
├── pyproject.toml             uv プロジェクト・依存定義（人間が管理）
├── .gitignore                 runs/ を除外（結果は版管理しない）。git ルートはこの階層
│
└── algorithm/                 自己完結な実行単位
    ├── CLAUDE.md              SST agent用：results を読み新SSを提案し、ループを回す
    ├── ss_seed.json           初期SS（人間が与える。変更禁止）
    ├── case.yaml              ケース定数（人間が管理）
    │
    ├── src/
    │   ├── topology.py        【核】アーク辞書・active_topology・validate・
    │   │                        補助ユニット導出・所有権・load_ss/save_ss・to_matrix
    │   ├── unit_registry.py   種別→最適化変数+境界、構造テンプレート、Robeson 膜モデル
    │   ├── evaluator.py       評価の抽象境界（Protocol・Metrics・BAD_VALUE）とコスト目的関数
    │   ├── subprocess_evaluator.py 本番の Evaluator 実装＝プロセス隔離スーパーバイザ
    │   │                        （子 spawn・結果ストリーム受信・wedge 検知・kill。5.2節）
    │   ├── aspen_worker.py    使い捨て子プロセスのエントリ（中で AspenEvaluator が動く）
    │   ├── aspen_watchdog.py  子内 out-of-band ハング検知（本番経路では no-op のフォールバック）
    │   ├── aspen_builder.py   具体トポロジー→Aspen COM 構築・補助ユニット実体化
    │   ├── simulator.py       Aspen実行・クラッシュ検知・タイムアウト・AspenEvaluator
    │   ├── ga.py              run_ga（Mixed GA・バッチ評価）
    │   ├── bo.py              run_bo（BoTorch Constrained BO。5.6節。既定）
    │   ├── signals.py         results→シグナル抽出（境界張り付き等）
    │   ├── run_iteration.py   内側ループ駆動：SS読込→ga/bo→results保存→auto-commit
    │   ├── apply_ss.py        ss_change(ユニット単位)→SS更新。topology を使う
    │   └── YAspen/            Aspenアーカイブ（コンパイル済みACM入り）。src/ 直下に置く
    │       └── Yaspen.apw       （run_iteration.py が dirname(__file__)/YAspen で参照）。これが無いとビルド不能
    │
    ├── tests/                 Aspen不要の単体テスト（unittest。discover で一括実行）
    │   ├── test_topology.py    active_topology / validate / 導出 / 往復 / 採番 / pruning
    │   ├── test_apply_ss.py    提案→SS更新（ユニット単位の展開・安全ガード・再適用ガード）
    │   ├── test_bo.py / test_ga.py / test_cost.py / test_run_iteration.py / test_signals.py / test_simulator_metrics.py
    │   ├── test_subprocess_evaluator.py / test_aspen_watchdog.py   評価境界の故障系
    │   └── test_soak_chaos.py  障害注入・耐久（_chaos_worker.py が故障を注入。CHAOS_GROUPS で規模）
    │
    ├── .claude/               SST セッション用の設定（algorithm/ で開く）
    │   ├── settings.json      自動実行の許可（uv run・runs/ への Write 等。case.yaml/ss_seed は deny）
    │   └── commands/
    │       ├── sst-analyze.md /sst-analyze（最新 results を分析→ss_change 提案・実行なし）
    │       └── sst-loop.md    /sst-loop（SST 外側ループを自律実行・任意の最大反復数）
    │
    └── runs/                  run データ（gitignore。SST セッションの作業領域）
        └── <name>/
            ├── ss_current.json            ライブSS状態（apply_ss.py が編集）
            ├── iterations/iter_NNN/
            │   ├── ss_snapshot.json        この反復で使ったSS
            │   ├── results.json            最適化結果＋ストリーム/エネルギー（正本。optimizer/seed 記録）
            │   ├── eval_log.jsonl          内側の全評価点（bits・purity・recovery・obj）
            │   ├── ss_change.json          （Claudeが書く）次SSへの差分
            │   └── ss_before_change.json   （apply_ss が退避）ロールバック起点
            ├── iter_NNN_log.txt            内側ループの生ログ（失敗時のみ参照）
            └── HANDOFF.md                  停止時の引き継ぎ（Claude が書く）
```

`run_iteration.py` の repo_root は `algorithm/` の親＝プロジェクトルート（git ルート）。`runs/` は gitignore されるため
auto-commit はコード・文書の変更のみを拾う。

### データフロー

```
[Claude=外側] results.json を読む → signals → ss_change.json を書く
                                                      ↓ apply_ss.py
                                                  ss_current.json 更新（topology）
                                                      ↓ run_iteration.py（内側）
        ga / bo ──→ SubprocessEvaluator（親・COM非接触）
         ↑               ↓ 子プロセス spawn（評価グループごと使い捨て）
         │          aspen_worker ──→ simulator(AspenEvaluator) ←── aspen_builder
         │                                                              ↑
     topology.active_topology(q) + unit_registry                  具体トポロジー
         └──→ results.json を書く ──→ [Claude=外側] へ戻る
```

`topology.py` が全体の中心（single source of truth）。

---

## 9. Aspen 連携の制約

- **ブロック名・ストリーム名にアンダースコアを使わない**（Aspen が拒否する）。命名は `V{i}`, `VS{i}T{j}`, `MEMB{n}`, `VP{n}`, `DV{i}`。
- feed の設定（FLOWBASE/TOTFLOW/CARBO-01/NITRO-01/BASIS/TEMP/PRES）はモデル構築後に `case.yaml.feed` から一括で適用する。
  builder は feed の既定値を持たない。
- `Reinit()` を `Run2()` 前に呼んで前回結果をクリアする（キャッシュ汚染を防ぐ）。
- `YAspen/Yaspen.apw` に ACM（GasPermModule）が埋め込まれている。apw を再保存する場合は ACM 同梱を確認。
- Mixer の出口圧は `PRES=0`（入口最小圧に追従）。固定値にすると、feed を昇圧しても膜入口の Mixer が圧を落として COMP を無効にする。
- 単位系：この .apw では動力（WNET）は kW、熱量（QCALC）は cal/s で返る（`simulator.py` が換算する）。
- git ルートはプロジェクト全体とし、`runs/` は `.gitignore` で除外する（結果は版管理しない）。

---

## 10. ケース固有の設計（膜分離 CO₂ 回収・石炭火力排ガス）

物理・経済設定は先行研究（Lee et al., J. Membr. Sci. 563 (2018) 820–834：膜カスケードの経済最適化、
目標 purity 95%/recovery 90%）に整合させる。

### 10.1 Robeson 膜モデル（段別膜物性の変数化）

膜物性を固定パラメータではなく**最適化変数**とする。ただし自由な2変数ではなく、
**Robeson 上限（CO2/N2）のトレードオフ曲線上の1自由度**として表現する（架空の万能膜を排除）。

- **変数の向き**：CO2 透過速度 `permeance_CO2` を連続変数とし、選択率を相関式で導出：
  `α = (k / Q[GPU])^(1/n)`、`permeance_N2 = permeance_CO2 / α`。
  係数は Robeson 2008 CO2/N2 上界＝Lee et al. (2018) Eq.21：
  **k = 3.0967×10⁸ [GPU]（膜厚 0.1 µm 換算）、n = 2.888**。
- **単位換算**：`1 GPU = 2.70677×10⁻³ m³(STP)/(m²·h·bar)`。`permeance_CO2 = 2.70677` は **1000 GPU**（α=50 の膜に相当し、上界より下。上界上の α(1000 GPU) ≈ 80）。
- **探索範囲**：**[500, 6000] GPU**（Lee の感度範囲 500〜5000 を含む。α は 43〜101 に対応）。case.yaml `membrane_model.permeance_bounds_gpu`。
- **2シナリオ**：(a) 全段同一膜（`tie: true`＝共有変数 `MEMB_perm` 1本、変数リスト先頭固定）と
  (b) 段別独立膜（`tie: false`＝各 MEMB に `{unit}_perm`）。切替は case.yaml。本ケースは (b)。
- **実装箇所**：unit_registry（`robeson_alpha`・`permeance_bounds_aspen`・`GPU_TO_ASPEN`）、
  topology.continuous_variables（membrane_model 引数・tie 変数）、simulator.build_unit_params
  （`MEMB*` 展開・N2 導出の純関数）、aspen_builder.set_continuous_variables の
  `L("CARBO-01")`/`L("NITRO-01")` 書き込み分岐、case.yaml `membrane_model:` セクション。
- エージェントは新ユニットの `params` に permeance を直指定しない（`algorithm/CLAUDE.md`）。

### 10.2 コスト目的関数（$/tCO2）

目的は**年間換算回収コスト**。エネルギーのみを目的にすると「膜面積がタダ」なため面積とエネルギーの
トレードオフが閉じない（ill-posed）。

- `F_obj = (capital_charge · f_in · C_TCC) / (M_CO2 · t_op) + E·Ce + ペナルティ`
  （Lee et al. 2018 Eq.15/16。`C_TCC = Σ Cm·A + Σ C_unit·|WNET| + Σ Chx·A_hx`）
  - 膜 CAPEX：50 $/m²（モジュール込み）。圧力機器：COMP 670 / VP 1341 / 膨張機 500 $/kW（膨張機は WNET<0 で判別）。
    熱交換器（自動冷却器）：300 $/m²。面積は Lee Eq.5/6（U=132.5 W/m²K、冷却水 20→25 ℃、向流 LMTD、ガス出口 35 ℃）で
    冷却器 duty から算出する。
  - 資本賦課率 20%/年 × 設置係数 f_in=1.6
  - 電力 OPEX：E [kWh/tCO2] × 0.04 $/kWh（年間稼働 7,446 h、`M_CO2 = recovery × feed CO2` で正規化）
  - ペナルティ：純度/回収 shortfall² 形式。係数はコストスケールの `economics.penalty_weight`（Lee の r=1000）。CBO は獲得関数に使わず記録用
  - **圧力機器 CAPEX は C_unit × |WNET|（電気動力そのまま・η で割らない）**。Lee Eq.16 の「W/η」は等エントロピー仕事→
    実動力の換算で、Aspen の WNET は既に実動力。Lee Fig.3/4 の4設計を再現計算し、論文 C_cap との差が −2% 前後で一致する
    （回帰テスト：`tests/test_cost.py::TestLeeReproduction`）。
- 経済定数は case.yaml `economics:` セクション（人間管理・Lee Table 1 の値）。
- **二相式 CBO との関係**：第1相（不足量最小化）は目的に依存しない。第2相の目的 outcome・best 選択・
  ロギング fitness がコスト軸になる。実装は **evaluator.cost_per_tco2（純関数）を optimizer 側（ga/bo）で合成**——
  Metrics・subprocess 直列化を変えずに済む。切替は `optimization_targets.objective: minimize_cost`。
- 比較可能性のため results.json の performance に energy と **`cost_usd_per_tCO2`** の両方を
  objective 設定に依らず常時記録する（signals にも表示）。

### 10.3 変数境界

- `p_permeate`：**[0.1, 0.99] bar**（真空ポンプの現実的範囲。0.99 は駆動力ゼロの回避）。
- `COMP.outlet_pressure`：ブロワー campaign では **[1.1, 1.1]（固定・変数にならない）**。
- 膜面積：段あたり **[1e5, 1.5e6] m²**（Lee の値）。feed 流量は Lee の 500 Nm³/s 相当（`totflow: 80307` kmol/h）。
  **feed 基準の実測事実**：case.yaml は `flowbase: MASS` を書き込むが実機では無効で、**TOTFLOW はモル流量 [kmol/h]
  として効く**。`evaluator.feed_co2_t_per_h` もモル解釈で換算する。
- 膜透過度：[500, 6000] GPU（10.1）。
- bounds 比は面積 15 / p_permeate 9.9 / 透過度 12 で、5.6 の対数スケール化は発火しない。
- 境界は人間が与える固定設定であり、エージェントは広げない（`set_bounds` は自律ループの外）。

### 10.4 圧力アーキテクチャ（ブロワー campaign）

- **シナリオ**：排ガス全量を昇圧しない「ブロワー＋真空」駆動。すべての膜入口を **1.1 bar** に統一する
  （seed の COMP1/COMP2 はブロワー。エージェントが段を追加するときも同じ形で COMP を置く）。
- **Mixer 出口圧**：builder の全 Mixer は `PRES=0`（入口最小圧に追従）。
- **リサイクル合流の圧整合**：auto-VP 出口は 1 bar 固定。全ストリームが 1.0〜1.1 bar に揃うため、任意の残渣・透過を
  任意の前混合器（ブロワー入口）へ戻しても圧の不整合は生じず、再昇圧 COMP は不要。
- **膨張機**：`EXP` ユニット型（Compr の TURBINE・`_create_expander`）。最適化変数なし（出口圧 params 固定・既定 1 bar）
  の構造部品。WNET は負値で energy_breakdown に載り、比エネルギー（回収控除）とコスト（500 $/kW CAPEX）に算入。
  本 campaign では圧力差が 1.1→1.0 bar しかなく、実質的に無意味（`algorithm/CLAUDE.md`）。
- **自動中間冷却**：auto-VP・COMP の出口に 35°C 冷却器を builder が自動挿入（4節の表参照）。冷却なしでは圧縮熱のカスケードで
  下流動力が膨張し、高分子膜の許容温度を超えるため、工学的標準装備として自動化する。
- **膜モデルの流動様式**：GasPermModule は完全混合セル。向流モジュールに比べ同一面積で回収率が低めであり、
  Lee の数値は絶対値ターゲットではなく参照値として扱う。
