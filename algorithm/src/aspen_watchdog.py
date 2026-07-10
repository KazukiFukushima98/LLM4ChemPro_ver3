"""Aspen ハング検知用のアウトオブバンド・ウォッチドッグ。

メインスレッドが COM 呼び出し（典型: app.Engine.IsRunning）でブロックしても、
このスレッドは time / taskkill / win32gui しか触らないので必ず動作する。
heartbeat（beat()）が stall_sec 秒更新されなければハングとみなし、
クラッシュダイアログを自動クリック → AspenPlus.exe を強制終了する。
kill によりブロック中の COM 呼び出しが RPC 切断(-2147023xx)で例外復帰する。

重要: このスレッドは Aspen の COM オブジェクトに一切触れない（クロススレッド
マーシャリングを避ける）。win32gui と taskkill のみ。

使い方:
    import aspen_watchdog
    aspen_watchdog.start_watchdog(stall_sec=120)      # run の最初
    try:
        with aspen_watchdog.armed():                  # Aspen を触る区間だけ武装
            run_aspen_with_timeout(...)               # 内部で beat() を呼ぶ
    finally:
        aspen_watchdog.stop_watchdog()                # run の最後
"""

import subprocess
import threading
import time

try:
    import win32con
    import win32gui
    _HAS_WIN32GUI = True
except Exception:  # 非 Windows / pywin32 不在でも import は通す
    _HAS_WIN32GUI = False

_last_activity = [time.time()]
_armed = threading.Event()
_stop = threading.Event()
_thread = None
_stall_sec = 120
_CHECK_INTERVAL = 5.0  # ワーカーのポーリング間隔。テストで小さくして発火を高速化する seam。


def beat():
    """心拍。Aspen を触る側がループ内で頻繁に呼ぶ。スレッド未起動でも無害。"""
    _last_activity[0] = time.time()


class armed:
    """Aspen を触る区間だけ watchdog を武装する context manager。"""

    def __enter__(self):
        _last_activity[0] = time.time()  # 入った瞬間に心拍リセット（直前の GA 処理の停滞で誤発動しないように）
        _armed.set()
        return self

    def __exit__(self, exc_type, exc, tb):
        _armed.clear()
        return False  # 例外は握りつぶさない


def _auto_close_aspen_dialog():
    """クラッシュ/エラーダイアログを探して OK 等をクリック（best-effort）。

    Visible=0 で走るため列挙できない可能性あり。失敗しても致命的でない
    （主たる解除手段は taskkill）。誤クリック防止のため、後輩同様
    title に "Aspen" を含む可視 top-level 窓のみを対象にする。
    実クラッシュ窓のタイトルが異なる場合はここを観測後に広げる。
    """
    if not _HAS_WIN32GUI:
        return False
    clicked = [False]
    button_texts = {"OK", "はい", "Yes", "Close", "閉じる", "終了"}

    def child_cb(c, _):
        try:
            if win32gui.GetWindowText(c) in button_texts:
                win32gui.PostMessage(c, win32con.WM_LBUTTONDOWN, 0, 0)
                win32gui.PostMessage(c, win32con.WM_LBUTTONUP, 0, 0)
                clicked[0] = True
        except Exception:
            pass

    def enum_cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return
        if "Aspen" in title:
            try:
                win32gui.EnumChildWindows(hwnd, child_cb, None)
            except Exception:
                pass

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return clicked[0]


def _worker():
    while not _stop.is_set():
        if _stop.wait(_CHECK_INTERVAL):  # 既定 5 秒ごとにチェック（stop なら即抜け）
            break
        if not _armed.is_set():
            continue
        if time.time() - _last_activity[0] > _stall_sec:
            print(f"\n  [WATCHDOG] {_stall_sec}s 心拍なし → ハングとみなし対処", flush=True)
            clicked = _auto_close_aspen_dialog()
            print(f"  [WATCHDOG] dialog OK click: {'done' if clicked else 'not found'}", flush=True)
            time.sleep(3)
            # タイムアウト必須：kill 不能な wedge Aspen で taskkill 自体が返らないと
            # 「保証付き最終手段」であるこのスレッドごと詰まる（run13 の 85 分ハングと同型。
            # subprocess_evaluator._default_kill_aspen と同じ対策）。
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "AspenPlus.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=20,
                )
            except Exception:
                pass  # timeout / 失敗でも watchdog を止めない（次周期で再試行される）
            print("  [WATCHDOG] AspenPlus killed → メインの COM 呼び出しが RPC 切断で復帰します", flush=True)
            _last_activity[0] = time.time()  # 連続発動防止


def start_watchdog(stall_sec=120):
    """run の最初に1回呼ぶ。daemon スレッドを起動（武装はまだしない）。"""
    global _thread, _stall_sec
    _stall_sec = stall_sec
    _stop.clear()
    _armed.clear()
    _last_activity[0] = time.time()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_worker, daemon=True)
        _thread.start()


def stop_watchdog():
    """run の最後に呼ぶ。"""
    _stop.set()
    _armed.clear()
    t = _thread
    if t is not None:
        t.join(timeout=10)
