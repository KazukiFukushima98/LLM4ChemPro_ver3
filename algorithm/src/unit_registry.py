"""ユニット種別 → GA変数定義・境界・構造テンプレートの登録簿。

責務:
- ユニット種別ごとの連続変数（GA で動かすパラメータ）と既定境界
- ユニット種別ごとの構造テンプレート（出力ポート名の集合）
- bounds_override が unit データ側にあれば既定より優先する
- Robeson 膜モデル（ver3 12.1）: permeance_CO2 を GA 変数化し、選択率を
  Robeson 2008 CO2/N2 上界（0.1 µm 膜厚換算）から導出する相関式

依存: なし（Aspen 非依存・ドメイン非依存ロジック）
"""

from __future__ import annotations

from typing import Any

# =========================================================
# Robeson 膜モデル（ver3 12.1）
# =========================================================
#
# 単位換算: Aspen GasPermModule の permeance 単位は m3(STP)/(m2·h·bar)。
# 1 GPU = 1e-6 cm3(STP)/(cm2·s·cmHg) ≈ 2.70677e-3 m3(STP)/(m2·h·bar)。
# 換算係数はプロジェクト従来の膜パラメータ（permeance_CO2=2.70677 が
# MTR Polaris 第1世代の 1000 GPU、permeance_N2 がその 1/50=α50）と正確に
# 整合するよう固定する（物理定数からの導出値 2.70022e-3 との差 0.24% は
# cmHg 換算の丸めで、既存資産との連続性を優先）。
GPU_TO_ASPEN = 2.70677e-3

# Robeson 2008 CO2/N2 上界（Lee et al. 2018, Eq. 21）の既定値:
#   permeance_CO2 [GPU] = k · α^(−n)  （膜厚 0.1 µm 換算）
#   ⇔ α = (k / Q[GPU])^(1/n)
# case.yaml の membrane_model: で上書き可能（人間管理）。
_ROBESON_DEFAULTS: dict[str, Any] = {
    "robeson_k_gpu": 3.0967e8,
    "robeson_n": 2.888,
    "gpu_to_aspen": GPU_TO_ASPEN,
    # 探索範囲 [GPU]。Lee の感度範囲 500-5000 と段別最適 5986 をカバー
    "permeance_bounds_gpu": [500.0, 6000.0],
    "tie": True,   # 全段同一膜（1変数共有）。False で段別独立
}


def _mm_cfg(membrane_model: dict[str, Any] | None) -> dict[str, Any]:
    """membrane_model 設定に既定値をマージして返す。"""
    return {**_ROBESON_DEFAULTS, **(membrane_model or {})}


def robeson_alpha(permeance_co2_aspen: float, membrane_model: dict[str, Any] | None = None) -> float:
    """CO2 permeance（Aspen 単位）から Robeson 上界上の CO2/N2 選択率 α を返す。

    α = (k / Q[GPU])^(1/n)。上界そのものをフロンティアとして使う（Lee 2018 と
    同一のアンカー。現行膜 Polaris 1000GPU/α50 は上界より下にあり、上界上の
    α(1000GPU)≈80）。permeance_N2 = permeance_CO2 / α で導出する。
    """
    cfg = _mm_cfg(membrane_model)
    q_gpu = float(permeance_co2_aspen) / float(cfg["gpu_to_aspen"])
    if q_gpu <= 0.0:
        raise ValueError(f"permeance_CO2 must be positive, got {permeance_co2_aspen!r}")
    alpha = (float(cfg["robeson_k_gpu"]) / q_gpu) ** (1.0 / float(cfg["robeson_n"]))
    return max(alpha, 1.0)  # 防御: 範囲外でも α<1（逆選択）にはしない


def permeance_bounds_aspen(membrane_model: dict[str, Any] | None = None) -> list[float]:
    """permeance_CO2 変数の bounds を Aspen 単位で返す（GPU 指定を換算）。"""
    cfg = _mm_cfg(membrane_model)
    lo_gpu, hi_gpu = cfg["permeance_bounds_gpu"]
    c = float(cfg["gpu_to_aspen"])
    return [float(lo_gpu) * c, float(hi_gpu) * c]


def is_tie_mode(membrane_model: dict[str, Any] | None) -> bool:
    """tie モード（全段同一膜＝permeance 1変数共有）かどうか。"""
    return bool(_mm_cfg(membrane_model).get("tie", True))


