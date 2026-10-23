"""测量引擎线程发布 -> Qt 主线程应用三卡内容的信号延迟。

使用 offscreen 平台，只验证线程切换与 Overlay 控件更新成本；真实游戏画面
是否可见仍需在 Windows 前台运行 EXE 验收。
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from src.overlay.window import OverlayWindow  # noqa: E402

OFFERS = [
    {"augment_id": 1325, "name_cn": "玻璃大炮", "performance": 70.88,
     "popular": 0.49, "recommended": False},
    {"augment_id": 1115, "name_cn": "最万用的瞄准镜", "performance": 81.77,
     "popular": 1.93, "recommended": True, "tie": False},
    {"augment_id": 1079, "name_cn": "战争交响乐", "performance": 80.07,
     "popular": 0.84, "recommended": False},
]


class Bridge(QtCore.QObject):
    changed = QtCore.Signal()


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[int(round((len(ordered) - 1) * ratio))]


def run(iterations: int = 30) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = OverlayWindow()
    window.show_overlay()
    bridge = Bridge()
    lock = threading.Lock()
    pending_at = [0.0]
    done = threading.Event()
    latencies: list[float] = []

    def apply() -> None:
        started = pending_at[0]
        window.set_offers(OFFERS)
        app.processEvents()
        latencies.append((time.perf_counter() - started) * 1000.0)
        done.set()

    bridge.changed.connect(apply, QtCore.Qt.QueuedConnection)

    def worker() -> None:
        for _ in range(iterations):
            with lock:
                pending_at[0] = time.perf_counter()
            done.clear()
            bridge.changed.emit()
            # 让主线程处理事件；worker 不碰 Qt 窗口。
            while not done.wait(0.005):
                pass

    thread = threading.Thread(target=worker, name="benchmark-publisher",
                              daemon=True)
    thread.start()
    deadline = time.perf_counter() + 10.0
    while thread.is_alive() and time.perf_counter() < deadline:
        QTest.qWait(1)
    thread.join(timeout=1.0)
    window.close()
    if len(latencies) != iterations:
        raise SystemExit("只完成 %d/%d 次信号测量" % (len(latencies), iterations))

    print("Qt 跨线程 HUB 信号 -> 三卡控件应用:")
    print("  P50=%.2f ms P95=%.2f ms max=%.2f ms"
          % (statistics.median(latencies), percentile(latencies, 0.95),
             max(latencies)))


if __name__ == "__main__":
    run()
