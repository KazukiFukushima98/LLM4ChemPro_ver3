"""ユニット種別 → GA変数定義・境界・構造テンプレートの登録簿。

責務:
- ユニット種別ごとの連続変数（GA で動かすパラメータ）と既定境界
- ユニット種別ごとの構造テンプレート（出力ポート名の集合）
- bounds_override が unit データ側にあれば既定より優先する

依存: なし（Aspen 非依存・ドメイン非依存ロジック）
"""

from __future__ import annotations

from typing import Any


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
}

# 構造テンプレート（add_unit が出力頂点を生成するときに参照）
# outlets はポート名のリスト。順序は決定論的（apply_ss が new vertex を順に採番する）
STRUCTURE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "MEMB": {"outlets": ["permeate", "retentate"]},
    "COMP": {"outlets": ["outlet"]},
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
) -> list[dict[str, Any]]:
    """ユニット1つに対応する連続 GA 変数のリストを返す。

    Parameters
    ----------
    unit_name : str
        ユニット名（例 "MEMB1"）。種別はプレフィクスで識別する。
    unit_data : dict | None
        SS の units[name] エントリ。`bounds_override: {param_name: [lo, hi]}` を
        持っていれば、その param について UNIT_BOUNDS の既定より優先する。

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
    return variables


def get_outlet_ports(unit_type: str) -> list[str]:
    """ユニット種別の出力ポート名リストを返す（STRUCTURE_TEMPLATES より）。"""
    template = STRUCTURE_TEMPLATES.get(unit_type)
    if template is None:
        raise KeyError(f"unknown unit_type: {unit_type!r}")
    return list(template["outlets"])