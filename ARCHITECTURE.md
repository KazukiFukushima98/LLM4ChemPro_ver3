# ARCHITECTURE.md

本ドキュメントは LLM4ChemPro_ver3 の設計の単一の真実（single source of truth）である。
実装に着手する前に必ず全体を読むこと。設計に迷ったときは推測せず本ドキュメントに従う。
記載のない設計判断が必要になった場合は、勝手に決めず人間に確認すること。

**ver3 の位置づけ**：ver2（完成・タグ `v2.0-complete`）で確立した SST 手法——アーク辞書トポロジー・
二層最適化・ユニット単位の SS 遷移・二相式 Constrained BO・プロセス隔離の耐障害基盤——を**凍結したまま
継承**し、物理・経済設定を先行研究（膜カスケードの経済最適化・石炭火力 CCS）に整合する現実的なものへ
拡張する。手法（2〜11節）は ver2 から不変。**ver3 の新規スコープは 12 節**に定義する：
Robeson 上限に沿った段別膜物性の変数化／コスト目的関数（$/tCO2）／現実的 bounds／圧力アーキテクチャ。
ver2 の実証結果（高目標 95/90 に無人5遷移で到達、3段構造の創発）は ver2 リポジトリの
`docs/experiment_log.md` を参照。

> 歴史的経緯：ver2 は旧プロジェクト LLM4ChemPro（ver1）の全面リファクタで、最大の変更点は
> トポロジー表現（整数隣接行列→安定文字列IDのアーク辞書＋実削除）だった。移植は ver2 で完了済み。

---

## 1. プロジェクトの目的

LLM駆動スーパーストラクチャ遷移（Superstructure Transition; SST）による化学プロセスの自動合成。
固定された超構造を仮定せず、最適化の結果を LLM が解釈して超構造そのものを反復的に組み替える。

ケーススタディは膜分離による CO₂ 分離プロセス（現状の feed は post-combustion 相当の CO₂ 15%、
将来 DAC 条件へ拡張可能）。**アルゴリズム本体はドメイン非依存**に保ち、膜・Aspen 固有の知識は
`aspen_builder.py` / `simulator.py` / `unit_registry.py` にのみ閉じ込める。

---

## 2. 二層最適化構造

```
外側（LLM / SST）   超構造そのものを変える     離散的・低頻度   ← Claude が回す
    ↑ 各SSの最適解と性能シグナルを見て次のSSを決める
内側（GA / MINLP）  固定された超構造の中で解く   パラメトリック   ← run_iteration.py
```

- **内側**：ある反復で超構造は固定。バイナリ変数 q と連続変数 x を Mixed GA（または Constrained BO、5.6節。`case.yaml` の `optimizer` で切替）で同時最適化する MINLP。
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
- **新頂点の採番は「既存IDの数値部分の最大＋1」**とする（頂点の個数＋1ではない）。
  例：`{V0..V8}` に追加 → V9。その後 V9,V10 を削除して再度追加 → V11（V9/V10 は再利用しない）。
  個数ベースで採番すると削除後に既存IDと衝突し、安定IDの前提が崩れるので**必ず最大値ベース**にする。
  頂点集合は連番ではなく飛び番を含む文字列IDの集合になりうる。
- 実装はアーク辞書（辞書操作）で行う。隣接行列・S·A·Sᵀ+ΔA は論文記述用の数学的形式であり、コードには持たない。
  論文用に行列が必要なときは辞書から生成する（`to_matrix`）。

> 旧版との違い：旧版は `arcs: {"0,1": 1}` の整数インデックスで、削除は値を 0 にするだけ（頂点は事前確保・再付番なし）。
> ver2 は文字列ID＋実削除にする。これが ver2 の本丸。

### 3.2 用語：SSテンプレート と 具体トポロジー

- **SSテンプレート**：候補アーク（`q_k`）を含む超構造の定義。`ss_current.json` が保持するもの。
- **具体トポロジー**：バイナリ `q` を固定して候補を解決した、実際に Aspen で建てられる構造。
  `active_topology(q)` がテンプレートから生成する。**evaluator / aspen_builder が受け取るのは常に具体トポロジー**。
  具体トポロジーは「候補解決済みの3辞書 `{vertices, arcs, units}`」とする（3.3節と同じ形）。
  `aspen_builder` が必要とする `auto_vps`（自動VPの一覧）や energy_blocks（VP+COMP）は**ビルド時に builder/evaluator が内部で導出**するものであり、具体トポロジーには含めない。
  したがって評価境界の引数は具体トポロジーのみで足りる（5.2節）。
  なお旧 `build_aspen_from_epnt` は整数インデックスの `adj_matrix` + `arc_definitions` を取るが、ver2 では**安定文字列ID（`V4` 等）をそのまま Aspen ストリーム名に使う**ようポートする（IDにアンダースコアが無いので可。整数行列は廃止し、頂点集合を走査する）。
  **命名の責務境界**：`topology.py` が扱うのは頂点ID（`V7` 等）だけ。Aspen 固有の補助名（Mixer ブロック `MIXV{j}`、中間ストリーム `VS{i}T{j}`、VP の `VP{n}`/`VPI{n}`、FSplit 余剰 `DV{i}`）は **aspen_builder の内部に閉じる**。頂点ID＝Aspenストリーム名は同一に揃えるので、ID とストリームの混在は起きない。

### 3.3 メモリ上の3辞書

`topology.py` が保持する内部表現。

