"""プロセス隔離スーパーバイザ（Evaluator Protocol 実装）。

run6 の wedge（server-kill しても in-flight COM 呼び出しが返らない）に対する
保証付き最終手段。親（このクラス）は COM を一切触らず、評価グループごとに
子プロセス aspen_worker.py を spawn する。子の中で既存 AspenEvaluator が動く。

階層:
    子プロセス内 = 速い一次回復（経路①クラス：RPC 切断で返るクラッシュ → 再ビルド）
    親プロセス   = 保証付き最終手段（返らない in-flight 詰まり → 子ごと kill）
                   TerminateProcess は無条件なので COM の詰まり方に依存しない。

生存信号は「子 stdout の結果ストリーム」だけ。別スレッドの heartbeat は作らない
（heartbeat は wedge を隠すため）。最後に結果を受け取ってからの経過が stall_sec を
超えたら wedge とみなす。評価は run_ga 内で逐次＝子・Aspen は常に同時 1 個なので、
taskkill /f /im AspenPlus.exe（イメージ名 kill）が安全に効く。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import DetailedResult, Metrics  # noqa: E402
from topology import dump_topology              # noqa: E402


def _default_kill_aspen() -> None:
    """AspenPlus.exe を taskkill する（タイムアウト付き）。

    os.system はタイムアウトが無く、kill 不能な状態に陥った wedge Aspen で
    呼び出し側（＝監督スレッド）ごと無制限にブロックし得る（COORDINATION: run13 の
    85分ハングの主因＝「保証付き最終手段」自身がここで詰まった）。subprocess.run の
    timeout で必ず戻るようにし、固まっても監督を縛らない。
    """
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "AspenPlus.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except Exception:
        pass  # timeout / 失敗でもブロックしない（監督を止めないことが最優先）


class SubprocessEvaluator:
    """各評価グループを使い捨て子プロセスで隔離評価する Evaluator。"""

    def __init__(
        self,
        case: dict,
        aspen_file: str,
        dmp_dir: str,
        *,
        worker_script: str | None = None,
        python_exe: str | None = None,
        stall_sec: float | None = None,
        kill_aspen=None,
    ) -> None:
        self._case = case
        self._aspen_file = aspen_file
        self._dmp_dir = dmp_dir
        self._worker = worker_script or os.path.join(os.path.dirname(__file__), "aspen_worker.py")
        self._python = python_exe or sys.executable

        et = int(case.get("aspen_timeout_eval", 60))
        dt = int(case.get("aspen_timeout_detail", 120))
        # 「結果無受信」の上限＝wedge と判定するまで。in-band per-x timeout（et/dt）より長くする。
        # 下限は「ビルド＋途中の再ビルド込みの正当な無音」(~150s)＝ここを割ると健全な処理を誤 kill
        # し、正しく走っていた評価を偽の BAD_VALUE に変えて GA/BO を汚染する。
        # 用途で分ける：GA 中（topology）は eval(et) だけが効くので短く、最後の詳細抽出（detailed）は
        # detail(dt) に合わせる。バッファは 120s（build/再ビルド分 ~90s ＋ 誤 wedge 回避マージン 30s。
        # 旧値 90s は正当な無音の推定上限 ~150s と同値＝マージンゼロで、遅いビルドを誤 kill し得た）。
        # 明示指定（stall_sec / case.subprocess_stall_sec）があれば両モードでそれを使う（テスト・手動上書き用）。
        override = (
            stall_sec
            if stall_sec is not None
            else (int(case["subprocess_stall_sec"]) if "subprocess_stall_sec" in case else None)
        )
        self._stall_topology = override if override is not None else et + 120
        self._stall_detailed = override if override is not None else dt + 120
        # テストで差し替え可能に（テスト中に実 Aspen を殺さないため必須）。
        # 既定はタイムアウト付き（_default_kill_aspen）。詳細はその docstring 参照。
        self._kill_aspen = kill_aspen or _default_kill_aspen

        # 計測（2026-07-15）: 評価グループごとの実時間記録。run_iteration が results.json
        # の timing に転記する。判定・制御には一切使わない（record-only）。
        #   groups[i] = {mode, n_requested, n_results, wall_sec, wedged, rc,
        #                eval_sec: [結果1件ごとの受信間隔秒（先頭はビルド込み）],
        #                lost_sec: wedge/異常終了で失った秒（最後の受信→終了）}
        self.timing: dict[str, Any] = {"groups": []}

    # ------------------------------------------------------------------
    # 子プロセス駆動（spawn → stream 受信 → stall 判定 → kill）
    # ------------------------------------------------------------------

    def _run_worker(self, request: dict, n: int, stall: float) -> list[dict | None]:
        """子を spawn し、stdout の結果ストリームを index 位置に埋める。

        stall : 結果無受信のまま wedge と判定するまでの秒数（モード別。__init__ 参照）。
        返り値は長さ n のリスト。受信できた index は子のメッセージ dict、
        未受信（wedge / 異常終了 / 欠落）は None。
        """
        results: list[dict | None] = [None] * n
        _t0 = time.monotonic()          # 計測: グループ全体の壁時計
        _t_last = _t0                   # 計測: 直近の結果受信時刻（受信間隔＝評価1件の実時間）
        _eval_sec: list[float] = []

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(request, f)
            req_path = f.name

        # stdout = 純 JSON チャネル、stderr = 子の診断（別パイプ。STDOUT にマージしない）。
        # 子の stdio を UTF-8 に揃える：AspenEvaluator の診断には日本語が混じり、
        # 既定 cp932 を親が strict utf-8 で読むと erdr が UnicodeDecodeError で死に、
        # 以降のクラッシュ診断が失われる。PYTHONUTF8=1 で子を UTF-8 にし、errors="replace" を保険に。
        proc = subprocess.Popen(
            [self._python, "-u", self._worker, req_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        q: queue.Queue = queue.Queue()
        SENT = object()  # stdout が閉じた（子が結果を出し終えた / 死んだ）合図
        err: list[str] = []

        def rdr() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    q.put(line)
            finally:
                q.put(SENT)

        def erdr() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    err.append(line)
            except Exception:
                pass

        threading.Thread(target=rdr, daemon=True).start()
        erdr_thread = threading.Thread(target=erdr, daemon=True)
        erdr_thread.start()

        wedged = False
        try:
            while True:
                try:
                    item = q.get(timeout=stall)
                except queue.Empty:
                    wedged = True  # stall 秒 無受信 → wedge
                    break
                if item is SENT:
                    break
                line = item.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    # stdout 上の非 JSON ノイズ（C 層やライブラリの吐き出し）は無視（防御）。
                    continue
                if not isinstance(msg, dict):
                    # valid JSON だが dict でない行（数値・文字列など）。msg.get で
                    # AttributeError になり評価ループごと巻き込むので、ノイズとして無視。
                    continue
                i = msg.get("index")
                if isinstance(i, int) and 0 <= i < n:
                    results[i] = msg
                    _now = time.monotonic()          # 計測: 受信間隔＝この1件の実時間
                    _eval_sec.append(round(_now - _t_last, 2))
                    _t_last = _now
        finally:
            # 強い手を先に出す（COORDINATION: 順番の罠の修正）。
            # proc.kill() は TerminateProcess＝COM の詰まり方に依存せず必ず効く最終手段。
            # これを最初にやると worker の in-flight COM ハンドルごと解放され、ラインは確実に
            # 前進できる。以前は下の _kill_aspen()（タイムアウト無しの os.system）が前にあり、
            # kill 不能な Aspen でそこが詰まると proc.kill() まで到達せず 85分ハングした。
            try:
                proc.kill()         # 子プロセス本体を無条件 kill（既に終了済みでも無害）
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            # worker を確実に始末した後で Aspen 本体を掃除（タイムアウト付き）。ここで取りこぼしても
            # 次の worker のビルドが起動時に taskkill するので回収される。
            if wedged:
                self._kill_aspen()
            erdr_thread.join(timeout=2)  # err tail を読む前に drain 完了を待つ
            try:
                os.remove(req_path)
            except OSError:
                pass

        abnormal = proc.returncode not in (0, None)
        # 異常終了で Aspen が残り得る場合の掃除（逐次評価＝同時 1 個なのでイメージ名 kill は安全）。
        # wedge 時は既に kill 済みなので二重には呼ばない。
        if abnormal and not wedged:
            self._kill_aspen()
        if wedged or abnormal:
            missing = sum(r is None for r in results)
            print(
                f"    [subprocess] wedged={wedged} rc={proc.returncode} "
                f"missing={missing}/{n}"
                + (f"\n    stderr tail: {''.join(err[-5:])}" if err else "")
            )

        # 計測記録（record-only。wedge/異常時は「最後の受信→終了」を lost_sec に計上）
        _t_end = time.monotonic()
        self.timing["groups"].append({
            "mode": request.get("mode"),
            "n_requested": n,
            "n_results": sum(r is not None for r in results),
            "wall_sec": round(_t_end - _t0, 2),
            "wedged": wedged,
            "rc": proc.returncode,
            "eval_sec": _eval_sec,
            "lost_sec": round(_t_end - _t_last, 2) if (wedged or abnormal) else 0.0,
        })
        return results

    # ------------------------------------------------------------------
    # Evaluator Protocol
    # ------------------------------------------------------------------

    def evaluate_topology(
        self,
        topology: dict[str, Any],
        x_list: list[list[float]],
    ) -> list[Metrics]:
        req = {
            "mode": "topology",
            "topology": dump_topology(topology),  # タプルキー arcs → list 形式（JSON 化）
            "x_list": x_list,
            "case": self._case,
            "aspen_file": self._aspen_file,
            "dmp_dir": self._dmp_dir,
        }
        raw = self._run_worker(req, len(x_list), self._stall_topology)
        return [
            Metrics(**m["metrics"]) if m is not None else Metrics.bad() for m in raw
        ]

    def evaluate_detailed(
        self,
        topology: dict[str, Any],
        x: list[float],
    ) -> DetailedResult:
        req = {
            "mode": "detailed",
            "topology": dump_topology(topology),
            "x": x,
            "case": self._case,
            "aspen_file": self._aspen_file,
            "dmp_dir": self._dmp_dir,
        }
        raw = self._run_worker(req, 1, self._stall_detailed)
        m = raw[0]
        if m is None:
            return DetailedResult(metrics=Metrics.bad())
        return DetailedResult(
            metrics=Metrics(**m["metrics"]),
            stream_results=m.get("stream_results", {}),
        )
