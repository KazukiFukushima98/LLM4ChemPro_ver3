"""比較対象（12.6 一括最適化）ランナー：Lee Fig.2 型 SS を1回だけ最適化する。

SST との比較のための従来型手法：大きな超構造（ss_lee2/ss_lee3）を固定し、
構造バイナリ＋連続変数を BO または GA で一括最適化する（構造遷移なし）。

既存 SST コード（src/）は一切変更しない。差分はすべて本スクリプト内の
「実行時差し替え」に閉じる：
  [BO]  bo._build_fixed_features → one-hot の有効組合せ（各出口ちょうど1本 ON）を
        解析的に生成したリストを返す関数に差し替え（既定の 2^n 総当たり列挙は
        3段版の n=24 で 1,677万通りになり実用不能なため）。
        bo._sobol_initial → 初期点のバイナリ部を有効 one-hot 組合せに吸着させる
        ラッパに差し替え（独立ビット丸めだと初期点がほぼ全て建設不能になるため）。
  [GA]  run_iteration.run_ga → baseline/ga_onehot.run_ga_onehot に差し替え
        （出口毎の行き先カテゴリカル遺伝子。詳細は ga_onehot.py 冒頭）。

予算（2026-07-12 ユーザ決定）: 一括側には SST（run24 実測 1,449 評価）より
明確に多い約 2,000 評価を与え、patience は無効化（0）＝早期打ち切りなし。
「時間が足りなかった」という言い訳を許さないための設定。

usage（algorithm/ から）:
    uv run python baseline/run_baseline.py --ss lee2 --optimizer bo [--pilot] [--force]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ALGO = os.path.normpath(os.path.join(HERE, ".."))
SRC = os.path.join(ALGO, "src")
for p in (SRC, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import topology as T  # noqa: E402

# 本番予算: 約 2,000 評価（SST run24 実測 1,449 の ~1.4 倍）・patience 無効
FULL_BO = {"n_init": 100, "n_iter": 475, "q_batch": 4, "patience": 0}
FULL_GA = {"pop_size": 40, "n_gen": 50}                     # 40 + 50*40 = 2,040
# パイロット: 配線確認用の縮小（実 Aspen・数分〜十数分）
PILOT_BO = {"n_init": 8, "n_iter": 4, "q_batch": 2, "patience": 0}
PILOT_GA = {"pop_size": 6, "n_gen": 3}


# =========================================================
# one-hot 生成（BO 差し替えの中身。テストからも import される）
# =========================================================

def build_onehot_fixed_features(ss: dict[str, Any]) -> list[dict[int, float]]:
    """「各出口の行き先はちょうど1つ」の有効組合せを解析的に全生成する。

    2^n の総当たり（bo._build_fixed_features の既定）を踏まずに、
    グループ（出口）毎の選択の直積 = Π|group| 個だけを直接作る。
    lee2: 3^4 = 81、lee3: 4^6 = 4096。
    """
    from ga_onehot import onehot_groups
    bin_vars = T.binary_variables(ss)
    groups = onehot_groups(ss)
    combos: list[dict[int, float]] = []
    for picks in itertools.product(*[range(len(g)) for g in groups]):
        ff = {i: 0.0 for i in range(len(bin_vars))}
        for g, choice in zip(groups, picks):
            ff[g[choice]] = 1.0
        combos.append(ff)
    return combos


def assert_all_buildable(ss: dict[str, Any], combos: list[dict[int, float]]) -> None:
    """全 one-hot 組合せがビルド可能であることの実行時検証（設計不変条件）。"""
    bin_vars = T.binary_variables(ss)
    for ff in combos:
        q_active = {bv["name"]: int(ff[i]) for i, bv in enumerate(bin_vars)}
        reason = T.is_buildable(T.active_topology(ss, q_active))
        assert reason is None, f"one-hot combo unbuildable: {q_active} → {reason}"


def patch_bo_for_onehot(ss: dict[str, Any], seed: int) -> None:
    """bo モジュールに one-hot 対応を実行時注入する（ソース無変更）。

    - _build_fixed_features: 生成済み one-hot リストを返すだけの関数に
    - _sobol_initial: 連続部は元の Sobol のまま、バイナリ部を one-hot 組合せ
      （seed 決定論のランダム選択）で上書きするラッパに
    """
    import random

    import bo as bo_mod

    combos = build_onehot_fixed_features(ss)
    assert_all_buildable(ss, combos)

    bo_mod._build_fixed_features = lambda _ss, _bin_vars: combos

    orig_sobol = bo_mod._sobol_initial
    rng = random.Random(seed * 7919 + 1)

    def sobol_onehot(n_init, bounds, n_bin, sobol_seed):
        x = orig_sobol(n_init, bounds, n_bin, sobol_seed)
        if n_bin > 0:
            for row in range(x.shape[0]):
                ff = combos[rng.randrange(len(combos))]
                for i in range(n_bin):
                    x[row, i] = ff[i]
        return x

    bo_mod._sobol_initial = sobol_onehot
    print(f"[baseline] BO one-hot 注入: 有効組合せ {len(combos)} 個"
          f"（2^{len(T.binary_variables(ss))} 総当たりを回避）")


# =========================================================
# 実行
# =========================================================

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ss", choices=["lee2", "lee3"], required=True)
    parser.add_argument("--optimizer", choices=["bo", "ga"], required=True)
    parser.add_argument("--pilot", action="store_true", help="縮小予算での配線確認")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="既存 run ディレクトリに追記する")
    args = parser.parse_args()

    import run_iteration as RI

    seed_path = os.path.join(HERE, f"ss_{args.ss}.json")
    run_name = f"baseline_{args.ss}_{args.optimizer}" + ("_pilot" if args.pilot else "")
    base_dir = os.path.join(ALGO, "runs", run_name)

    if os.path.exists(os.path.join(base_dir, "iterations")) and not args.force:
        raise SystemExit(f"{base_dir} には既に結果があります（上書き防止）。--force で追記。")
    os.makedirs(base_dir, exist_ok=True)
    shutil.copyfile(seed_path, os.path.join(base_dir, "ss_current.json"))

    # case.yaml を読み、メモリ上でのみ上書き（ファイルは不変）
    case = RI.load_case()
    case["optimizer"] = args.optimizer
    if args.optimizer == "bo":
        case["bo"] = dict(PILOT_BO if args.pilot else FULL_BO)
    else:
        case["ga"] = dict(PILOT_GA if args.pilot else FULL_GA)

    ss = T.load_ss(seed_path)
    n_bin = len(T.binary_variables(ss))
    n_cont = len(T.continuous_variables(ss, case.get("membrane_model")))
    budget = (case["bo"]["n_init"] + case["bo"]["n_iter"] * case["bo"]["q_batch"]
              if args.optimizer == "bo"
              else case["ga"]["pop_size"] * (case["ga"]["n_gen"] + 1))
    print(f"[baseline] SS={args.ss}（バイナリ{n_bin}・連続{n_cont}）, "
          f"optimizer={args.optimizer}, 予算≈{budget}評価, pilot={args.pilot}")

    # 実行時差し替え（本スクリプト内に閉じる。src は無変更）
    if args.optimizer == "bo":
        patch_bo_for_onehot(ss, args.seed)
    else:
        import ga_onehot
        RI.run_ga = ga_onehot.run_ga_onehot
        print("[baseline] GA one-hot 注入: run_iteration.run_ga → ga_onehot.run_ga_onehot")

    results = RI.run_one_iteration(base_dir, case, commit=False)

    perf = results.get("performance", {})
    summary = {
        "run": run_name, "ss": args.ss, "optimizer": args.optimizer,
        "pilot": args.pilot, "budget": budget,
        "n_evaluations": results.get("n_evaluations"),
        "performance": perf,
        "active_candidates": results.get("active_candidates"),
    }
    with open(os.path.join(base_dir, "baseline_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[baseline] 完了: {base_dir}")


if __name__ == "__main__":
    # cp932 コンソール対策（apply_ss.main と同じ堅牢化）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
