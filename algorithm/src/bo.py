"""ベイズ最適化（BoTorch Constrained BO 標準パイプライン）。

GA と並走する内側ループ。Evaluator Protocol 経由で評価する点は ga.py と同じで、
ga.py と差し替え可能な run_bo(ss, case, evaluator, seed) シグネチャを提供する。

設計（二相式 Constrained BO・2026-07-08 run22 の教訓で二相化）:
    - **第1相（bootstrap）**: feasible 観測がゼロの間は、エネルギーを完全に無視して
      「制約不足量 max(0,πmin−π)+max(0,ρmin−ρ)」を単一 GP + qLogEI で最小化する。
      根拠: 全点 infeasible だと CEI の P(feasible) が平坦化して実質エネルギー最小化器に
      縮退し、高エネルギー側の feasible 盆地に永遠に到達しない（run22 で実測）。
      penalty 法(λ=1e5)も同盆地を fitness 上選ばないため、λ 不要の不足量直接最小化を採る。
    - **第2相（CEI）**: 初の feasible 観測が出た反復から従来の Constrained EI に切替：
      3 outcome を別々の GP で学習（energy=目的、purity・recovery=制約）、ModelListGP +
      qLogExpectedImprovement(constraints, objective)。feasible 圏内でエネルギーを削る。
    - bad 観測（is_buildable 失敗・Aspen クラッシュ）は energy=観測最大値の倍にクリップして
      GP に渡す→ infeasible 領域として正しく学習され、CEI が自動的に避ける。
      実行時 bad は retry_bad 回まで同一トポロジーで再評価（一時的 wedge の偽 infeasible
      汚染を防ぐ。リトライ後も bad なら真の非収束として学習）
    - ビルド不能なバイナリ組合せは fixed_features_list から事前除外（予算の希釈防止）
    - best_f は feasible（両制約満たす）観測のうち energy 最良。**全 infeasible のまま
      終了した場合は min-shortfall の観測を返す**（同率は penalty 込み fitness で
      tie-break。12.5(a)。bootstrap: off なら旧来の penalty-min）
    - **ロールバック口**: case.yaml の bo: に `bootstrap: off` / `retry_bad: 0` を書けば
      コード変更なしで run21 までの挙動に戻る

パイプライン:
    1. Sobol 初期サンプル（n_init 点）
    2. 3 GP を fit（ModelListGP）：energy/purity/recovery
    3. qLogEI(constraints=[purity≥purity_min, recovery≥recovery_min]) を optimize_acqf_mixed で最大化
    4. q_batch 点を取得 → 同一 binary key でグループ化して Aspen build 償却
    5. n_iter 回反復

case.yaml への変更は不要：bo: セクションが無い場合は本ファイル内の defaults を使う。
"""

from __future__ import annotations

import itertools
import os
import sys
from collections import defaultdict
from typing import Any

import numpy as np
import torch
from botorch.acquisition.logei import qLogExpectedImprovement
from botorch.acquisition.objective import LinearMCObjective
from botorch.fit import fit_gpytorch_mll
from botorch.models import MixedSingleTaskGP, ModelListGP, SingleTaskGP
from botorch.models.transforms.input import Normalize
from botorch.models.transforms.outcome import Standardize
from botorch.optim import optimize_acqf_mixed
from gpytorch.mlls import ExactMarginalLogLikelihood, SumMarginalLogLikelihood
from torch.quasirandom import SobolEngine

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import BAD_VALUE, Evaluator, Metrics  # noqa: E402
from topology import active_topology, binary_variables, continuous_variables, is_buildable  # noqa: E402


# bo: セクションが case.yaml に無い場合の defaults（n_init ≈ 2-3d 目安、d=binary+continuous）
# bootstrap / retry_bad は run22 の教訓（2026-07-08）による安定化。case.yaml の bo: に
# `bootstrap: off` / `retry_bad: 0` を書けばコード変更なしで旧挙動へ戻せる（ロールバック口）。
_BO_DEFAULTS: dict[str, Any] = {
    "n_init":  16,
    "n_iter":  40,
    "q_batch":  4,
    # 第1相（feasible 観測ゼロの間）の獲得関数:
    #   "shortfall": エネルギーを無視し制約不足量のみ最小化（既定）。
    #     根拠: 全点 infeasible だと CEI の P(feasible) 項が平坦化して実質エネルギー
    #     最小化器に縮退し、高エネルギー側にある feasible 盆地（run22 probe で存在証明済み、
    #     E≈3倍）へ永遠に行かない。penalty 法(λ=1e5)も同盆地を fitness で選ばない
    #     （2192 vs 6226）ため、λ 不要の不足量直接最小化を第1相とする。
    #   "off": 常に CEI（run21 までの挙動）。
    "bootstrap": "shortfall",
    # 実行時 bad（wedge/クラッシュ）の同一トポロジー内リトライ回数。
    # 一時的 wedge（実測15-20%）が purity=0/recovery=0 の偽 infeasible として
    # GP を汚染するのを防ぐ。リトライしても bad ＝ 真の非収束として学習される。
    "retry_bad": 1,
}


