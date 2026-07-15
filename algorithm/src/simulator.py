"""Aspen 実行・クラッシュ検知・タイムアウト処理 + AspenEvaluator。

run_aspen_with_timeout:
    旧 LLM4ChemPro/algorithm/src/simulator.py からの忠実移植。ロジックに変更なし。

AspenEvaluator（フェーズ4a）:
    旧 run_iteration.py の evaluate_group / get_detailed_results から Aspen 操作部分を移植。
    重複実装（_safe / _apply_feed）を 1 か所に集約。
"""

import glob
import os
import sys
import time
import pythoncom

sys.path.insert(0, os.path.dirname(__file__))

import aspen_watchdog  # noqa: E402
from aspen_builder import build_aspen_from_epnt, kill_aspen_image, set_continuous_variables  # noqa: E402
from evaluator import BAD_VALUE, DetailedResult, Metrics                   # noqa: E402
from topology import auto_vps as _auto_vps, continuous_variables            # noqa: E402
from unit_registry import robeson_alpha                                     # noqa: E402


class AspenCrashError(RuntimeError):
    """Raised when Aspen crashes (detected via new .dmp file in dmp_dir)."""


def build_unit_params(
    x: list[float],
    cont_vars: list[dict],
    topology: dict,
    membrane_model: dict | None = None,
) -> dict[str, dict[str, float]]:
    """x（具体トポロジー次元）から set_continuous_variables 用の unit_params を組み立てる。

    - unit 名が "*" で終わるエントリ（tie 共有 permeance の "MEMB*"）は、その
      プレフィクスを持つトポロジー内の全ユニットへ同じ値を展開する
    - membrane_model（12.1）が与えられていれば、permeance_CO2 を持つユニットに
      Robeson 上界から permeance_N2 = permeance_CO2 / α を導出して追加する

    Aspen 非依存の純関数（COM を触らないので単体テスト可能）。
    """
    unit_params: dict[str, dict[str, float]] = {}
    for val, cv in zip(x, cont_vars):
        uname, param = cv["unit_param"]
        if uname.endswith("*"):
            prefix = uname[:-1]
            targets = [u for u in topology.get("units", {}) if u.startswith(prefix)]
        else:
            targets = [uname]
        for u in targets:
            unit_params.setdefault(u, {})[param] = float(val)

    if membrane_model is not None:
        for params in unit_params.values():
            if "permeance_CO2" in params:
                alpha = robeson_alpha(params["permeance_CO2"], membrane_model)
                params["permeance_N2"] = params["permeance_CO2"] / alpha

    return unit_params


MAX_REBUILDS_PER_GROUP = 3  # 1グループ内で許す再ビルド回数の上限（無限ループ防止）


def run_aspen_with_timeout(aspen, timeout=120, dmp_dir=None):
    """Run Aspen and raise TimeoutError or AspenCrashError on failure.

    ウォッチドッグ用に beat() でループ進捗を通知する。COM 呼び出しがブロックして
    別スレッドの watchdog が Aspen を kill すると、ブロック中の呼び出しが
    RPC 切断(-2147023xx) で例外復帰する → ここで AspenCrashError に変換し、
    呼び出し側（evaluate_topology）の「1回再ビルド」経路に乗せる。
    """
    existing_dmps = set(glob.glob(os.path.join(dmp_dir, "*.dmp"))) if dmp_dir else set()

    aspen_watchdog.beat()
    try:
        aspen.Reinit()
        aspen.Run2(1)
    except Exception as e:
        # 実行開始そのものに失敗した場合、エンジン状態は信用できない。
        # 「この x だけ bad で続行」ではなく AspenCrashError に変換して
        # 呼び出し側の再ビルド経路に乗せる（壊れたエンジンでグループの残りを
        # 全滅させないための ver2 堅牢化。ロジック本体は不変）。
        raise AspenCrashError(f"Reinit/Run2 failed (engine unusable): {e}") from e

    start = time.time()
    try:
        while True:
            aspen_watchdog.beat()            # 心拍（ブロックの直前に打つ）
            pythoncom.PumpWaitingMessages()

            if aspen.Engine.IsRunning != 1:
                break

            if dmp_dir:
                current_dmps = set(glob.glob(os.path.join(dmp_dir, "*.dmp")))
                if current_dmps - existing_dmps:
                    raise AspenCrashError("Aspen crashed (new .dmp file detected)")

            if time.time() - start > timeout:
                try:
                    aspen.Engine.Stop()
                except Exception:
                    pass
                # COORDINATION #4: Engine.Stop だけではハング時に効かないため強制 kill
                # （タイムアウト付き。os.system は kill 不能 Aspen でここごと詰まる）
                kill_aspen_image()
                time.sleep(3)
                raise TimeoutError(f"Aspen run timed out after {timeout}s")

            time.sleep(0.5)
    except AspenCrashError:
        raise
    except TimeoutError:
        raise
    except Exception as e:
        # watchdog の kill / クラッシュで COM が切れた場合は RPC 切断として現れる
        if "-2147023" in str(e):
            raise AspenCrashError(f"RPC disconnect (Aspen killed/crashed): {e}")
        raise