```python
vertices = {
    "V0": {"role": "feed",     "label": "Feed (CO2 15%)"},
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
              "params": {"permeance_CO2": 2.70677, "permeance_N2": 0.0541354,
                         "area": 20000.0, "p_permeate": 0.2}},
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

**測定点（重要・実装上の注意）**：Aspen は「出アークなし・Mixerなし・ユニットなし」の頂点をストリーム化しない
（旧 `_find_terminal_vertices` がスキップする）。したがって sink 頂点をそのまま測定点にはできない。
ver2 では **sink の手前に必ず Mixer を置いて sink をストリーム化**し（4節の Mixer 規則に sink を含める）、
**純度・回収率は product sink のストリームで測る**。複数の流れが product sink に合流しても Mixer が集約するので、
段を追加しても測定点は product sink で固定される。これにより旧版の `product_stream` 動的上書きは不要になる。
（旧コードは sink をストリーム化せず最終膜透過 `V5` で測っていた。ver2 はこの挙動を変更する＝builder のポート時に sink-Mixer を追加する。）

> 入次数・出次数は role 判定には使わず、補助ユニット導出（4節）にのみ使う。

### 3.5 アークの3値（最適化変数との対応）

| 状態       | アーク辞書での表現        | MINLPでの役割 |
|------------|---------------------------|---------------|
| アークなし | キーが存在しない          | （なし） |
| 固定アーク | `candidate` キーなし       | 定数（常時アクティブ） |
| 候補アーク | `{"candidate": "q_k"}`     | バイナリ最適化変数の1つ |

加えて、ユニットの `params` のうち `unit_registry` で GA 変数に指定されたもの（膜なら area, p_permeate）が連続変数 x に対応する。

つまり**アーク辞書＋units は MINLP の記号的定義そのもの**。SS を書き下すと変数リストが確定する。

**候補ラベル `q_k` の採番**は頂点IDと同じ規則：**既存の `q` 番号の最大＋1**で振り、削除しても再付番・再利用しない（個数ベースは削除後に衝突するので不可）。これにより、染色体のバイナリ部の並び順（5.1節「binary_variables の順」）も `q` ラベル昇順で決定論的に定まる。

### 3.6 アーク型の語彙（旧コード踏襲・aspen_builder が参照）

`feed` / `membrane_permeate` / `membrane_retentate` / `product` / `residue` / `compressor` /
`expander`（ver3 12.4 で追加・膨張機 EXP の内部アーク） / `recycle` / `process`。
`aspen_builder` はこの `type` でブロック生成と接続を分岐する。`product`/`residue` 型は sink 行きを示すが、
役割は頂点の role が正であり、arc 型は builder 用の補助情報。

### 3.7 ディスク上の JSON スキーマ

JSON はタプルキーを持てないため、アークは**リスト形式**で保存する。

**ss_seed.json / ss_current.json**
```json
{
  "iteration": 0,
  "vertices": {
    "V0": {"role": "feed",     "label": "Feed (CO2 15%)"},
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
              "params": {"permeance_CO2": 2.70677, "permeance_N2": 0.0541354,
                         "area": 20000.0, "p_permeate": 0.2}}
  },
  "history": []
}
```

ケース定数（feed・目標・ペナルティ・GAハイパラ・VP出口圧）は `ss_*.json` に**入れない**。`case.yaml`（3.8節）に置く。

**id_counters（採番カウンタ・任意キー）**：3.1節の「削除しても再付番・再利用しない」を削除後も保証するため、
`ss_current.json` は `"id_counters": {"vertex": <過去に払い出した最大番号>, "candidate": <同・q番号>}` を持つ。
現存要素の最大値ではなく**過去に払い出した最大値**から +1 で採番する（最大番号の頂点/候補を削除→再追加しても
同じ ID が別の流れに再割当されない）。キーが無い旧形式（`ss_seed.json` 含む）は読み込み時に現存最大から
初期化されるので人間が書く必要はない（`topology.allocate_vertex_id` / `allocate_candidate_id`）。

**results.json**（run_iteration.py が書く・signals.py と Claude が読む正本）
```json
{
  "iteration": 1,
  "optimizer": "bo",
  "seed": 1,
  "performance": {"CO2_purity": 0.0, "CO2_recovery": 0.0,
                  "specific_energy_kWh_tCO2": 0.0, "total_compressor_kW": 0.0},
  "optimal_params": {"q_1": 0, "MEMB1_area": 0.0, "MEMB1_p_perm": 0.0},
  "active_candidates": {"q_1": 0},
  "stream_results": {"V0": {"CO2_molfrac": 0.0, "CO2_moleflow": 0.0, "description": "..."}},
  "energy_breakdown": {"VP1": 0.0, "VP2": 0.0},
  "gen_log": [{"gen": 1, "best_fitness": 0.0}],
  "n_evaluations": 0
}
```

**ss_change.json**（Claude が書く・apply_ss.py が読む。ユニット単位＝ver2の新スキーマ。6.2節）

### 3.8 case.yaml（ケース定数・反復で不変）

```yaml
feed:
  flowbase: MASS
  totflow: 1000.0
  co2_frac: 0.15            # post-combustion。DAC にするならここを変える
  temp: 25.0                # °C
  pressure: 1.0             # bar
  basis: MOLE-FRAC          # 組成基準（FLOWBASE=MASS との組合せが動作実績）
optimization_targets:
  purity_min: 0.90
  recovery_min: 0.70
  objective: minimize_specific_energy
penalty_weight: 100000.0    # PENALTY_W（YAML1.1 で 1.0e5 は文字列になるため整数表記）
vp_outlet_pressure: 1.0     # bar。auto-VP の出口圧（固定）
aspen_timeout_eval: 60      # 秒。GA/BO ループ中の per-x タイムアウト
aspen_timeout_detail: 120   # 秒。best 詳細抽出のタイムアウト
optimizer: bo               # 内側最適化器の切替: ga | bo（行削除で ga）
ga:
  pop_size: 20
  n_gen: 50
bo:                         # optimizer: bo のときの設定（無ければ bo.py の defaults）
  n_init: 24
  n_iter: 60
  q_batch: 4
max_binary_variables: 8     # バイナリ候補の上限（2^n 爆発の管理。総数上限は撤廃・6.0節）
min_variables: 4            # 縮退防止の目安（ハード制約ではない）
# 任意キー（省略時はコード既定値）:
#   recovery_physical_max: 1.02   # 質量収支ガードの閾値（simulator.py）
#   subprocess_stall_sec: <秒>    # wedge 判定の手動上書き（subprocess_evaluator.py）
```

---

## 4. 補助ユニットの自動導出

Mixer・Splitter・昇圧機（VP）は**アーク辞書に明示エントリを持たない**。具体トポロジーの構造から決定論的に導出する。

| 補助ユニット | 導出条件 |
|--------------|----------|
| Mixer        | 次のいずれか（具体トポロジー上で判定）：(1) **膜の入口頂点**（膜アークの src）、(2) **入次数 2 以上**の頂点、(3) **sink 頂点**（product/residue。ストリーム化＝測定点確保のため）。(1) があるので、リサイクル候補が OFF でも膜入口に着地点が常に在る。Aspen は1入力 Mixer を許容するため、入力1本でも問題ない |
| Splitter     | ある頂点からの出次数が 2 以上。**ただし builder は未実装**（FSplit を作れない）。非膜頂点の出次数 ≥2 の具体トポロジーは `is_buildable` が評価前に弾く。分岐は「相互排他な候補トグルペア」で表現するのが現行の設計（`algorithm/CLAUDE.md` のリサイクル則）。`topology.splitter_vertices` は導出関数として存在するが将来 FSplit 実装時用 |
| 昇圧機 (VP)  | 各膜の permeate 側に自動挿入。透過側を膜の `p_permeate` から `case.yaml.vp_outlet_pressure`（1 bar）へ昇圧する操作。この昇圧仕事（WNET）が比エネルギーに算入される。**VP は GA 変数を持たない**（入口=p_permeate、出口=1bar固定）。**命名**：VP{n} の `n` は対応する膜の数値部分をそのまま流用（`MEMB3`→`VP3`）。膜を削除すると VP も連動して消え、残った膜が `MEMB1, MEMB5` であれば VP は `VP1, VP5`（欠番OK・再付番しない。頂点ID/候補IDの採番規則と一貫させる） |

- 導出・検査は **具体トポロジー**（q 固定後）に対して行う。
- COMP（圧縮機）は自動付与しない。必要なら Claude が `add_unit` で明示的に追加する（自前の `outlet_pressure` を持つ実ユニット。auto-VP とは別物）。
- feed 頂点のみが入力スペック設定の対象。
- 旧 `aspen_builder._find_mixer_vertices` は (1)(2) を実装済み。**(3) sink への Mixer 配置はポート時に追加する**（旧版は sink を terminal としてスキップしていた。3.4節の測定点の根拠）。

---

## 5. 内側問題：固定超構造上の MINLP

### 5.1 変数

- バイナリ `q ∈ {0,1}^m`：候補アークの ON/OFF。m = 候補アーク数。
- 連続 `x`：`unit_registry` が各ユニット種別から自動生成（膜は area, p_permeate）。

染色体は SS から動的生成：`[q_0..q_{m-1} | x_0..x_{n-1}]`。**並び順は決定論的**——
バイナリは `binary_variables` の順、連続は `continuous_variables` の順（`unit_registry.make_ga_variables` が生成する順）。

### 5.2 評価（ブラックボックス、抽象境界経由）

性能指標は Aspen がブラックボックスとして計算する。内側 GA は Aspen を直接 import せず、`evaluator.py` の境界を経由する。

```python
# evaluator.py — バッチ評価を保つため「具体トポロジー単位」の境界にする
class Evaluator(Protocol):
    def evaluate_topology(self, topology, x_list) -> list[Metrics]: ...
    # 同一トポロジーを1回構築し、x_list を順に流して評価する
    def evaluate_detailed(self, topology, x) -> DetailedResult: ...
    # best解の詳細抽出（results.json 用）。stream_results まで返す