def _is_off(value: Any) -> bool:
    """case.yaml のフラグ値が「off」を意味するか判定する。

    YAML 1.1（PyYAML）は素の `off`/`no`/`false` を bool False にパースするため、
    文字列比較 `value != "off"` だけではロールバック口が効かない。文字列・bool の
    両表現を吸収する（"shortfall" 等の有効値は off 扱いにならない）。
    """
    if isinstance(value, str):
        return value.strip().lower() in ("off", "false", "no", "0")
    return value is None or value is False or value == 0


def _fitness(m: Metrics, targets: dict, penalty_w: float) -> float:
    """ロギング・互換用の penalty 込み fitness（GA._fitness と同一定義）。

    Constrained BO 本体は使わない（acqf が constraints + objective で扱う）。
    gen_log / best 戻り値の表示・SST 互換のためだけに残す。
    """
    if m.specific_energy >= BAD_VALUE:
        return BAD_VALUE
    penalty = (
        penalty_w * max(0.0, targets["purity_min"]   - m.purity)   ** 2 +
        penalty_w * max(0.0, targets["recovery_min"] - m.recovery) ** 2
    )
    return m.specific_energy + penalty


def _evaluate_batch_multi(
    x_np: np.ndarray,
    ss: dict,
    bin_vars: list[dict],
    n_bin: int,
    evaluator: Evaluator,
    retry_bad: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """binary key でグループ化 → 各グループを 1 ビルドで一括評価。3 outcome を返す。

    GA の `_evaluate_population` と同じ畳み込み（同一 binary に固まった点は 1 Aspen build
    で q 点まとめて simulate）。

    retry_bad > 0 のとき、ビルド可能なのに bad が返った x だけを同一トポロジーで
    再評価する（最大 retry_bad 回）。一時的 COM wedge（実測15-20%）による偽の
    purity=0/recovery=0 観測が GP を汚染し探索を歪めるのを防ぐ。リトライしても
    bad な点は真の非収束として残す（infeasible 学習は正しい挙動）。
    is_buildable で弾かれたグループは決定論的な構造不能なのでリトライしない。

    Returns
    -------
    energy_arr   : (N,) specific_energy_kWh_tCO2（bad は BAD_VALUE）
    purity_arr   : (N,) purity [0,1]（bad は 0.0）
    recovery_arr : (N,) recovery [0,1]（bad は 0.0）
    valid_mask   : (N,) bool（True = bad でない＝有効観測）
    n_evals      : 実際の Aspen 評価回数（リトライ分を含む。>= N）
    """
    N = x_np.shape[0]
    groups: dict[tuple, list[tuple[int, list[float]]]] = defaultdict(list)
    for i in range(N):
        key = tuple(int(round(float(x_np[i, k]))) for k in range(n_bin))
        x_cont = [float(v) for v in x_np[i, n_bin:]]
        groups[key].append((i, x_cont))

    energies   = np.empty(N, dtype=np.float64)
    purities   = np.empty(N, dtype=np.float64)
    recoveries = np.empty(N, dtype=np.float64)
    valids     = np.empty(N, dtype=bool)
    n_evals = N

    for binary_key, items in groups.items():
        q_active = {bv["name"]: binary_key[i] for i, bv in enumerate(bin_vars)}
        topology = active_topology(ss, q_active)
        reason = is_buildable(topology)
        if reason is not None:
            metrics_list = [Metrics.bad() for _ in items]
        else:
            x_list = [x_cont for _, x_cont in items]
            metrics_list = evaluator.evaluate_topology(topology, x_list)
            # 一時的 wedge 対策: bad だけを再評価（真の非収束は retry 後も bad のまま）
            for _ in range(max(0, retry_bad)):
                bad_pos = [
                    k for k, m in enumerate(metrics_list)
                    if m.specific_energy >= BAD_VALUE
                ]
                if not bad_pos:
                    break
                retry_metrics = evaluator.evaluate_topology(
                    topology, [x_list[k] for k in bad_pos]
                )
                n_evals += len(bad_pos)
                recovered = 0
                for k, m in zip(bad_pos, retry_metrics):
                    if m.specific_energy < BAD_VALUE:
                        recovered += 1
                    metrics_list[k] = m
                if recovered:
                    print(f"  [CBO] retry recovered {recovered}/{len(bad_pos)} bad eval(s) "
                          f"(transient wedge)")
        for (i, _), m in zip(items, metrics_list):
            is_bad = m.specific_energy >= BAD_VALUE
            energies[i]   = BAD_VALUE if is_bad else float(m.specific_energy)
            purities[i]   = 0.0       if is_bad else float(m.purity)
            recoveries[i] = 0.0       if is_bad else float(m.recovery)
            valids[i]     = not is_bad

    return energies, purities, recoveries, valids, n_evals


def _select_device() -> torch.device:
    """GPU があれば cuda:0、無ければ CPU（テスト時のフォールバック）。"""
    return torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")


def _sobol_initial(
    n_init: int,
    bounds: torch.Tensor,
    n_bin: int,
    seed: int,
) -> torch.Tensor:
    """Sobol で n_init 点を生成し、binary 部分のみ {0,1} に丸める。"""
    d = bounds.shape[1]
    sobol = SobolEngine(dimension=d, scramble=True, seed=seed)
    raw = sobol.draw(n_init).to(dtype=bounds.dtype, device=bounds.device)  # in [0,1]^d
    x = bounds[0] + (bounds[1] - bounds[0]) * raw
    if n_bin > 0:
        x[:, :n_bin] = x[:, :n_bin].round().clamp_(0.0, 1.0)
    return x


def _build_fixed_features(ss: dict, bin_vars: list[dict]) -> list[dict[int, float]]:
    """binary の組み合わせを fixed_features_list 形式で返す（ビルド不能な組合せは除外）。

    トグルペア両ON等の構造的にビルド不能な組合せは決定論的に分かっているので、
    「提案→BAD→学習で回避」という無駄なループに載せず最初から獲得関数の探索空間
    から外す（run22: 16トポロジー中の無効組合せが 264 評価の予算を希釈した教訓）。
    全組合せが不能な病的ケースのみ、防御として全列挙にフォールバックする。
    """
    n_bin = len(bin_vars)
    if n_bin == 0:
        return [{}]
    all_combos = list(itertools.product([0, 1], repeat=n_bin))
    buildable = []
    for combo in all_combos:
        q_active = {bv["name"]: v for bv, v in zip(bin_vars, combo)}
        if is_buildable(active_topology(ss, q_active)) is None:
            buildable.append({i: float(b) for i, b in enumerate(combo)})
    if not buildable:  # 防御（SS テンプレが病的な場合のみ）
        return [{i: float(b) for i, b in enumerate(c)} for c in all_combos]
    return buildable


def _make_gp(
    train_x: torch.Tensor,
    train_y_single: torch.Tensor,
    n_bin: int,
    cat_dims: list[int],
    bounds: torch.Tensor,
    d: int,
):
    """1 outcome 用の GP を作る。binary がある場合 MixedSingleTaskGP、無い場合 SingleTaskGP。

    train_y_single は (N, 1) 形状（BoTorch outcome dim を明示）。
    """
    outcome_transform = Standardize(m=1)
    input_transform   = Normalize(d=d, bounds=bounds)
    if n_bin > 0:
        return MixedSingleTaskGP(
            train_X=train_x,
            train_Y=train_y_single,
            cat_dims=cat_dims,
            input_transform=input_transform,
            outcome_transform=outcome_transform,
        )
    return SingleTaskGP(
        train_X=train_x,
        train_Y=train_y_single,
        input_transform=input_transform,
        outcome_transform=outcome_transform,
    )


def _clip_bad_energy(
    energy_arr: np.ndarray,
    valid_mask: np.ndarray,
) -> np.ndarray:
    """bad な energy を「有効観測の最大値の2倍」にクリップ。

    GP fit を壊さず、infeasible として正しく学習される（CEI が自動回避）。
    全 invalid の場合は BAD_VALUE をそのまま使う（rare、初期 Sobol で全失敗）。
    """
    out = energy_arr.copy()
    if valid_mask.any():
        cap = float(energy_arr[valid_mask].max() * 2.0 + 1.0)
        out[~valid_mask] = cap
    return out


def run_bo(
    ss: dict,
    case: dict,
    evaluator: Evaluator,
    seed: int = 1,
) -> tuple[Any, list[dict], int]:
    """BoTorch Constrained BO で ss の連続・バイナリ変数を最適化する。

    ga.run_ga と同一シグネチャ。run_iteration.py から optimizer フラグで切り替えて呼ぶ。

    目的：energy（specific_energy_kWh_tCO2）を最小化
    制約：purity ≥ purity_min, recovery ≥ recovery_min（case.yaml から）

    Returns
    -------
    best     : 最良の x（DEAP Individual 互換の素な list）。feasible 観測があれば
               その中の energy 最小。全 infeasible なら min-shortfall の観測
               （同率は penalty 込み fitness で tie-break）。bootstrap: off のときのみ
               旧来の penalty 込み fitness 最小
    gen_log  : [{"gen": i, "best_fitness": f}, ...] (長さ n_iter)
               best_fitness は penalty 込み値で、SST signals 互換のため
    n_evals  : 総評価回数
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss)
    n_bin     = len(bin_vars)
    n_cont    = len(cont_vars)
    d         = n_bin + n_cont

    targets      = case["optimization_targets"]
    purity_min   = float(targets["purity_min"])
    recovery_min = float(targets["recovery_min"])
    penalty_w    = float(case.get("penalty_weight", 1e5))  # ロギング fitness 用のみ
    bo_cfg       = {**_BO_DEFAULTS, **case.get("bo", {})}
    n_init       = int(bo_cfg["n_init"])
    n_iter       = int(bo_cfg["n_iter"])
    q_batch      = int(bo_cfg["q_batch"])
    # "shortfall" | "off"。YAML 1.1 は `off` を bool False にパースするので _is_off で吸収
    bootstrap_on = not _is_off(bo_cfg.get("bootstrap", "shortfall"))
    retry_bad    = int(bo_cfg.get("retry_bad", 1))

    device = _select_device()
    dtype  = torch.double

    # bounds: shape (2, d)。binary 部分は [0,1]、continuous は cv["bounds"]。
    bounds_list = [[0.0, 1.0]] * n_bin + [list(cv["bounds"]) for cv in cont_vars]
    bounds = torch.tensor(bounds_list, dtype=dtype, device=device).T  # (2, d)

    # 再現性: torch 側の全乱数を seed で固定（Sobol は engine の seed で別途）
    torch.manual_seed(seed)

    cat_dims = list(range(n_bin))
    fixed_features_list = _build_fixed_features(ss, bin_vars)

    print(f"[CBO] device={device}, d={d} (binary={n_bin}, cont={n_cont}), "
          f"n_init={n_init}, n_iter={n_iter}, q_batch={q_batch}, "
          f"fixed_features={len(fixed_features_list)}/{2**n_bin} (unbuildable excluded), "
          f"bootstrap={'shortfall' if bootstrap_on else 'off'}, retry_bad={retry_bad}, "
          f"constraints: purity≥{purity_min}, recovery≥{recovery_min}")

    # ----- 1. Sobol 初期サンプル -----
    train_x_np = _sobol_initial(n_init, bounds, n_bin, seed).detach().cpu().numpy()
    e_arr, p_arr, r_arr, v_mask, n_evals = _evaluate_batch_multi(
        train_x_np, ss, bin_vars, n_bin, evaluator, retry_bad=retry_bad
    )

    # 全観測を tensor へ（GP には energy クリップ版を渡す、ロギング fitness は raw を使う）
    all_x = torch.tensor(train_x_np, dtype=dtype, device=device)
    all_e_raw = e_arr.copy()
    all_p_raw = p_arr.copy()
    all_r_raw = r_arr.copy()
    all_v = v_mask.copy()

    # ----- 2-4. BO ループ -----
    gen_log: list[dict] = []
    objective = LinearMCObjective(weights=torch.tensor([1.0, 0.0, 0.0], dtype=dtype, device=device))

    for it in range(n_iter):
        e_capped = _clip_bad_energy(all_e_raw, all_v)
        feasible_mask = (all_p_raw >= purity_min) & (all_r_raw >= recovery_min) & all_v

        # ---- 二相切替（run22 の教訓・2026-07-08）----
        # feasible 観測ゼロの間、CEI は P(feasible)≈0 が平坦化して実質エネルギー最小化器に
        # 縮退し、高エネルギー側の feasible 盆地へ行かない。第1相ではエネルギーを完全に
        # 無視して「制約不足量」だけを最小化し、初の feasible が出た反復から CEI に切替える。
        # bootstrap="off" で常に CEI（run21 までの挙動）へ戻せる。
        use_bootstrap = bootstrap_on and (not bool(feasible_mask.any()))
        phase = "bootstrap" if use_bootstrap else "cei"

        # GP fit → acqf 最適化。失敗時（例: 初期 Sobol が全 bad で outcome の分散ゼロ、
        # GP の数値不安定、acqf 最適化の内部エラー）はループを殺さず Sobol 探索に
        # フォールバックして観測を増やす（次周期で有効観測が入れば GP に復帰する）。
        try:
            if use_bootstrap:
                # ---- 第1相: 制約不足量の最小化（λ 不要・エネルギー無視）----
                # bad 観測は purity=0/recovery=0 なので不足量が最大値になり、
                # 自然に「近寄らない方がよい点」として学習される（retry 済みの残りは真の非収束）。
                shortfall = (
                    np.maximum(0.0, purity_min   - all_p_raw)
                    + np.maximum(0.0, recovery_min - all_r_raw)
                )
                train_s = torch.tensor(-shortfall, dtype=dtype, device=device).unsqueeze(-1)
                gp_s = _make_gp(all_x, train_s, n_bin, cat_dims, bounds, d)
                mll = ExactMarginalLogLikelihood(gp_s.likelihood, gp_s)
                fit_gpytorch_mll(mll)
                best_f_tensor = torch.tensor(
                    float(-shortfall.min()), dtype=dtype, device=device
                )
                acqf = qLogExpectedImprovement(model=gp_s, best_f=best_f_tensor)
            else:
                # ---- 第2相: Constrained EI（feasible 領域内でエネルギーを削る）----
                # GP fit（energy は最小化なので符号反転して max 化、bad は cap で infeasible 学習）
                train_e = torch.tensor(-e_capped, dtype=dtype, device=device).unsqueeze(-1)
                train_p = torch.tensor(all_p_raw,  dtype=dtype, device=device).unsqueeze(-1)
                train_r = torch.tensor(all_r_raw,  dtype=dtype, device=device).unsqueeze(-1)

                gp_e = _make_gp(all_x, train_e, n_bin, cat_dims, bounds, d)
                gp_p = _make_gp(all_x, train_p, n_bin, cat_dims, bounds, d)
                gp_r = _make_gp(all_x, train_r, n_bin, cat_dims, bounds, d)
                model = ModelListGP(gp_e, gp_p, gp_r)
                mll = SumMarginalLogLikelihood(model.likelihood, model)
                fit_gpytorch_mll(mll)

                # best_f: feasible 観測のうち energy（-値）最大＝energy 最小。
                # （bootstrap="off" の全 infeasible 時のみ旧来の「最悪未満」经路に入る）
                if feasible_mask.any():
                    best_f = float(-e_capped[feasible_mask].min())  # = max(-energy)
                else:
                    best_f = float(-e_capped.max() - 1.0)  # 最悪より下＝改善余地あり扱い
                best_f_tensor = torch.tensor(best_f, dtype=dtype, device=device)

                # acqf: Constrained EI（制約は ≤0 で feasible の規約）
                constraints = [
                    lambda Z: purity_min   - Z[..., 1],  # purity   ≥ purity_min   ⇔ purity_min - purity ≤ 0
                    lambda Z: recovery_min - Z[..., 2],  # recovery ≥ recovery_min ⇔ recovery_min - recovery ≤ 0
                ]
                acqf = qLogExpectedImprovement(
                    model=model,
                    best_f=best_f_tensor,
                    objective=objective,
                    constraints=constraints,
                )

            # categorical 列挙 × continuous L-BFGS（両相共通）
            candidates, _ = optimize_acqf_mixed(
                acq_function=acqf,
                bounds=bounds,
                q=q_batch,
                num_restarts=10,
                raw_samples=256,
                fixed_features_list=fixed_features_list,
            )
        except Exception as e:
            print(f"  [CBO] Iter {it+1}: GP/acqf failed ({type(e).__name__}: {e}) "
                  f"→ Sobol フォールバックで {q_batch} 点探索")
            # seed は反復ごとに変えて重複サンプルを避ける（再現性は seed 起点で保たれる）
            candidates = _sobol_initial(q_batch, bounds, n_bin, seed=seed * 10007 + it + 1)

        # 評価
        c_np = candidates.detach().cpu().numpy()
        new_e, new_p, new_r, new_v, n_new = _evaluate_batch_multi(
            c_np, ss, bin_vars, n_bin, evaluator, retry_bad=retry_bad
        )

        # 観測を蓄積
        all_x     = torch.cat([all_x, candidates], dim=0)
        all_e_raw = np.concatenate([all_e_raw, new_e])
        all_p_raw = np.concatenate([all_p_raw, new_p])
        all_r_raw = np.concatenate([all_r_raw, new_r])
        all_v     = np.concatenate([all_v,     new_v])
        n_evals  += n_new

        # ロギング: 全観測のうち最良の penalty 込み fitness（互換用）
        all_fitness = np.array([
            _fitness(Metrics(specific_energy=e, purity=p, recovery=r), targets, penalty_w)
            for e, p, r in zip(all_e_raw, all_p_raw, all_r_raw)
        ])
        best_fit_log = float(all_fitness.min())
        # phase は SST エージェント・分析用の診断情報（bootstrap=制約探索中 / cei=feasible 圏内）
        gen_log.append({"gen": it + 1, "best_fitness": best_fit_log, "phase": phase})

        # 進捗表示: best fitness と、feasible best energy（あれば）の両方
        if feasible_mask.any():
            fe_min = float(e_capped[feasible_mask].min())
            print(f"  Iter {it+1:2d} [{phase}]: best_fit={best_fit_log:.1f}  feasible_E_min={fe_min:.1f}")
        else:
            min_short = float((
                np.maximum(0.0, purity_min   - all_p_raw)
                + np.maximum(0.0, recovery_min - all_r_raw)
            ).min())
            print(f"  Iter {it+1:2d} [{phase}]: best_fit={best_fit_log:.1f}  "
                  f"(no feasible yet, min_shortfall={min_short:.3f})")

    # ----- 5. best 個体を選んで DEAP Individual 互換 list で返す -----
    # CBO は constraint satisfaction を優先する設計なので、feasible 観測があれば
    # その中で energy 最小を返す（infeasible 良点を選ぶと SST 判定軸に反する）。
    # 全 infeasible のときは bootstrap 相の探索軸と返却軸を揃えて min-shortfall の
    # 観測を返す（12.5(a)。run23 iter_004: 探索が踏んだ shortfall 0.014 の点が
    # 旧 penalty-min 選択で埋もれ 0.063 が記録された）。同率は penalty 込み fitness
    # で tie-break（lexsort は安定ソートなので同 seed で決定論的）。
    # bootstrap: off のときのみ旧来の penalty-min フォールバック。
    final_feasible_mask = (all_p_raw >= purity_min) & (all_r_raw >= recovery_min) & all_v
    if final_feasible_mask.any():
        energy_for_select = np.where(final_feasible_mask, all_e_raw, np.inf)
        best_idx = int(np.argmin(energy_for_select))
    else:
        all_fitness = np.array([
            _fitness(Metrics(specific_energy=e, purity=p, recovery=r), targets, penalty_w)
            for e, p, r in zip(all_e_raw, all_p_raw, all_r_raw)
        ])
        if bootstrap_on:
            # bad 観測は purity=0/recovery=0 で shortfall 最大に落ち、valid と同率の
            # 場合も fitness（bad は BAD_VALUE）の tie-break で valid が勝つ
            shortfall = (
                np.maximum(0.0, purity_min   - all_p_raw)
                + np.maximum(0.0, recovery_min - all_r_raw)
            )
            best_idx = int(np.lexsort((all_fitness, shortfall))[0])
        else:
            best_idx = int(np.argmin(all_fitness))
    best_x = all_x[best_idx].detach().cpu().numpy()
    best_individual: list[float] = [float(v) for v in best_x]

    return best_individual, gen_log, n_evals
