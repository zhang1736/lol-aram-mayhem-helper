"""插桩版复现：精确定位「托盘退出」卡在哪一步。结果写入 research/_quit_trace.txt。"""

from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import time
import traceback

if "--native" not in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtWidgets  # noqa: E402

TRACE = os.path.join(ROOT, "research", "_quit_trace.txt")
_fh = io.open(TRACE, "w", encoding="utf-8")


def log(msg: str) -> None:
    _fh.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    _fh.flush()
    os.fsync(_fh.fileno())


import src.ui.tray as tray_mod  # noqa: E402

CAPTURED: dict = {}
_orig_init = tray_mod.TrayIcon.__init__


def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    CAPTURED["tray"] = self


tray_mod.TrayIcon.__init__ = _patched_init

# 插桩 QApplication.quit
_orig_quit = QtWidgets.QApplication.quit      # PySide6 里是静态方法


def _patched_quit(*args, **kwargs):
    log("app.quit() CALLED (args=%d)" % len(args))
    try:
        result = _orig_quit()
        log("app.quit() returned")
        return result
    except Exception:
        log("app.quit() EXCEPTION:\n" + traceback.format_exc())
        raise


QtWidgets.QApplication.quit = _patched_quit


def _dump(tag: str) -> None:
    frames = sys._current_frames()
    log("=== THREAD DUMP (%s) ===" % tag)
    for thread in threading.enumerate():
        log("  %-18s daemon=%s alive=%s" % (thread.name, thread.daemon, thread.is_alive()))
        frame = frames.get(thread.ident)
        if frame is not None:
            for entry in traceback.extract_stack(frame)[-5:]:
                log("      %s:%s %s" % (os.path.basename(entry.filename),
                                        entry.lineno, entry.name))


def main() -> int:
    from src.config import load_settings
    from src.ui.main_window import MainWindow
    from src.ui.app_ui import run_gui

    args = argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5,
        scan_interval=0.25, stats_repository=None)

    app = QtWidgets.QApplication([])

    orig_close = MainWindow.close

    def traced_close(self):
        log("MainWindow.close() enter")
        result = orig_close(self)
        log("MainWindow.close() return")
        return result

    MainWindow.close = traced_close

    def trigger_quit() -> None:
        tray = CAPTURED.get("tray")
        if tray is None:
            log("tray not captured")
            app.quit()
            return
        for action in tray.contextMenu().actions():
            if action.text() == "退出":
                log("triggering tray action 退出")
                action.trigger()
                log("tray action returned")
                return
        log("退出 menu item not found")

    QtCore.QTimer.singleShot(8000, trigger_quit)

    def watchdog() -> None:
        time.sleep(20)
        log("!! 20s after quit: process still alive -> HANG")
        _dump("hang")
        _fh.close()
        os._exit(9)

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        run_gui(load_settings(), args)
        log("run_gui RETURNED")
    except Exception:
        log("run_gui EXCEPTION:\n" + traceback.format_exc())
    _dump("main exit")
    log("script main() done")
    _fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
