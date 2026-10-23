"""复现「预览悬浮窗过早消失」：记录每一次 set_offers 调用与时间戳。"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtWidgets  # noqa: E402

TRACE = os.path.join(ROOT, "research", "_preview_trace.txt")
_fh = io.open(TRACE, "w", encoding="utf-8")
_t0 = time.monotonic()


def log(msg: str) -> None:
    _fh.write("%7.3fs  %s\n" % (time.monotonic() - _t0, msg))
    _fh.flush()
    os.fsync(_fh.fileno())


from src.overlay.window import OverlayWindow  # noqa: E402

_orig_set_offers = OverlayWindow.set_offers


def _traced_set_offers(self, offers):
    payload = list(offers or [])
    log("set_offers(count=%d) ids=%s" % (len(payload),
                                         [o.get("augment_id") for o in payload]))
    return _orig_set_offers(self, payload)


OverlayWindow.set_offers = _traced_set_offers


def main() -> int:
    from src.config import load_settings
    from src.ui.app_ui import PREVIEW_OFFERS, run_gui

    args = argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5,
        scan_interval=0.25, stats_repository=None)

    app = QtWidgets.QApplication([])
    clicked = {"done": False}

    def click_preview() -> None:
        log(">>> clicking 预览悬浮窗")
        texts = []
        for button in QtWidgets.QApplication.allWidgets():
            if isinstance(button, QtWidgets.QPushButton):
                texts.append(button.text())
                if button.text() == "预览悬浮窗":
                    button.click()
                    clicked["done"] = True
                    log(">>> clicked")
                    return
        log("!! buttons seen: %s" % texts)

    QtCore.QTimer.singleShot(3000, click_preview)

    def report() -> None:
        log(">>> 6s after preview click: 预览是否还在？见上面的 set_offers 记录")
        _fh.close()
        os._exit(0)

    QtCore.QTimer.singleShot(9000, report)

    run_gui(load_settings(), args)
    _fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
