"""Overlay 三卡渲染自检：用 Day 3 真实数据展示 6 秒。

    python tools/overlay_demo.py [秒数]
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.core.augment_flow import recommended_augment_ids


class Offer:
    def __init__(self, augment_id, popular, performance, name_cn):
        self.augment_id = augment_id
        self.popular = popular
        self.performance = performance
        self.name_cn = name_cn


def main(seconds: float = 6.0) -> int:
    from PySide6 import QtCore, QtWidgets

    from src.overlay.manager import launch_overlay

    # Day 3 真机实测数据（Varus / championId 110）
    offers = [
        Offer(1325, 0.49, 70.88, "玻璃大炮"),
        Offer(1115, 1.93, 81.77, "最万用的瞄准镜"),
        Offer(1079, 0.84, 80.07, "战争交响乐"),
    ]
    recommended = recommended_augment_ids(offers)
    is_tie = len(recommended) > 1
    payload = [{
        "augment_id": o.augment_id,
        "name_cn": o.name_cn,
        "performance": o.performance,
        "popular": o.popular,
        "recommended": o.augment_id in recommended,
        "tie": is_tie,
    } for o in offers]

    window = launch_overlay(screen_index=0, enabled=True)
    window.set_status(True, "InProgress")
    window.set_offers(payload)
    print("overlay 三卡已显示，推荐 = %s" % recommended)
    for item in payload:
        print("  %-14s 强度 %-7s 欢迎度 %-7s %s"
              % (item["name_cn"], item["performance"], item["popular"],
                 "★ 推荐" if item["recommended"] else ""))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    QtCore.QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()
    print("overlay demo 结束")
    return 0


if __name__ == "__main__":
    sys.exit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 6.0))
