"""Out-of-band watchdog for detecting Aspen hangs.

Even when the main thread is blocked in a COM call (typically app.Engine.IsRunning),
this thread keeps working because it only touches time / taskkill / win32gui.
If the heartbeat (beat()) is not updated for stall_sec seconds, the run is treated
as hung: the crash dialog is clicked away automatically, then AspenPlus.exe is
killed. The kill makes the blocked COM call return as an RPC-disconnect
exception (-2147023xx).

Important: this thread never touches Aspen COM objects (to avoid cross-thread
marshalling). It uses win32gui and taskkill only.

Usage:
    import aspen_watchdog
    aspen_watchdog.start_watchdog(stall_sec=120)      # at the start of a run
    try:
        with aspen_watchdog.armed():                  # arm only while touching Aspen
            run_aspen_with_timeout(...)               # calls beat() internally
    finally:
        aspen_watchdog.stop_watchdog()                # at the end of a run
"""

import subprocess
import threading
import time

try:
    import win32con
    import win32gui
    _HAS_WIN32GUI = True
except Exception:  # keep the import working on non-Windows / without pywin32
    _HAS_WIN32GUI = False

_last_activity = [time.time()]
_armed = threading.Event()
_stop = threading.Event()
_thread = None
_stall_sec = 120
_CHECK_INTERVAL = 5.0  # Worker polling interval. A seam tests shrink to trigger faster.


def beat():
    """Heartbeat. Called frequently by the code touching Aspen. No-op if the thread is down."""
    _last_activity[0] = time.time()


class armed:
    """Context manager that arms the watchdog only while Aspen is being touched."""

    def __enter__(self):
        _last_activity[0] = time.time()  # reset on entry, so a preceding slow optimizer step cannot trigger it
        _armed.set()
        return self

    def __exit__(self, exc_type, exc, tb):
        _armed.clear()
        return False  # do not swallow exceptions


def _auto_close_aspen_dialog():
    """Find a crash/error dialog and click OK or similar (best-effort).

    Aspen runs with Visible=0, so the window may not be enumerable. Failing here
    is not fatal (taskkill is the primary recovery). To avoid clicking the wrong
    window, only visible top-level windows whose title contains "Aspen" are
    considered. If real crash windows turn out to have different titles, widen
    this after observing them.
    """
    if not _HAS_WIN32GUI:
        return False
    clicked = [False]
    # Locale-dependent Windows UI button captions, matched verbatim against
    # win32gui.GetWindowText(). These are data, not prose: translating or
    # removing the Japanese entries breaks dialog dismissal on a Japanese
    # Windows locale. Do not translate.
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
        if _stop.wait(_CHECK_INTERVAL):  # check every 5 s by default (exit at once on stop)
            break
        if not _armed.is_set():
            continue
        if time.time() - _last_activity[0] > _stall_sec:
            print(f"\n  [WATCHDOG] no heartbeat for {_stall_sec}s -> treating as hung, recovering", flush=True)
            clicked = _auto_close_aspen_dialog()
            print(f"  [WATCHDOG] dialog OK click: {'done' if clicked else 'not found'}", flush=True)
            time.sleep(3)
            # The timeout is essential: if taskkill itself never returns against an
            # unkillable wedged Aspen, this thread -- the guaranteed last resort --
            # blocks too (the same countermeasure as subprocess_evaluator._default_kill_aspen).
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "AspenPlus.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=20,
                )
            except Exception:
                pass  # a timeout / failure must not stop the watchdog (it retries next cycle)
            print("  [WATCHDOG] AspenPlus killed -> the main COM call will return via RPC disconnect", flush=True)
            _last_activity[0] = time.time()  # prevent back-to-back firing


def start_watchdog(stall_sec=120):
    """Call once at the start of a run. Starts the daemon thread (not yet armed)."""
    global _thread, _stall_sec
    _stall_sec = stall_sec
    _stop.clear()
    _armed.clear()
    _last_activity[0] = time.time()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_worker, daemon=True)
        _thread.start()


def stop_watchdog():
    """Call at the end of a run."""
    _stop.set()
    _armed.clear()
    t = _thread
    if t is not None:
        t.join(timeout=10)