# 連続変数の既定境界（ユニット種別ごと）
# ver3（12.3）: 先行研究 Lee et al., J. Membr. Sci. 563 (2018) 820-834 の §2.6 に整合。
#   - area: 段あたり [1e5, 1.5e6] m²（feed も Lee の 500 Nm³/s 相当へスケールアップ済み。
#     case.yaml feed.totflow 参照。コストモデルが線形なので $/tCO2 はスケール不変）
#   - p_permeate: 真空ポンプ吸引圧 0.1-1 bar（0.01 bar=10 mbar は工業的に非現実的。
#     上限は駆動力ゼロを避けて 0.99）
#   - COMP.outlet_pressure: 1-4 bar（feed 昇圧の現実的範囲）
UNIT_BOUNDS: dict[str, dict[str, list[float]]] = {
    "MEMB": {
        "area":       [100000.0, 1500000.0],
        "p_permeate": [0.1, 0.99],
    },
    "COMP": {
        "outlet_pressure": [1.0, 4.0],
    },
    # 膨張機（ver3 12.4）: GA 変数なし（出口圧は params.outlet_pressure 固定・既定 1 bar）。
    # 構造部品としてエージェントが高圧経路（昇圧後の残渣等）に配置し電力を回収する。
    "EXP": {},
    # 冷却器/加熱器（ver3 12.4 検算で追加）: GA 変数なし（params.temperature [°C]・
    # params.pressure は Aspen Heater の PRES 指定＝0 で圧力損失なし）。builder の
    # _create_heater（ver1 由来・動作資産）を使う。圧縮後の高温ガスを膜運転温度へ
    # 戻す中間冷却器として配置する（冷却水コストはモデル外＝HX 省略の決定と整合）。
    "HEAT": {},
}

# 連続変数の宣言（GA 変数名の組み立てに使う）
_UNIT_GA_VARS: dict[str, list[dict[str, str]]] = {
    "MEMB": [
        {"suffix": "area",   "param": "area"},
        {"suffix": "p_perm", "param": "p_permeate"},
    ],
    "COMP": [
        {"suffix": "pout", "param": "outlet_pressure"},
    ],
    "EXP": [],
    "HEAT": [],
}

# 構造テンプレート（add_unit が出力頂点を生成するときに参照）
# outlets はポート名のリスト。順序は決定論的（apply_ss が new vertex を順に採番する）
STRUCTURE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "MEMB": {"outlets": ["permeate", "retentate"]},
    "COMP": {"outlets": ["outlet"]},
    "EXP":  {"outlets": ["outlet"]},
    "HEAT": {"outlets": ["outlet"]},
}


def get_unit_type(unit_name: str) -> str | None:
    """ユニット名から種別プレフィクスを取り出す（"MEMB1" -> "MEMB"）。

    UNIT_BOUNDS に登録のないプレフィクスは None を返す。
    """
    for prefix in UNIT_BOUNDS:
        if unit_name.startswith(prefix):
            return prefix
    return None


def make_ga_variables(
    unit_name: str,
    unit_data: dict[str, Any] | None = None,
    membrane_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """ユニット1つに対応する連続 GA 変数のリストを返す。

    Parameters
    ----------
    unit_name : str
        ユニット名（例 "MEMB1"）。種別はプレフィクスで識別する。
    unit_data : dict | None
        SS の units[name] エントリ。`bounds_override: {param_name: [lo, hi]}` を
        持っていれば、その param について UNIT_BOUNDS の既定より優先する。
    membrane_model : dict | None
        case.yaml の membrane_model: セクション（12.1）。与えられ、かつ tie=False
        （段別独立膜）のとき、MEMB に permeance_CO2 変数 `{unit}_perm` を追加する。
        tie=True（全段同一膜）の共有変数は topology.continuous_variables 側で
        先頭に 1 本だけ付与する（ここでは付けない）。None なら従来どおり
        （permeance は params の固定値）。

    Returns
    -------
    list[dict]
        各要素は {"name": str, "unit_param": [unit_name, param], "bounds": [lo, hi]}。
    """
    unit_type = get_unit_type(unit_name)
    if unit_type is None:
        return []

    overrides = (unit_data or {}).get("bounds_override", {}) or {}

    variables: list[dict[str, Any]] = []
    for spec in _UNIT_GA_VARS[unit_type]:
        param = spec["param"]
        bounds = overrides.get(param, UNIT_BOUNDS[unit_type][param])
        variables.append({
            "name":       f"{unit_name}_{spec['suffix']}",
            "unit_param": [unit_name, param],
            "bounds":     list(bounds),
        })

    if unit_type == "MEMB" and membrane_model is not None and not is_tie_mode(membrane_model):
        variables.append({
            "name":       f"{unit_name}_perm",
            "unit_param": [unit_name, "permeance_CO2"],
            "bounds":     permeance_bounds_aspen(membrane_model),
        })

    return variables


def get_outlet_ports(unit_type: str) -> list[str]:
    """ユニット種別の出力ポート名リストを返す（STRUCTURE_TEMPLATES より）。"""
    template = STRUCTURE_TEMPLATES.get(unit_type)
    if template is None:
        raise KeyError(f"unknown unit_type: {unit_type!r}")
    return list(template["outlets"])