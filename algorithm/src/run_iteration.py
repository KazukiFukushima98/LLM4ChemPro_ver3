"""内側ループ駆動：SS読込 → GA → best 詳細抽出 → results.json 保存 → signals 表示 → auto-commit。

旧 run_iteration.py の main / get_next_iter_num / auto_commit_iteration を縮小移植。
GA本体は ga.run_ga、Aspen評価は simulator.AspenEvaluator、シグナル抽出は signals に分離済み。
ここは「読込・配線・保存・コミット」だけを行うドライバ。

CLI:
    uv run python algorithm/src/run_iteration.py --base-dir runs/runN
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import signals as sig_mod  # noqa: E402
import topology as T  # noqa: E402
from evaluator import BAD_VALUE, DetailedResult, Metrics  # noqa: E402
from ga import run_ga  # noqa: E402
from subprocess_evaluator import SubprocessEvaluator, _default_kill_aspen  # noqa: E402
from topology import active_topology, binary_variables, continuous_variables, is_buildable  # noqa: E402

# bo.py は torch/botorch を import するため遅延 import（optimizer="ga" の既定経路では読まない）。


# =========================================================
# 固定パス（ARCH 8節・10節）
# =========================================================

HERE = os.path.dirname(os.path.abspath(__file__))
ASPEN_FILE = os.path.join(HERE, "YAspen", "Yaspen.apw")
DMP_DIR    = os.path.join(HERE, "YAspen")
CASE_PATH  = os.path.normpath(os.path.join(HERE, "..", "case.yaml"))
# REPO_ROOT = algorithm/src の 2 つ上 = プロジェクトルート（LLM4ChemPro_ver2/）
# 旧版は dirname(dirname(__file__)) で algorithm/ を git ルートとしていた。ver2 は親に直す（ARCH 10節）。
REPO_ROOT  = os.path.dirname(os.path.dirname(HERE))


# =========================================================
# Iteration 番号の決定
# =========================================================

def get_next_iter_num(base_dir: str) -> int:
    """iterations/iter_NNN の数値部分の max + 1 を返す（欠番・削除後も再利用しない）。

    iter_ プレフィックスを剥がした残りが数字のみで、かつディレクトリのものだけを
    対象に数値を集め、その max + 1 を返す。1つも無ければ 1（iter は 1 始まり）。
    iter_log / iter_005_log.txt のような異物・非ディレクトリは raise せず skip する。
    """
    iter_dir = os.path.join(base_dir, "iterations")
    os.makedirs(iter_dir, exist_ok=True)
    nums: list[int] = []
    for name in os.listdir(iter_dir):
        if not name.startswith("iter_"):
            continue
        suffix = name[len("iter_"):]
        if suffix.isdigit() and os.path.isdir(os.path.join(iter_dir, name)):
            nums.append(int(suffix))
    return max(nums) + 1 if nums else 1


# =========================================================
# results.json 組み立て
# =========================================================

def build_results_dict(
    iter_num: int,
    best: Any,
    ss: dict,
    detailed: Any,
    gen_log: list[dict],
    n_evals: int,
    optimizer: str = "ga",
    seed: int | None = None,
) -> dict:
    """ARCH 3.7 の results.json スキーマに沿った辞書を組み立てる。

    optimizer / seed は再現性のための記録（どの最適化器がどの乱数で出した結果か。
    seed=iter番号はディレクトリ状態に依存してドリフトし得るため、値そのものを残す）。
    """
    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss)
    n_binary  = len(bin_vars)

    q_active: dict[str, int] = {bv["name"]: int(best[k] > 0.5) for k, bv in enumerate(bin_vars)}
    optimal_params: dict[str, Any] = dict(q_active)
    for k, cv in enumerate(cont_vars):
        optimal_params[cv["name"]] = float(best[n_binary + k])

    m = detailed.metrics
    return {
        "iteration": iter_num,
        "optimizer": optimizer,
        "seed": seed,
        "performance": {
            "CO2_purity":               m.purity,
            "CO2_recovery":             m.recovery,
            "specific_energy_kWh_tCO2": m.specific_energy,
            "total_compressor_kW":      sum(m.energy_breakdown.values()),
        },
        "optimal_params":    optimal_params,
        "active_candidates": q_active,
        "stream_results":    detailed.stream_results,
        "energy_breakdown":  dict(m.energy_breakdown),
        "gen_log":           gen_log,
        "n_evaluations":     n_evals,
    }


# =========================================================
# auto-commit
# =========================================================

def _driving_ss_change_reason(base_dir: str, iter_num: int) -> str:
    """この iter を駆動した ss_change.json は iter_{iter_num-1}/ss_change.json にある。

    iter_1 の場合は driving change が存在しない（初期 SS）。
    """
    if iter_num <= 1:
        return "(initial — no prior ss_change.json)"
    prior = os.path.join(base_dir, f"iterations/iter_{iter_num-1:03d}/ss_change.json")
    if not os.path.exists(prior):
        return f"(no ss_change.json at iter_{iter_num-1:03d})"
    try:
        with open(prior, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("reason", "(no reason field)")
    except Exception as e:
        return f"(failed to read prior ss_change.json: {e})"


def _format_bounds_hit(signals_obj: sig_mod.Signals) -> str:
    if not signals_obj.bounds_hit:
        return " (none)"
    items = [
        f"{h.name}={h.value:.4g} ({h.side} {h.limit:.4g})"
        for h in signals_obj.bounds_hit
    ]
    return "\n  - " + "\n  - ".join(items)


def auto_commit_iteration(
    base_dir: str,
    iter_num: int,
    results: dict,
    signals_obj: sig_mod.Signals,
) -> None:
    """git add -A + commit。runs/ は .gitignore で除外されコード・docs だけステージされる（ARCH 10節）。

    失敗時は print して握りつぶす（旧と同じ）。
    """
    try:
        run_name = os.path.basename(os.path.normpath(os.path.abspath(base_dir)))
        perf     = results.get("performance", {})
        purity   = float(perf.get("CO2_purity", 0.0)) * 100
        recovery = float(perf.get("CO2_recovery", 0.0)) * 100
        spec_e   = float(perf.get("specific_energy_kWh_tCO2", 0.0))

        ss_change_reason = _driving_ss_change_reason(base_dir, iter_num)
        active_cands = results.get("active_candidates", {}) or {}
        active_cands_str = str(active_cands) if active_cands else "(none)"

        title = (
            f"{run_name} iter{iter_num:03d}: "
            f"E={spec_e:.0f}kWh/tCO2 purity={purity:.1f}% recovery={recovery:.1f}%"
        )
        body = (
            f"## Changes\n"
            f"- SS-change: {ss_change_reason}\n"
            f"- Best: specific_energy={spec_e:.2f} kWh/tCO2, "
            f"purity={purity:.1f}%, recovery={recovery:.1f}%\n"
            f"- Active candidates: {active_cands_str}\n"
            f"\n"
            f"## Issues\n"
            f"- Bound-hitting variables:{_format_bounds_hit(signals_obj)}\n"
            f"- Other: TBD (add manually to docs/experiment_log.md)"
        )
        msg = f"{title}\n\n{body}"

        subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
        if diff.returncode == 0:
            print("[git] no staged changes, commit skipped")
            return
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True)
        print(f"[git] committed: {title}")
    except Exception as e:
        # auto-commit 失敗は run 全体の成否に影響させない（旧と同じ）
        print(f"[git] auto-commit failed (ignored): {e}")


# =========================================================
# Main / 1 反復本体
# =========================================================

def load_case() -> dict:
    """algorithm/case.yaml を読み込む。

    呼び出し側で case dict を書き換えてから run_one_iteration() に渡せば、
    case.yaml ファイル自体を変更せずに GA パラメータ等を上書きできる
    （scratch/dryrun_iteration.py から利用）。
    """
    with open(CASE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_ga_overrides(case: dict, pop: int | None, gen: int | None) -> dict:
    """CLI 由来の pop/gen を in-memory の case dict に上書きして返す純関数。

    case.yaml ファイルは一切書かない（ドライランで設定ファイルを汚さずに規模を下げる）。
    pop/gen が None の項目は上書きしない（case.yaml の値を使う）。
    入力 dict は変更しない（上書きがある場合はコピーを返す）。
    """
    if pop is None and gen is None:
        return case
    new_case = copy.deepcopy(case)
    ga = new_case.setdefault("ga", {})
    if pop is not None:
        ga["pop_size"] = pop
    if gen is not None:
        ga["n_gen"] = gen
    return new_case


def apply_bo_overrides(
    case: dict,
    optimizer: str | None,
    n_init: int | None,
    n_iter: int | None,
    q_batch: int | None,
) -> dict:
    """CLI 由来の optimizer / BO パラメータを in-memory の case dict に上書きする純関数。

    case.yaml は不変（BO 検証時にファイルを汚さず規模を下げるため）。
    None の項目は触らない。入力 dict は変更しない。
    """
    if all(v is None for v in (optimizer, n_init, n_iter, q_batch)):
        return case
    new_case = copy.deepcopy(case)
    if optimizer is not None:
        new_case["optimizer"] = optimizer
    bo = new_case.setdefault("bo", {})
    if n_init  is not None: bo["n_init"]  = n_init
    if n_iter  is not None: bo["n_iter"]  = n_iter
    if q_batch is not None: bo["q_batch"] = q_batch
    return new_case


def evaluate_detailed_with_retry(
    evaluator: Any,
    topology: dict,
    x: list[float],
    retries: int = 2,
) -> Any:
    """best 詳細評価の transient wedge 対策：bad が返ったら最大 retries 回まで再実行する。

    バッチ評価では成功した点が、詳細評価（新規ビルド＋再収束）だけ COM wedge で落ちる
    ことがある（run22 で2反復連続を実測。リトライ1回目で実値を回復＝transient）。
    詳細評価は 1 点のみなので再実行は安価（数分）だが、performance が番兵値のまま
    results.json に残ると外側ループの停止判定・リサイクル判断（stream_results）が
    盲目になるため、ここで粘る価値がある。
    """
    detailed = evaluator.evaluate_detailed(topology, x)
    for attempt in range(retries):
        if detailed.metrics.specific_energy < BAD_VALUE:
            break
        print(f"    detailed eval failed (attempt {attempt + 1}/{retries + 1}) — retrying...")
        detailed = evaluator.evaluate_detailed(topology, x)
    return detailed


def run_one_iteration(base_dir: str, case: dict, commit: bool = True) -> dict:
    """1 反復回す本体。case dict は呼び出し側が用意する（ファイル I/O はしない）。

    Returns
    -------
    dict
        書き出した results.json と同じ内容。
    """
    iter_num = get_next_iter_num(base_dir)
    iter_dir = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}")
    os.makedirs(iter_dir, exist_ok=True)

    print("=" * 60)
    print(f"Iteration {iter_num}  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"base_dir:  {base_dir}")
    print(f"iter_dir:  {iter_dir}")
    print("=" * 60)

    # SS 読み込み + スナップショット保存
    ss_path = os.path.join(base_dir, "ss_current.json")
    ss = T.load_ss(ss_path)
    T.save_ss(ss, os.path.join(iter_dir, "ss_snapshot.json"))

    bin_vars  = binary_variables(ss)
    cont_vars = continuous_variables(ss)
    print(f"\n[1] SS: {len(ss['vertices'])} vertices, units={list(ss['units'])}")
    print(f"    binary={[bv['name'] for bv in bin_vars]}")
    print(f"    continuous={[cv['name'] for cv in cont_vars]}")
    optimizer = case.get("optimizer", "ga")
    if optimizer == "bo":
        bo_cfg = {**{"n_init": 16, "n_iter": 40, "q_batch": 4}, **case.get("bo", {})}
        print(f"\n[2] BO: n_init={bo_cfg['n_init']}, n_iter={bo_cfg['n_iter']}, "
              f"q_batch={bo_cfg['q_batch']}, seed={iter_num}")
    else:
        # ga セクションは GA 経路でのみ必須（BO 運用の case.yaml から外しても落ちない）
        ga_cfg = case["ga"]
        print(f"\n[2] GA: pop={ga_cfg['pop_size']}, gen={ga_cfg['n_gen']}, seed={iter_num}")

    # 内側ループ駆動。評価はプロセス隔離（SubprocessEvaluator）。親は COM を触らないため
    # out-of-band watchdog は不要——親の stall 監視（最後の結果受信からの経過）が全カバーする。
    evaluator = SubprocessEvaluator(case, ASPEN_FILE, DMP_DIR)
    if optimizer == "bo":
        from bo import run_bo  # 遅延 import: GA 既定経路では torch/botorch を読み込まない
        best, gen_log, n_evals = run_bo(ss, case, evaluator, seed=iter_num)
    else:
        best, gen_log, n_evals = run_ga(ss, case, evaluator, seed=iter_num)

    # best の詳細評価（stream_results 込み）
    print("\n[3] Evaluating best solution (detailed)...")
    n_binary = len(bin_vars)
    q_active = {bv["name"]: int(best[k] > 0.5) for k, bv in enumerate(bin_vars)}
    topology_best = active_topology(ss, q_active)
    x_best = [float(best[n_binary + k]) for k in range(len(cont_vars))]
    build_reason = is_buildable(topology_best)
    if build_reason is not None:
        # 全個体ペナルティ等で best がビルド不能な q に落ちた場合、Aspen を無駄に
        # 起動せず bad を明示する（results.json 上も失敗として読める）
        print(f"    best topology is unbuildable ({build_reason}) — detailed eval skipped")
        detailed = DetailedResult(metrics=Metrics.bad())
    else:
        detailed = evaluate_detailed_with_retry(evaluator, topology_best, x_best)

    # 反復の評価がすべて終わったので、残留 AspenPlus.exe を回収する（次の評価までの
    # 外側ループ思考中にメモリ・ライセンスを占有し続けるのを防ぐ。逐次評価＝同時1個の
    # 前提でイメージ名 kill が安全なのは評価中と同じ。タイムアウト付きで必ず戻る）。
    _default_kill_aspen()

    # results.json 書き出し（アトミック：外側ループの正本が途中クラッシュで壊れないように）
    results = build_results_dict(
        iter_num, best, ss, detailed, gen_log, n_evals,
        optimizer=optimizer, seed=iter_num,
    )
    results_path = os.path.join(iter_dir, "results.json")
    tmp_path = results_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, results_path)

    # シグナル抽出 + 表示
    signals_obj = sig_mod.extract(results, ss, case)
    print()
    print(sig_mod.summarize(signals_obj))

    # サマリ
    perf = results["performance"]
    print()
    print("=" * 60)
    print(f"Iteration {iter_num} summary")
    print("=" * 60)
    print(f"  CO2 purity:       {perf['CO2_purity']*100:.1f}%")
    print(f"  CO2 recovery:     {perf['CO2_recovery']*100:.1f}%")
    print(f"  Specific energy:  {perf['specific_energy_kWh_tCO2']:.1f} kWh/tCO2")
    print(f"  Total compressor: {perf['total_compressor_kW']:.1f} kW")
    print(f"  Evaluations:      {n_evals}")
    print(f"  Saved to:         {iter_dir}/")

    # auto-commit（ARCH 10節：repo_root はプロジェクトルート、runs/ は gitignore で除外）
    # --no-commit のドライランでは、一時的な pop/gen 等を巻き込まないようスキップする。
    if commit:
        auto_commit_iteration(base_dir, iter_num, results, signals_obj)
    else:
        print("[git] --no-commit: auto-commit skipped")

    return results


def main() -> None:
    # Windows の既定コンソールは cp932 で、SS の reason 等に含まれる非 cp932 文字を
    # print すると UnicodeEncodeError で落ちる。標準ストリームを UTF-8 に固定して堅牢化する。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".",
                        help="run directory containing ss_current.json and iterations/")
    parser.add_argument("--pop", type=int, default=None,
                        help="override ga.pop_size in-memory (case.yaml は変更しない)")
    parser.add_argument("--gen", type=int, default=None,
                        help="override ga.n_gen in-memory (case.yaml は変更しない)")
    parser.add_argument("--optimizer", choices=["ga", "bo"], default=None,
                        help="override optimizer in-memory (case.yaml は変更しない)")
    parser.add_argument("--bo-n-init",  type=int, default=None, help="override bo.n_init")
    parser.add_argument("--bo-n-iter",  type=int, default=None, help="override bo.n_iter")
    parser.add_argument("--bo-q-batch", type=int, default=None, help="override bo.q_batch")
    parser.add_argument("--no-commit", action="store_true",
                        help="末尾の auto-commit をスキップ（ドライラン用）")
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    case = load_case()
    case = apply_ga_overrides(case, args.pop, args.gen)
    case = apply_bo_overrides(case, args.optimizer, args.bo_n_init, args.bo_n_iter, args.bo_q_batch)

    # 最適化器と縮小フラグの食い違い警告：optimizer=bo のとき --pop/--gen は読まれない
    # （逆も同様）。「ドライランのつもりがフル規模で走る」事故を無言で通さない。
    effective_optimizer = case.get("optimizer", "ga")
    if effective_optimizer == "bo" and (args.pop is not None or args.gen is not None):
        print("[WARN] optimizer=bo のため --pop/--gen は無効です。"
              "BO の縮小は --bo-n-init/--bo-n-iter/--bo-q-batch を使ってください。")
    if effective_optimizer != "bo" and any(
        v is not None for v in (args.bo_n_init, args.bo_n_iter, args.bo_q_batch)
    ):
        print("[WARN] optimizer=ga のため --bo-* フラグは無効です。"
              "GA の縮小は --pop/--gen を使ってください。")

    run_one_iteration(base_dir, case, commit=not args.no_commit)


if __name__ == "__main__":
    main()