class AspenEvaluator:
    """Aspen Plus を用いた具体トポロジーの評価（Evaluator Protocol 実装）。

    移植元（旧 run_iteration.py）の重複実装を 1 か所に集約:
      _safe       ← :179-184 (evaluate_group 内) + :306-311 (get_detailed_results 内)
      _apply_feed ← :168-172 (_build_aspen 内)   + :294-297 (get_detailed_results 内)

    evaluate_topology ← :151-239 (evaluate_group の Aspen 操作部分)
    evaluate_detailed ← :281-356 (get_detailed_results)
    """

    def __init__(self, case: dict, aspen_file: str, dmp_dir: str) -> None:
        """
        Parameters
        ----------
        case       : case.yaml の内容（dict）。feed / optimization_targets を含む。
        aspen_file : Yaspen.apw の絶対パス。
        dmp_dir    : .dmp 出力先ディレクトリ（クラッシュ検知に使う）。
        """
        self._case = case
        self._aspen_file = aspen_file
        self._dmp_dir = dmp_dir

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _safe(self, aspen, path: str, default=None):
        """Aspen ツリーから値を安全に読む。ノード不在・例外は default を返す。

        旧 evaluate_group:179-184 と get_detailed_results:306-311 の重複を集約。
        """
        try:
            n = aspen.Tree.FindNode(path)
            return float(n.value) if n and n.value is not None else default
        except Exception:
            return default

    def _apply_feed(self, aspen) -> None:
        """case.yaml.feed の仕様を V0 ストリームに書き込む（7項目・旧コードの合成を再現）。

        旧コードは _configure_feed（aspen_builder.py:371-382）と evaluate_group の
        _build_aspen（run_iteration.py:168-172）の2段階で feed を設定していた。
        ver2 では builder に feed 既定を持たせないため（ARCHITECTURE 10節）、
        その合成結果をここで一括設定する。

        旧 _configure_feed が設定していた7項目のうち evaluate_group が上書きしなかった3項目
        （BASIS / TEMP / PRES）が ver2 で抜けており BAD_VALUE の原因となっていたため追加。
        """
        feed = self._case["feed"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\BASIS\MIXED").value    = feed["basis"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\TEMP\MIXED").value     = feed["temp"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\PRES\MIXED").value     = feed["pressure"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOWBASE\MIXED").value = feed["flowbase"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\TOTFLOW\MIXED").value  = feed["totflow"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOW\MIXED\CARBO-01").value = feed["co2_frac"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOW\MIXED\NITRO-01").value = 1.0 - feed["co2_frac"]

    def _build(self, topology: dict) -> tuple:
        """Aspen を構築して (aspen, energy_blocks) を返す。失敗時は (None, None)。

        旧 _build_aspen 関数（run_iteration.py:155-173）に相当。
        build_aspen_from_epnt は内部で taskkill 済み（aspen_builder.py:68）のため
        事前 taskkill は不要。失敗時のみ後始末で呼ぶ。
        """
        existing_dmps = set(glob.glob(os.path.join(self._dmp_dir, "*.dmp")))
        try:
            aspen, _ = build_aspen_from_epnt(topology, self._aspen_file)
            new_dmps = set(glob.glob(os.path.join(self._dmp_dir, "*.dmp"))) - existing_dmps
            if new_dmps:
                print("    Aspen crash during build (.dmp detected)")
                kill_aspen_image()
                return None, None
            self._apply_feed(aspen)   # ← try の中（COM 書き込みハングも捕捉）
        except Exception as e:
            print(f"    Aspen build failed: {e}")
            kill_aspen_image()
            return None, None

        # energy_blocks = auto-VP 名 + 明示 COMP/EXP ユニット名
        # （EXP＝膨張機は WNET が負＝回収電力として合計に算入される。ver3 12.4）
        vp_map = _auto_vps(topology)
        energy_blocks: list[str] = list(vp_map.values())
        for uname, udef in topology["units"].items():
            if udef.get("type") in ("COMP", "EXP") and uname not in energy_blocks:
                energy_blocks.append(uname)

        return aspen, energy_blocks

    def _product_vid(self, topology: dict) -> str | None:
        """product role の頂点IDを返す（なければ None）。"""
        return next(
            (v for v, d in topology["vertices"].items() if d.get("role") == "product"),
            None,
        )

    def _extract_metrics(
        self, aspen, product_vid: str, energy_blocks: list[str]
    ) -> Metrics:
        """収束済み Aspen から Metrics を抽出する。"""
        purity  = self._safe(aspen, rf"\Data\Streams\{product_vid}\Output\MOLEFRAC\MIXED\CARBO-01", 0.0)
        prod_mf = self._safe(aspen, rf"\Data\Streams\{product_vid}\Output\MOLEFLOW\MIXED\CARBO-01", 0.0)
        v0_mf   = self._safe(aspen, r"\Data\Streams\V0\Output\MOLEFLOW\MIXED\CARBO-01", 0.0)

        energy_bd: dict[str, float] = {}
        total_kw = 0.0
        for blk in energy_blocks:
            w = self._safe(aspen, rf"\Data\Blocks\{blk}\Output\WNET", 0.0)
            if w:
                energy_bd[blk] = w
                total_kw += w

        if not v0_mf or v0_mf <= 0 or not prod_mf or prod_mf <= 0:
            return Metrics.bad()

        recovery = prod_mf / v0_mf

        # 質量収支ガード（COORDINATION Fix A）：リサイクル tear がヘッドレス COM 上で
        # 「収束」と返っても、非物理な点（製品 CO2 > feed CO2 ＝回収率 > 1）に落ちることがある。
        # 回収率 > 1 は質量収支違反なので、検算でこれを捕捉して BAD で弾く（GA がゴミ解を
        # 最良として追うのを防ぐ）。recovery_physical_max は case.yaml で調整可（既定 1.02）。
        recovery_max = float(self._case.get("recovery_physical_max", 1.02))
        if recovery > recovery_max:
            print(
                f"    mass-balance guard: recovery={recovery:.4f} > {recovery_max} "
                f"(prod_mf={prod_mf:.4g}, v0_mf={v0_mf:.4g}) → BAD（非物理・質量収支違反）"
            )
            return Metrics.bad()

        # エネルギー検算ガード（質量収支ガードと同型）：energy_blocks（VP/COMP）が存在する
        # のに WNET 合計が 0 以下＝ノード読み取りの全滅（命名ずれ・未収束の取りこぼし）。
        # このまま通すと比エネルギー 0 の「偽の完璧解」になり GA/BO が誤った最良解を追う。
        # 物理的に正当な 0 は無い：auto-VP は必ず p_permeate(≤0.99bar)→1bar の昇圧仕事を持つ。
        if energy_blocks and total_kw <= 0:
            print(
                f"    energy guard: blocks={energy_blocks} but total WNET={total_kw:.4g} "
                f"→ BAD（動力の読み取り全滅＝偽のゼロエネルギー解を弾く）"
            )
            return Metrics.bad()

        spec_e   = total_kw / (prod_mf * 44.0 / 1000.0) if prod_mf > 0 else BAD_VALUE

        return Metrics(
            specific_energy=spec_e,
            purity=purity,
            recovery=recovery,
            energy_breakdown=energy_bd,
        )

    # ------------------------------------------------------------------
    # Evaluator Protocol
    # ------------------------------------------------------------------

    def evaluate_topology(
        self,
        topology: dict,
        x_list: list[list[float]],
        on_result=None,
    ) -> list[Metrics]:
        """1 回 Aspen を構築し x_list を順に評価する（フェーズ4a 最小形）。

        旧 evaluate_group（run_iteration.py:151-239）の Aspen 操作部分を移植。
        クラッシュ時は 1 回リトライ（旧実装どおり）。タイムアウトは
        case.yaml.aspen_timeout_eval（既定 60s）。

        Parameters
        ----------
        on_result : Callable[[int, Metrics], None] | None
            各結果が確定するたびに `(index, metrics)` で呼ぶコールバック（後方互換・既定 None）。
            aspen_worker（子プロセス）がこれで1件ずつ stdout に流し、親（SubprocessEvaluator）が
            「最後の受信からの経過」で stall 判定する。途中で wedge しても通過済みの x は親が保持できる。
            すべての結果確定は emit() を経由するので、append/extend を直接書かないこと。
        """
        with aspen_watchdog.armed():
            membrane_model = self._case.get("membrane_model")
            cont_vars   = continuous_variables(topology, membrane_model)
            product_vid = self._product_vid(topology)
            results: list[Metrics] = []

            def emit(m: Metrics) -> None:
                """結果を1件確定する。append + コールバック通知を1か所に集約。"""
                results.append(m)
                if on_result is not None:
                    on_result(len(results) - 1, m)

            def emit_bad_rest() -> None:
                """残り（未確定）の x をすべて bad で埋める。"""
                for _ in range(len(x_list) - len(results)):
                    emit(Metrics.bad())

            if product_vid is None:
                emit_bad_rest()
                return results

            aspen, energy_blocks = self._build(topology)
            if aspen is None:
                emit_bad_rest()
                return results

            timeout_eval = int(self._case.get("aspen_timeout_eval", 60))
            rebuilds = 0

            for x in x_list:
                try:
                    unit_params = build_unit_params(x, cont_vars, topology, membrane_model)
                    set_continuous_variables(aspen, unit_params)
                    run_aspen_with_timeout(aspen, timeout=timeout_eval, dmp_dir=self._dmp_dir)
                    emit(self._extract_metrics(aspen, product_vid, energy_blocks))
                    continue
                except (AspenCrashError, TimeoutError):
                    pass  # → 下の再ビルド処理へ
                except Exception as e:
                    if "-2147023" not in str(e):
                        # COM 切断以外の予期せぬ例外 → この x のみ bad で継続（Aspen は生存前提）
                        print(f"    eval error (non-COM, skipped): {e}")
                        emit(Metrics.bad())
                        continue
                    # COM 切断（kill/クラッシュ由来）→ 下の再ビルド処理へ

                # ここに来た = クラッシュ / COM 切断。失敗 x を bad で埋め、再ビルドして次へ
                print(f"    Aspen crash/COM-disconnect (rebuilds={rebuilds})")
                emit(Metrics.bad())
                rebuilds += 1
                if rebuilds > MAX_REBUILDS_PER_GROUP:
                    print(f"    rebuild budget exhausted ({MAX_REBUILDS_PER_GROUP}) → グループ打ち切り")
                    emit_bad_rest()
                    break
                kill_aspen_image()
                aspen, energy_blocks = self._build(topology)
                if aspen is None:
                    emit_bad_rest()
                    break

            return results

    def evaluate_detailed(
        self,
        topology: dict,
        x: list[float],
    ) -> DetailedResult:
        """best 解 1 点の詳細抽出。stream_results（各頂点の CO₂ 情報）まで返す。

        旧 get_detailed_results（run_iteration.py:281-356）の Aspen 操作部分を移植。
        タイムアウトは case.yaml.aspen_timeout_detail（既定 120s）。
        ビルド・実行失敗時は DetailedResult(metrics=Metrics.bad()) を返す。
        """
        with aspen_watchdog.armed():
            membrane_model = self._case.get("membrane_model")
            cont_vars   = continuous_variables(topology, membrane_model)
            product_vid = self._product_vid(topology)
            if product_vid is None:
                return DetailedResult(metrics=Metrics.bad())

            aspen, energy_blocks = self._build(topology)
            if aspen is None:
                return DetailedResult(metrics=Metrics.bad())

            timeout_detail = int(self._case.get("aspen_timeout_detail", 120))
            try:
                unit_params = build_unit_params(x, cont_vars, topology, membrane_model)
                set_continuous_variables(aspen, unit_params)   # ← try の中（COM 書き込みハングも捕捉）
                run_aspen_with_timeout(aspen, timeout=timeout_detail, dmp_dir=self._dmp_dir)
            except Exception as e:
                print(f"    evaluate_detailed: run failed: {e}")
                return DetailedResult(metrics=Metrics.bad())

            # 旧 get_detailed_results:315-325 に相当。頂点IDで走査（n_vertices は使わない）。
            # pressure_bar は検収用（2026-07-15）: ブロワー campaign の「全膜入口 1.1 bar」を
            # run 後に実測圧で確認する運用のため記録する（record-only）。
            stream_results: dict = {}
            for vid, vdef in topology["vertices"].items():
                co2_frac = self._safe(aspen, rf"\Data\Streams\{vid}\Output\MOLEFRAC\MIXED\CARBO-01")
                co2_mf   = self._safe(aspen, rf"\Data\Streams\{vid}\Output\MOLEFLOW\MIXED\CARBO-01")
                pres     = self._safe(aspen, rf"\Data\Streams\{vid}\Output\PRES_OUT\MIXED")
                if co2_frac is not None:
                    stream_results[vid] = {
                        "CO2_molfrac":  co2_frac,
                        "CO2_moleflow": co2_mf,
                        "pressure_bar": pres,
                        "description":  vdef.get("label", vid),
                    }

            metrics = self._extract_metrics(aspen, product_vid, energy_blocks)
            return DetailedResult(metrics=metrics, stream_results=stream_results)
