"""复现「托盘 → 退出」卡死：触发与托盘菜单完全相同的退出动作，观察进程是否退出。

用法（子进程会被外层超时杀掉，从而判断是否卡住）：
    python tools/repro_quit.py
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import traceback

if "--native" not in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6 import QtCore, QtWidgets  # noqa: E402

import src.ui.tray as tray_mod  # noqa: E402

CAPTURED: dict = {}
_orig_init = tray_mod.TrayIcon.__init__


def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    CAPTURED["tray"] = self


tray_mod.TrayIcon.__init__ = _patched_init


def _dump_threads(tag: str) -> None:
    frames = sys._current_frames()
    print("\n=== THREAD DUMP (%s) ===" % tag, flush=True)
    for thread in threading.enumerate():
        print("  thread %-18s daemon=%-5s alive=%s"
              % (thread.name, thread.daemon, thread.is_alive()), flush=True)
        frame = frames.get(thread.ident)
        if frame is not None:
            stack = traceback.extract_stack(frame)[-4:]
            for entry in stack:
                print("      %s:%s in %s" % (os.path.basename(entry.filename),
                                             entry.lineno, entry.name), flush=True)


def main() -> int:
    from src.config import load_settings
    from src.ui.app_ui import run_gui

    args = argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5,
        scan_interval=0.25, stats_repository=None)

    app = QtWidgets.QApplication([])

    def trigger_quit() -> None:
        tray = CAPTURED.get("tray")
        if tray is None:
            print("!! tray not captured", flush=True)
            app.quit()
            return
        for action in tray.contextMenu().actions():
            if action.text() == "退出":
                print(">>> triggering tray action 退出", flush=True)
                action.trigger()
                break
        else:
            print("!! 未找到退出菜单项", flush=True)

    QtCore.QTimer.singleShot(8000, trigger_quit)

    def watchdog() -> None:
        import time
        time.sleep(22)
        print("!! 退出动作已发出但进程仍在（判定为卡死）", flush=True)
        _dump_threads("after quit")
        os._exit(9)          # 仅诊断脚本使用，用于让外层拿到结论

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        run_gui(load_settings(), args)
        print(">>> run_gui returned; 主流程已结束", flush=True)
    except Exception:
        traceback.print_exc()
    _dump_threads("main exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