```

- `Metrics`（GA中の各 x、fitness 計算に必要な分）＝ `(specific_energy, purity, recovery, energy_breakdown)`。
  `energy_breakdown`（VP/COMP ごとの WNET）まで含めるのは、ビルド時に auto_vps/energy_blocks を内部で確定できるから。
- `DetailedResult`（best 解1点のみ）＝ Metrics ＋ 各頂点の `stream_results`（CO₂ molfrac/moleflow）。`results.json` の元になる。
- 2経路に分ける理由：GA ループ中は fitness 用に Metrics で足り、頂点ごとのストリーム抽出は best 解1点だけでよい（旧 run_ga と get_detailed_results の役割分担を踏襲）。
- `AspenEvaluator`（simulator.py 側）が実装：`build_aspen_from_epnt` で1回構築し、各 x に `set_continuous_variables` して再収束。`auto_vps`・energy_blocks はこの内部で導出し、境界の外に出さない。
- 将来サロゲート（FMQA/BOQA）に差し替える際は別実装を注入するだけ。テストはモック注入。

**本番経路はプロセス隔離（run6 wedge 対策で追加）**：`run_iteration.py` が実際に注入するのは
`SubprocessEvaluator`（subprocess_evaluator.py）。評価グループごとに使い捨て子プロセス
`aspen_worker.py` を spawn し、その中で `AspenEvaluator` が動く。子は結果を 1 件ずつ stdout に
JSON で流し、親は「最後の受信からの経過」で wedge（返らない in-flight COM 詰まり）を検知して
子ごと kill → 未受信分を `Metrics.bad()` で埋めて前進する。親プロセスは COM に一切触れない。
`aspen_watchdog.py` は子内の out-of-band ハング検知（旧経路のフォールバック。本番経路では
スレッド未起動＝no-op）。障害注入の耐久テストは `tests/test_soak_chaos.py`（`CHAOS_GROUPS` で規模指定）。

### 5.3 目的関数

```
f(q, x) = E + λ [ max(0, π_min − π)² + max(0, ρ_min − ρ)² ]
E = 比エネルギー [kWh/tCO2],  π = 純度,  ρ = 回収率,  λ = case.yaml.penalty_weight
```

### 5.4 GA の実装方針（旧 run_iteration.py の run_ga を移植）

- **Mixed GA（DEAP）**。交叉＝バイナリは一様交叉・連続は blend、変異＝バイナリはフリップ・連続はガウシアン＋境界クリップ。選択トーナメント。
- **バッチ評価（必須）**：集団をバイナリキーでグループ化し、トポロジー1個につき構築1回。連続 x はパラメータ更新で使い回す（旧 `_evaluate_population` を踏襲）。
- **クラッシュ吸収**：Aspen 非収束・COMクラッシュ・タイムアウト・ビルド失敗は有限の `BAD_VALUE`（1e6）。`taskkill` で Aspen を落として1回リトライ（旧実装どおり）。
- 再現性：`random.seed` を反復番号で固定（使用した optimizer と seed は results.json に記録される）。

### 5.6 BO（bo.py・GA と切替可能な内側最適化器）

`case.yaml` の `optimizer: bo` で GA の代わりに **BoTorch Constrained BO** を使う（2026-06 追加。
GA の (D) seed ブレ支配への対策で、約 1/3 の評価数で同等以上の解に到達した実績がある）。

- `run_bo(ss, case, evaluator, seed)` は `run_ga` と同一シグネチャ・同一の Evaluator 境界。
  バイナリキーでのグループ化・`is_buildable` ガード・`BAD_VALUE` 吸収も GA と共通の規律。
- 3 outcome（energy / purity / recovery）を別 GP で学習し、`qLogExpectedImprovement` に
  constraints（purity ≥ purity_min, recovery ≥ recovery_min）を渡す **Constrained EI**。
  bad 観測は energy を有効観測最大の2倍にクリップして infeasible として学習させる。
- **best の選択は feasible 優先**：feasible 観測があればその中の energy 最小、無ければ
  penalty 込み fitness 最小にフォールバック（`d65423b`）。
- **二相式（2026-07-08 追加・run22 の教訓）**：feasible 観測がゼロの間は CEI の
  P(feasible) が平坦化し実質エネルギー最小化器に縮退するため（高エネルギー側の feasible
  盆地へ到達しない）、**第1相＝制約不足量のみを単一 GP + qLogEI で最小化**（エネルギー無視・
  λ 不要）し、初の feasible 観測から第2相＝CEI に切替える。`gen_log` 各要素の `phase` キー
  （"bootstrap"/"cei"）で診断可能。**`bo: bootstrap: off` で旧挙動（常時 CEI）に戻せる**。
- **実行時 bad のリトライ**：一時的 COM wedge（実測15-20%）が purity=0/recovery=0 の偽
  infeasible として GP を汚染するのを防ぐため、ビルド可能なのに bad の x は同一トポロジーで
  `retry_bad` 回（既定1）再評価する。**`bo: retry_bad: 0` で無効化**。
- **ビルド不能な組合せの事前除外**：トグル両 ON 等の構造的 unbuildable なバイナリ組合せは
  `fixed_features_list` から除外し、探索予算を希釈しない。
- **注意（既知の軸ずれ）**：`gen_log` の `best_fitness` は互換のため「全観測の penalty 込み最小」
  のままで、返される best（feasible 優先）と軸が異なる。停止判定の扱いは `algorithm/CLAUDE.md` 参照。
- GP fit / 獲得関数最適化が失敗した反復は Sobol サンプリングにフォールバックしてループを継続する。

### 5.5 移植時の落とし穴（旧 run_iteration.py → ga.py / run_iteration.py）

旧コードは設定をすべて `ss`（ss_current.json）から読んでいたが、ver2 では分離・導出に変わる。移植時に必ず差し替えること。

| 旧コードの参照 | ver2 での出どころ |
|---|---|
| `ss["ga_params"]["pop_size"]` / `["n_gen"]` | **case.yaml.ga** |
| `ss["optimization_targets"]` | **case.yaml.optimization_targets** |
| `ss["feed"]` | **case.yaml.feed** |
| `PENALTY_W = 1e5`（定数） | **case.yaml.penalty_weight** |
| `ss["ga_params"]["continuous_variables"]` | **units + unit_registry から導出**（ss に持たない） |
| `ss["ga_params"]["binary_variables"]` | **candidate アークから導出**（ss に持たない）。`_get_energy_blocks` が COMP 候補を見つけられるよう、導出した各バイナリ変数に対応 unit を持たせる |
| `for i in range(ss["n_vertices"])` | **`vertices` 辞書のキーを走査**（n_vertices は無い） |
| `ss["vertex_descriptions"][i]` | **`vertices[vid]["label"]`** |

つまり `ga.py` / `run_iteration.py` は「case.yaml を読む」「変数リストを `topology` + `unit_registry` から導出する」形にする。`ss` から GA ハイパラ・目標・feed・変数リストを読もうとしないこと。

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
  しない、という非対称性）。（ver3 変更 2026-07-10：旧 `max_variables`（総数上限 20）は撤廃。
  12.1 の permeance 変数化で連続がユニットあたり3本になり、総数上限が構造探索を不当に
  圧迫するため。連続増による BO の収束鈍化は `bo.n_init` 側で調整する。）
- **下限 `case.yaml.min_variables`** は縮退防止の目安。遷移は続けてほしいので、変数を削りすぎて探索が死なないようにする。これは提案を弾くハード制約ではなく、削除と追加で「移動」させ続けるための方向性。
- **構造下限（膜1段必須などの最小骨格）は設けない。** プロセスとして破綻する提案（孤立流れ・feed→product 不達など）は `validate` が弾くので骨格固定は不要。骨格を固定しない方が「事前に想定していなかった構造を提案する」という手法の主張と整合する（膜を全部別方式に置き換える遷移も許す）。
- 上限値（バイナリ 8 / 下限 4）は**暫定で調整の余地あり**。実際の計算時間（BO/GA の評価数 × ユニークトポロジー数 × Aspen評価コスト）を見て見直す。
- 結果として、各反復で解く MINLP は小さいまま（解けるサイズ）でありながら、遷移を重ねることで探索が届く構造空間は広くなる（広い想定範囲）。「解けるサイズ」と「広い想定範囲」の両立が本手法の主張。

> この原則は `algorithm/CLAUDE.md` の運用ルールにも反映する。Claude は提案のたびに「変数を増やし続けていないか」を自己点検する。

### 6.1 1反復の手順（Claude の手番）

```
1. runs/runN/iterations/iter_NNN/results.json を読む
2. シグナルを抽出（境界張り付き／エネルギー支配／不活性候補／制約違反）
3. ss_change.json を「削除＋追加のセット」として書く
4. uv run python src/apply_ss.py --base-dir runs/runN --iter N
5. uv run python src/run_iteration.py --base-dir runs/runN > runs/runN/iter_(N+1)_log.txt 2>&1
6. run_iteration.py 末尾の auto-commit を確認し、1 に戻る
```
（SST セッションは `algorithm/` で開き、コマンドはすべてそこから実行する。run データは
`algorithm/runs/`＝gitignore 済み。`algorithm/CLAUDE.md` と同一の形。algorithm/ は論文公開対象の
自己完結な実行単位で、`.claude/settings.json`（自動実行許可）と `.claude/commands/` も同梱する。）

詳細な運用ルール（自律・停止条件・ロールバック判断）は `algorithm/CLAUDE.md` に定義する。

### 6.2 提案はユニット単位で記述する（ver2 の新スキーマ）

旧版のアーク単位（`fix_arcs`/`add_arcs`…）は漏れ・事故が多いため、ver2 はユニット単位にする。
`apply_ss.py` が `topology.py` のユニット所有権を使ってアークレベルへ展開する。

```json
{
  "reason": "MEMB2_p_perm が下限 0.01 に張り付き → 透過側 COMP を追加して駆動力範囲を拡張",
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
- `add_gated_unit`：ユニットを**「GA が on/off を決めるトグル」**として追加する。`add_unit` が常時 engaged の固定追加なのに対し、本オペは engagement をバイナリ候補に帰属させる＝**構造を GA 変数にする**（6.0 の理想形）。
  ```json
  {"op": "add_gated_unit", "unit_type": "MEMB", "unit": "MEMB3",
   "feed_from": "V5", "bypass_to": "V7", "permeate_to": "V7", "retentate_to": "V8", "params": {...}}
  ```
  これは「MEMB2 透過 V5 の流れを『MEMB3 へ ⇄ そのまま V7 へ（バイパス）』のトグルペアにする」。`apply_ss` の展開：専用の新インレット頂点 Vin を作り、膜本体（`(Vin,Vp) membrane_permeate`・`(Vin,Vr) membrane_retentate`・`(Vp,V7) process`・`(Vr,V8) process`）を固定で張る。給餌 `(V5,Vin)` とバイパス `(V5,V7)` を**ともに候補化**（相互排他のトグルペア、バイナリ2本）。既存の固定 `(V5,V7)` は型を引き継いで候補化する。
  **要点**：給餌候補 OFF → Vin の入次数0 → `active_topology` の dead-unit pruning が MEMB3 を刈り取る＝「ユニットなしのクリーン下位構造」。これで GA が「段あり ⇄ 段なし」を公平評価できる。`feed_from` は「インターセプトする1本の流れ」の源であること（他に固定出アークが残ると候補 ON 時に出次数>1 で `is_buildable` が弾く＝リサイクルのトグルペアと同じ規律）。
- `delete_unit`：ユニット削除（6.3節のルール）。
- `add_arc`：アーク追加。`candidate:true` ならバイナリ変数を1つ増やす。
- `delete_arc`：`(from, to)` で指定したアークを1本削除する。unit 所有アーク（膜の permeate/retentate、COMP の内部アーク）は単体削除すると装置構造が壊れるので拒否し、`delete_unit` を使わせる。削除で頂点が孤立・行き止まりになる場合は適用後の `validate`（項目8）が弾く。`promote_candidate` で固定昇格した非 unit アークもこれで削除でき、昇格は不可逆ではない（必要なら `delete_arc` → `add_arc(candidate:true)` で候補へ戻せる）。
- `promote_candidate`：常時アクティブだった候補を固定アークに昇格。
- `set_bounds`：連続変数の境界を上書き（稀）。**保存場所**：`units[name]` 内に `bounds_override: {param_name: [lo, hi]}` を持たせる（SSテンプレートの一部として永続化）。`unit_registry.make_ga_variables` は `bounds_override` があれば `UNIT_BOUNDS` の既定より優先して採用する（ユニット側の指示が常に勝つ）。`delete_unit` 時に `bounds_override` ごと消える（独立した状態を持たない）。

### 6.2.1 適用後の検証と例外停止

`apply_ss.py` は操作を適用した直後に `topology.validate(new_ss)` を呼ぶ。違反（孤立頂点・feed→product 不達・ユニット所有権の不整合など）が見つかった場合は**例外で停止**し、**`ss_current.json` は更新しない**。`ss_before_change.json` は適用前に必ず退避済み（7節）なので、Claude はそこから差し戻すか別提案を出せる。半端な状態でライブSSが壊れることを防ぐ最終ゲート。

> フェーズ5実装時の未決確認事項は `docs/COORDINATION.md` 参照。

### 6.3 ユニット削除のルール

- 流入：削除ユニットの入口に入ってくる流れは **residue sink へ向ける**（別接続先が要るなら候補アークで導入）。
- 流出：削除ユニットの出力頂点とそれに触れるアークは**単純に削除**。バイパス再接続は不要。
- ユニット採番は減らさない。削除しても番号は欠番、次の追加は新番号。

### 6.4 ユニット所有権

`units` 辞書の `inlet` / `outlets` が各ユニットの所有頂点を記録し、`delete_unit` を曖昧さなく解決する。

---

## 7. 「止まらない」ための仕組み

旧版で既に動いている部分と、Claude の判断に委ねる部分に分かれる。**専用の orchestrate.py は作らない**。

| 仕組み | 実現方法 |
|--------|----------|
| プロセス使い捨て | `run_iteration.py` は毎回新プロセス。クラッシュ時は `taskkill`（旧実装どおり） |
| **評価のプロセス隔離** | `SubprocessEvaluator` が評価グループごとに使い捨て子プロセス（`aspen_worker.py`）を spawn。返らない in-flight COM 詰まり（wedge）は「最後の結果受信からの経過」で検知し**子ごと kill**（`TerminateProcess` は COM の詰まり方に依存せず必ず効く）。受信済みの結果は保全、未受信分は `Metrics.bad()`（5.2節） |
| 途中再開 | run はスクラッチ前提。`iterations/` から次 iter を自動検出（旧 `get_next_iter_num`） |
| ロールバック | **事前 can_build テストはしない**。GA 結果が全個体ペナルティ等で使い物にならなければ、Claude が前の SS に差し戻すか別提案を出す（`algorithm/CLAUDE.md` の判断ルール）。差し戻しのため、**`apply_ss.py` は適用前に必ず `ss_current.json` を `iterations/iter_NNN/ss_before_change.json` に退避する**（旧 apply_ss_change.py の挙動を踏襲）。**退避先が既に存在する iter への再適用は既定で拒否**（リトライでロールバック起点が上書き消失するのを防ぐ。意図的な再適用は `--force`）。**さらに `apply_ss.py` は適用末尾で `validate` を呼び、違反時は例外で停止し `ss_current.json` を更新しない**（6.2.1）。これにより構造破綻は GA に届く前に弾かれる |
| 停止条件 | 連続3反復改善なし／Aspenクラッシュ2連・収束失敗3連／ユーザ停止指示（`algorithm/CLAUDE.md`） |

---

## 8. ディレクトリ構成とファイルの役割

```
LLM4ChemPro_ver2/
├── CLAUDE.md                  開発用：コードを書く・直すための指示
├── ARCHITECTURE.md            本ドキュメント（設計の単一の真実）
├── pyproject.toml             uv プロジェクト・依存定義（人間が管理）
├── .gitignore                 runs/ を除外（結果は版管理しない）。git ルートはこの階層
│
├── algorithm/
│   ├── CLAUDE.md              SST agent用：results を読み新SSを提案し、ループを回す
│   ├── ss_seed.json           初期SS（人間が与える。新スキーマ・変更禁止）
│   ├── case.yaml              ケース定数（人間が管理）
│   │
│   ├── src/
│   │   ├── topology.py        【核・新規】アーク辞書・active_topology・validate・
│   │   │                        補助ユニット導出・所有権・load_ss/save_ss・to_matrix
│   │   ├── unit_registry.py   【移植】種別→GA変数+境界、構造テンプレート
│   │   ├── evaluator.py       【新規】評価の抽象境界（Protocol・Metrics・BAD_VALUE）
│   │   ├── subprocess_evaluator.py 【新規】本番の Evaluator 実装＝プロセス隔離スーパーバイザ
│   │   │                        （子 spawn・結果ストリーム受信・wedge 検知・kill。5.2節）
│   │   ├── aspen_worker.py    【新規】使い捨て子プロセスのエントリ（中で AspenEvaluator が動く）
│   │   ├── aspen_watchdog.py  【新規】子内 out-of-band ハング検知（本番経路では no-op のフォールバック）
│   │   ├── aspen_builder.py   【移植】具体トポロジー→Aspen COM 構築・補助ユニット実体化
│   │   ├── simulator.py       【移植】Aspen実行・クラッシュ検知・タイムアウト・AspenEvaluator
│   │   ├── ga.py              【移植】run_ga（Mixed GA・バッチ評価）
│   │   ├── bo.py              【新規】run_bo（BoTorch Constrained BO。5.6節。optimizer: bo で使用）
│   │   ├── signals.py         【新規・一部移植】results→シグナル抽出（境界張り付き等）
│   │   ├── run_iteration.py   【移植・縮小】内側ループ駆動：SS読込→ga/bo→results保存→auto-commit
│   │   ├── apply_ss.py        【作り直し】ss_change(ユニット単位)→SS更新。topology を使う
│   │   └── YAspen/            ★Aspenアーカイブ（コンパイル済みACM入り）。旧repoから必ずコピー。
│   │       └── Yaspen.apw       src/ 直下に置く（run_iteration.py が dirname(__file__)/YAspen で参照）。
│   │                            これが無いとビルド不能。_2815zdl.* 等の整合に注意（STATUS参照）
│   │
│   └── tests/                 Aspen不要の単体テスト（unittest。discover で一括実行）
│       ├── test_topology.py    active_topology / validate / 導出 / 往復 / 採番 / pruning
│       ├── test_apply_ss.py    提案→SS更新（ユニット単位の展開・安全ガード・再適用ガード）
│       ├── test_bo.py / test_run_iteration.py / test_signals.py / test_simulator_metrics.py
│       ├── test_subprocess_evaluator.py / test_aspen_watchdog.py   評価境界の故障系
│       └── test_soak_chaos.py  障害注入・耐久（_chaos_worker.py が故障7種を注入。CHAOS_GROUPS で規模）
│
├── algorithm/.claude/         SST セッション用の設定（algorithm/ で開く。公開対象）
│   ├── settings.json          自動実行の許可（uv run・runs/ への Write 等。case.yaml/ss_seed は deny）
│   └── commands/
│       ├── sst-analyze.md     /sst-analyze（最新 results を分析→ss_change 提案・実行なし）
│       └── sst-loop.md        /sst-loop（SST 外側ループを自律実行・任意の最大反復数）
│
├── algorithm/runs/            run データ（gitignore。SST セッションの作業領域。HANDOFF.md 含む）
│
├── docs/
│   ├── COORDINATION.md        直近の作業引き継ぎ（run のたびに上書き）
│   └── experiment_log.md      各 run の結果・仕様変更履歴（新しい順に追記）
│
└── runs/                      旧 run データ（run5〜21・参照のみ。新規 run は algorithm/runs/ に置く）
    └── runN/
        ├── ss_current.json            ライブSS状態（apply_ss.py が編集）
        ├── iterations/iter_NNN/
        │   ├── ss_snapshot.json        この反復で使ったSS
        │   ├── results.json            GA/BO結果＋ストリーム/エネルギー（正本。optimizer/seed 記録）
        │   ├── ss_change.json          （Claudeが書く）次SSへの差分
        │   └── ss_before_change.json   （apply_ss が退避）ロールバック起点
        └── iter_NNN_log.txt            Aspen生ログ（失敗時のみ参照）
```

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

## 9. 実装の順序

下から積み上げ、各フェーズはテストが通ってから次へ。Aspen 不要なフェーズ1は最優先で固める。

1. `topology.py` + `unit_registry.py` + `tests/test_topology.py`
   （Aspen不要。active_topology / validate / 補助ユニット導出 / load_ss・save_ss / 所有権 / 変数導出）
2. `simulator.py`（移植：実行・クラッシュ検知・プロセス使い捨て・AspenEvaluator の器）
3. `aspen_builder.py`（移植：具体トポロジー→Aspen、Mixer/VP実体化、命名規則）
4. `evaluator.py` + `ga.py`（移植：バッチ評価で①②③を束ねる）
5. `apply_ss.py`（作り直し：ユニット単位→topology 更新）+ `tests/test_apply_ss.py`
6. `signals.py` + `run_iteration.py`（内側駆動）+ `case.yaml` 配線
7. `algorithm/CLAUDE.md` + `.claude/commands/`（外側ループ運用）

①と⑤が ver2 の本丸（新トポロジー表現と新差分スキーマ）。

---

## 10. Aspen 連携の制約（移植時の必読事項）

- **ブロック名・ストリーム名にアンダースコアを使わない**（Aspen が拒否する）。命名は `V{i}`, `VS{i}T{j}`, `MEMB{n}`, `VP{n}`, `DV{i}`。
- feed の上書き（FLOWBASE/TOTFLOW/CARBO-01/NITRO-01）はモデル構築後に `case.yaml.feed` から適用する。
  **feed 設定は構築後の case.yaml 上書きに一本化する**。旧 `_configure_feed` は内部既定（`MOLE-FRAC`/`FLOWBASE=MOLE`/420ppm）を持つが、
  旧 run_iteration は構築後に `FLOWBASE=MASS`＋`FLOW\CARBO-01=co2_frac` で上書きしており、基底設定が二重で紛らわしい。
  ver2 では builder 内の feed 既定に依存せず、必ず case.yaml 由来の上書きを正とする。
- `Reinit()` を `Run2()` 前に呼んで前回結果をクリアする（キャッシュ汚染を防ぐ）。
- `YAspen/Yaspen.apw` に ACM（GasPermModule）が埋め込まれている。apw を再保存する場合は ACM 同梱を確認。
- 旧 STATUS.md は「膜ポート未実装」等と書くが**陳腐化**している。run2/run3 で動作実績があるため、移植は**コードを正**とする。
- **git ルートは `LLM4ChemPro_ver3/`（プロジェクト全体）**とし、`runs/` は `.gitignore` で除外する（結果は版管理しない）。
  `run_iteration.py` の repo_root は `algorithm/` の親＝プロジェクトルート（実装済み）。
  `runs/` は gitignore されるため auto-commit はコード・docs の変更のみを拾う。

---

## 11. 旧コードの流用方針（移植は ver2 で完了済み・以下は歴史的記録）

参照専用リポジトリ（**いずれも書き込み禁止**）：
- **ver1**: `C:\Users\inukai\PycharmProjects\LLM4ChemPro`（移植元。以下の移植マップの「旧コード」）
- **ver2**: `C:\Users\inukai\PycharmProjects\LLM4ChemPro_ver2`（完成・タグ `v2.0-complete`。
  ver3 のコードはここの追跡ツリーをそのまま継承した。実験記録・git 履歴・過去 run の正本）

| 新ファイル | 旧コードからの流用 | 方針 |
|---|---|---|
| topology.py | `ss_utils.py` のトポロジー部分 | **作り直し**（整数ID→文字列ID、トグル→実削除。ver2の本丸） |
| apply_ss.py | `apply_ss_change.py` | **作り直し**（アーク単位→ユニット単位） |
| unit_registry.py | `unit_registry.py` | ほぼそのまま移植 |
| aspen_builder.py | `aspen_builder.py` | 移植（新トポロジー入力に合わせてインターフェースのみ調整。命名規則・Mixer/VP は維持） |
| simulator.py | `simulator.py` | 移植（+ AspenEvaluator の器を追加） |
| ga.py | `run_iteration.py` の `run_ga` / `get_detailed_results` | 移植（バッチ評価・有限ペナルティはそのまま） |
| signals.py | `run_iteration.py` の `auto_commit_iteration` 内の境界検出 | 抽出して独立化＋拡張 |
| run_iteration.py | `run_iteration.py` の `main` / 駆動部 | 縮小移植（GA本体は ga.py へ） |
| case.yaml | `ss_seed.json` 内の feed/targets/ga_params/penalty | 抽出して分離 |
| ss_seed.json | 旧 `ss_seed.json` | 新スキーマへ変換（文字列ID・role・リスト形式アーク） |
| YAspen/ | `src/YAspen/` | **そのままコピー**（Aspenアーカイブ。`src/` 直下に置く。再生成しない） |
| （破棄） | `result_saver.py`, `phase1_dummy_data.py`, `main_loop.py` | 移植しない |

移植後の ver2 新規追加（旧コードに対応物なし）：`subprocess_evaluator.py` / `aspen_worker.py` /
`aspen_watchdog.py`（run6 の wedge 対策＝評価のプロセス隔離、7節）、`bo.py`（GA の seed ブレ対策
＝Constrained BO、5.6節）。いずれも評価は Evaluator 境界（5.2節）を守る。

Aspen COM 周り（builder / simulator）は動作実績のある資産。ロジックは移植し、新トポロジー表現に合わせて
インターフェースのみ調整する。**「改善」と称して書き直さないこと。**

---

## 12. ver3 のスコープ（本バージョンの新規設計・実装はここに従う）

ver2 で確立した手法（2〜11節）は凍結。ver3 は物理・経済設定を先行研究
（膜カスケードの経済最適化：石炭火力 CCS、目標 purity 95%/recovery 90%、GA＋超構造）に整合させる。
**実装順の推奨**：12.5（アルゴリズム持ち越し・安い順）→ 12.1〜12.4（4本柱）→ run24 系開始。
各項の実装は「テスト緑→コミット」の粒度で進め、case.yaml のスキーマ追加は人間の承認を得る。

### 12.1 Robeson 膜モデル（段別膜物性の変数化）【実装済み 2026-07-10】

膜物性を固定パラメータから**最適化変数**に格上げする。ただし自由な2変数ではなく、
**Robeson 上限（CO2/N2）のトレードオフ曲線上の1自由度**として表現する（架空の万能膜を排除）。

- **変数の向き（先行研究と同一）**：CO2 透過速度 `permeance_CO2` を連続変数とし、選択率を相関式で導出：
  `α = (k / Q[GPU])^(1/n)`、`permeance_N2 = permeance_CO2 / α`。
  係数は Robeson 2008 CO2/N2 上界＝先行研究 Lee et al. (2018) Eq.21：
  **k = 3.0967×10⁸ [GPU]（膜厚 0.1 µm 換算）、n = 2.888**。
  （旧記載の n ≈ 2.616 は CO2/CH4 の 2.636 との混同で誤り。2026-07-10 修正。）
- **アンカー**：**真の Robeson 0.1 µm 上界そのもの**（Lee と同一）。単位換算は
  `1 GPU = 2.70677×10⁻³ m³(STP)/(m²·h·bar)` で確定した——現行膜
  `permeance_CO2 = 2.70677` は正確に **1000 GPU（MTR Polaris 第1世代、α=50）**であり、
  換算の曖昧さは無い。現行膜は上界より下（上界上の α(1000GPU)≈80）。
  「現行膜を通る平行線」案は先行研究の結論（~4000 GPU/α50）と直接比較できないため不採用。
- **探索範囲**：**[500, 6000] GPU**（Lee の感度範囲 500〜5000 ＋ 段別最適 5986 をカバー。
  α は 43〜101 に対応）。case.yaml `membrane_model.permeance_bounds_gpu`。
- **2シナリオ**：(a) 全段同一膜（`tie: true`＝共有変数 `MEMB_perm` 1本、変数リスト先頭固定）と
  (b) 段別独立膜（`tie: false`＝各 MEMB に `{unit}_perm`）。切替は case.yaml。
  それ自体が ablation（先行研究の結論「同一膜なら ~4000 GPU/α50 が指針」との比較点）。
  **run24 は tie: false（段別独立）で開始**（2026-07-10 ユーザ決定。変数上限をバイナリのみに
  変えたため連続変数の増加は許容。同一膜シナリオは ablation 側で実施）。
- **実装箇所（済）**：unit_registry（`robeson_alpha`・`permeance_bounds_aspen`・`GPU_TO_ASPEN`）、
  topology.continuous_variables（membrane_model 引数・tie 変数）、simulator.build_unit_params
  （`MEMB*` 展開・N2 導出の純関数）、aspen_builder.set_continuous_variables に
  `L("CARBO-01")`/`L("NITRO-01")` 書き込み分岐（インターフェース追加のみ・ロジック不変）、
  case.yaml `membrane_model:` セクション。membrane_model セクションを削除すると従来動作。
- **playbook 変更（未・run24 準備時）**：新ユニットの `params` から permeance 直指定を廃止
  （エージェントの「架空高性能膜の発明」の穴を閉じる）。`algorithm/CLAUDE.md` 更新で対応。

### 12.2 コスト目的関数（$/tCO2）【実装済み 2026-07-10】

目的をエネルギーのみから**年間換算回収コスト**へ変更する。ver2 の実測で、エネルギーのみ目的は
「膜面積がタダ」なため面積爆発（MEMB1=32万m²）と大循環 basin を招いた（面積とエネルギーの
トレードオフが閉じない ill-posed 問題）。

- `F_obj = (capital_charge · f_in · C_TCC) / (M_CO2 · t_op) + E·Ce + ペナルティ`
  （Lee et al. 2018 Eq.15/16。`C_TCC = Σ Cm·A + Σ C_unit·|WNET|`）
  - 膜 CAPEX：50 $/m²（モジュール込み）。圧力機器：COMP 670 / VP 1341 / 膨張機 500 $/kW
    （膨張機は WNET<0 で判別。12.4 実装後に効く）
  - 資本賦課率 20%/年 × 設置係数 f_in=1.6
  - 電力 OPEX：E [kWh/tCO2] × 0.04 $/kWh（年間稼働 7,446 h、`M_CO2 = recovery × feed CO2` で正規化）
  - ペナルティ：既存の純度/回収 shortfall² 形式を維持。係数はコストスケールの
    `economics.penalty_weight`（Lee の r=1000）。CBO は獲得関数に使わず記録用
  - **熱交換器（Chx）は省略**（フローシートに冷却器を持たないため。2026-07-10 決定）。
  - **圧力機器 CAPEX は C_unit × |WNET|（電気動力そのまま・η で割らない）**。
    Lee Fig.3/4 の4設計を再現計算し、論文 C_cap との差が全設計で −2% 前後（≒省略した
    HX 分）で一致することを確認済み（2026-07-10。Eq.16 の「W/η」は等エントロピー仕事→
    実動力の換算で、Aspen の WNET は既に実動力。字面どおり除算すると +8〜10% 過大）。
    回帰テスト：`tests/test_cost.py::TestLeeReproduction`
- 経済定数は case.yaml `economics:` セクション（人間管理・Lee Table 1 の値で確定）。
- **二相式 CBO との関係（済）**：第1相（不足量最小化）は不変。第2相の目的 outcome・best 選択・
  ロギング fitness を energy→cost に差し替え。実装は Metrics 拡張ではなく
  **evaluator.cost_per_tco2（純関数）を optimizer 側（ga/bo）で合成**——Metrics・subprocess
  直列化を変えずに済む。切替は `optimization_targets.objective: minimize_cost`。
- 比較可能性のため results.json の performance に energy と **`cost_usd_per_tCO2`** の両方を
  objective 設定に依らず常時記録する（signals にも表示）。

### 12.3 現実的 bounds【実装済み 2026-07-10・人間承認済み】

- `p_permeate` 下限 **0.01 → 0.1 bar**（先行研究の真空ポンプ範囲 0.1–1 bar。0.01 bar=10 mbar は
  工業的に非現実的で、ver2 の最適解が常に張り付いていた＝エネルギー過大の一因）。
- `COMP.outlet_pressure` を **1〜4 bar** に（旧 1.1〜20。feed 昇圧の現実的範囲）。
- 膜面積は先行研究の段あたり範囲 **[1e5, 1.5e6] m²** をそのまま使い、**feed 流量側を
  先行研究の 500 Nm³/s 相当へスケールアップ**して整合させた（2026-07-10 ユーザ決定）。
  **feed 基準の実測事実（同日 smoke_feed で確定）**：case.yaml は `flowbase: MASS` を
  書き込むが実機では無効で、**TOTFLOW はモル流量 [kmol/h] として効く**（ver2 時代から
  同挙動）。したがって `totflow: 80307`（= 500 Nm³/s = 22,307.5 mol/s）。
  `evaluator.feed_co2_t_per_h` もモル解釈で換算する。
  実機の定量整合：feed 2.5 bar の圧縮機動力 79.9 MW ≒ Lee Fig.4a の 81.1 MW（同条件）。
  副作用：bounds 比が 15（面積）/ 9.9（p_permeate）となり、12.5(b) の対数スケール化は
  現行 bounds では発火しない（比 50 超の将来ケースへの保険として残る）。
- 注意：bounds・feed 変更は ver2 の run との数値比較を壊す。ver3 の run シリーズ（run24〜）として
  別管理。Lee スケール feed での Aspen 収束は実機 smoke 済み（2026-07-10）。

### 12.4 圧力アーキテクチャ（feed 昇圧・圧整合・膨張機）【実装済み 2026-07-10・実機 smoke 済み】

- **Mixer 出口圧の罠（解消）**：builder（Step 3）の全 Mixer `PRES=1.0` 固定を **`PRES=0`
  （入口最小圧に追従）へ変更**。実機 smoke：全ストリーム 1 bar の既存構成で結果完全一致
  （回帰なし）、feed 2.5 bar で COMP1=79.9 MW ≒ Lee Fig.4a の 81.1 MW（同条件）＝
  COMP が初めて有効に機能することを定量確認。
- **リサイクル合流の圧整合（方式決定）**：**明示 COMP による再昇圧**（ユーザ決定 2026-07-10）。
  auto-VP 出口は 1 bar 固定のまま。高圧給気へ戻すリサイクルは SST エージェントが COMP を
  経路に置いて表現する（Lee と同構成・追加実装なし・構造の要否は GA/SST が決める＝手法の
  主張と整合）。1 bar のまま合流させると合流 Mixer が最小圧＝1 bar に落ちる点は
  `algorithm/CLAUDE.md` に注意書き。
- **膨張機（実装済み）**：`EXP` ユニット型（Compr の TURBINE・`_create_expander`）。
  GA 変数なし（出口圧 params 固定・既定 1 bar＝大気放出）の構造部品で、エージェントが
  高圧経路（昇圧後の残渣等）に `add_unit`/`add_gated_unit` で配置する。WNET は負値で
  energy_breakdown に載り、比エネルギー（回収控除）とコスト（500 $/kW CAPEX）に算入。
  実機 smoke：2.5 bar 残渣で −40.2 MW を回収。
- **VP2 WNET 欠落疑義（解消）**：実機 smoke で VP1/VP2 両方の WNET が正しく読めることを
  確認（run23 の記録は別要因の可能性。読み取り機構は健全）。
- **COMP 出口圧の境界**：pout=1.0 bar（=feed 圧・無圧縮）でも BAD にならないことを実機確認。

### 12.5 アルゴリズム持ち越し（bo.py・run24 の前に投入推奨）

| 項 | 内容 | 要点 |
|---|---|---|
| (a) bootstrap 相の best 返却修正【済 `978fc9e`】 | feasible ゼロ時の返却が旧来の penalty-min のまま。run23 iter_004 で探索が踏んだ shortfall 0.014 の点が埋もれ 0.063 が記録された | min-shortfall の観測を返す（同率は fitness で tie-break）。`bootstrap: off` 連動で旧挙動（YAML 1.1 の bool False も off として吸収） |
| (b) 対数スケール化【済 `931d892`】 | bounds 比 50 倍超の正の連続変数を GP/acqf/Sobol 内部で log 変換 | 「細い盆地（面積軸の0.6%）が見えない」問題と高カット basin 捕捉（E 5倍）への本命対処。`log_scale_inputs: off` で復帰。**注**: 12.3 の Lee 整合 bounds では比 50 未満のため発火しない（広 bounds への保険として維持） |
| (c) フェーズ対応 patience | BO の頭打ち後の空転（反復あたり~30分）を適応的に打ち切る | **素朴な連続無改善カウントは棄却済み**（run21 の 66%/50% 改善・run22 の 34% 改善を切り捨てる）。要件：判定軸をフェーズ別（bootstrap=min_shortfall／CEI=feasible cost）・相切替でカウンタリセット・床 n_iter/3・`patience: 0` で無効 |

### 12.6 検証・比較実験（論文の実験セット・ver3 ケースで実施）

- **一括 vs SST**：3段**全結合**超構造（膜出口6×行き先~5 の候補アーク 24〜30 本、one-hot 制約で有効
  ~1.5万トポロジー）を SST 遷移なしの一括最適化で解き比較。注意：bo.py の fixed_features は 2^n 全列挙
  なので一括側は GA かサンプリング列挙が必要。全結合 seed は別ファイル（ss_seed は不変）。
  総評価数・獲得関数・retry は SST 側実測に揃える。
- **probe あり vs なし SST**：probe（強制トポロジー診断）は手法から除外済み（開発診断のみ）。
  許可バリアントの playbook を用意し、探索加速効果を ablation として測る。
- **run 間ブラインドの原則**（ver2 で確立・継続）：エージェントは他 run のデータを読まない。
  終了 run は docs 転記後にルート `runs/` へアーカイブ。知識の一般化は人間が指示書・seed・case に反映する
  形でのみ行う。
