"""Overlay 窗口：透明 / 无边框 / 置顶 / 鼠标穿透。

Day 4：显示三张海克斯卡片（名称 / 强度 / 欢迎度 / ★推荐）。

PySide6 采用「导入即尝试、失败则降级」的方式，
保证未安装 PySide6 时项目其它模块仍可正常 import。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Sequence

from src.overlay.geometry import overlay_card_positions
from src.ocr.roi import build_roi_set
from src.overlay.widgets import (
    CARD_STYLE,
    STYLE,
    format_performance_line,
    format_popular_line,
    format_recommend_label,
    overlay_text,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

APP_NAME = "ARAM Helper"
RECOMMEND_REASON = "按当前英雄 OP.GG Aggregate 欢迎度最高"

try:  # pragma: no cover - 依赖可选安装
    from PySide6 import QtCore, QtGui, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    PYSIDE6_AVAILABLE = False

MISSING_PYSIDE_MESSAGE = (
    "PySide6 is not installed.\n"
    "Core services can run, but Overlay is unavailable."
)


def _enable_click_through(widget) -> None:
    """Windows 下补充 WS_EX_TRANSPARENT | WS_EX_LAYERED，确保鼠标穿透。"""
    if sys.platform != "win32":  # pragma: no cover
        return
    try:
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("设置鼠标穿透失败: %s", exc)


if PYSIDE6_AVAILABLE:  # pragma: no cover - 需要安装 PySide6

    class AugmentCard(QtWidgets.QFrame):
        """单个海克斯信息卡。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("augmentCard")
            self.setStyleSheet(CARD_STYLE)
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(2)

            self.name = QtWidgets.QLabel("-")
            self.name.setObjectName("augmentName")
            self.name.setWordWrap(True)
            self.performance = QtWidgets.QLabel("强度 -")
            self.performance.setObjectName("augmentLine")
            self.popular = QtWidgets.QLabel("欢迎度 -")
            self.popular.setObjectName("augmentLine")
            self.recommend = QtWidgets.QLabel("")
            self.recommend.setObjectName("augmentRecommend")
            self.recommend.setWordWrap(True)

            for widget in (self.name, self.performance, self.popular, self.recommend):
                layout.addWidget(widget)

        def update_offer(self, offer: dict) -> None:
            # 正式 UI 只显示"已确认快照"（后台识别过程不进入界面）。
            # 刷新/动画期间展示的就是上一份完整确认结果。
            self.name.setText(offer.get("name_cn") or "无法识别海克斯")
            # 缺失时显示"—"（OP.GG 正在请求 / 请求失败），绝不用 0 顶替
            self.performance.setText(format_performance_line(offer.get("performance")))
            self.popular.setText(format_popular_line(offer.get("popular")))

            if offer.get("recommended"):
                self.setObjectName("augmentCardRecommended")
                self.recommend.setText("%s\n%s"
                                       % (format_recommend_label(bool(offer.get("tie"))),
                                          RECOMMEND_REASON))
            else:
                self.setObjectName("augmentCard")
                self.recommend.setText("")
            self.setStyleSheet(CARD_STYLE)

    class OverlayWindow(QtWidgets.QWidget):
        """全屏透明覆盖层：状态面板 + 三张海克斯卡片。"""

        def __init__(self, screen_index: int = 0, opacity: int = 100,
                     offset_x: int = 0, offset_y: int = 0) -> None:
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.setWindowFlags(
                QtCore.Qt.FramelessWindowHint
                | QtCore.Qt.WindowStaysOnTopHint
                | QtCore.Qt.Tool
            )
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
            self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            self.setStyleSheet(STYLE)
            self.setWindowOpacity(max(0, min(100, int(opacity))) / 100.0)

            self.screen_index = screen_index
            self.opacity = max(0, min(100, int(opacity)))
            self.offset_x = int(offset_x)
            self.offset_y = int(offset_y)
            self._screen = self._select_screen(screen_index)
            self._roi = None

            self._title = QtWidgets.QLabel(APP_NAME)
            self._title.setObjectName("title")
            self._status = QtWidgets.QLabel("LCU: Connecting...")
            self._status.setObjectName("status")
            self._panel = QtWidgets.QFrame()
            self._panel.setObjectName("card")
            panel_layout = QtWidgets.QVBoxLayout(self._panel)
            panel_layout.setContentsMargins(14, 10, 14, 10)
            panel_layout.addWidget(self._title)
            panel_layout.addWidget(self._status)

            self._cards = [AugmentCard(self) for _ in range(3)]
            for card in self._cards:
                card.hide()

            self._place_on_screen()

        def _select_screen(self, screen_index: int):
            screens = QtGui.QGuiApplication.screens()
            if not screens:
                return None
            return screens[min(screen_index, len(screens) - 1)]

        def screen_size(self) -> tuple[int, int]:
            if self._screen is None:
                return (2560, 1440)
            geometry = self._screen.geometry()
            return (geometry.width(), geometry.height())

        def _place_on_screen(self) -> None:
            if self._screen is None:
                return
            geometry = self._screen.geometry()
            self.setGeometry(geometry)
            self._panel.adjustSize()
            self._panel.move(24, 24)

            width, height = self.screen_size()
            self._roi = build_roi_set(width, height)
            # 位置计算抽到 src/overlay/geometry.py（纯几何，可单测）：
            # 默认整体在卡片下方 70px 处，避免遮挡海克斯描述文字。
            positions = overlay_card_positions(width, height,
                                               offset_x=self.offset_x,
                                               offset_y=self.offset_y,
                                               count=len(self._cards))
            for card, (left, top, card_width) in zip(self._cards, positions):
                card.setFixedWidth(max(180, card_width))
                card.adjustSize()
                card.move(left, top)

        def set_opacity(self, opacity: int) -> None:
            """透明度 0~100。"""
            self.opacity = max(0, min(100, int(opacity)))
            self.setWindowOpacity(self.opacity / 100.0)

        def set_offset(self, offset_x: int, offset_y: int) -> None:
            """整体位置偏移（像素）。"""
            self.offset_x, self.offset_y = int(offset_x), int(offset_y)
            self._place_on_screen()

        def set_status(self, lcu_connected: bool, phase: str | None = None) -> None:
            _, status = overlay_text(APP_NAME, lcu_connected, phase)
            self._status.setText(status)

        def set_offers(self, offers: Sequence[dict] | None) -> None:
            self.update_offers(offers)

        def update_offers(self, offers: Sequence[dict] | None,
                          pending_slots=()) -> None:
            """更新三张卡（**只接受已确认快照**）。

            `pending_slots` 只是传给 Debug 用的内部标记，**不影响任何 UI 文案**：
            刷新/动画期间调用方应当继续传上一份完整快照。
            """
            items = [dict(o) for o in (offers or [])]
            for slot in pending_slots or ():
                try:
                    index = int(slot)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(items):
                    items[index]["pending"] = True       # 仅供 Debug 显示
            for index, card in enumerate(self._cards):
                if index < len(items):
                    card.update_offer(items[index])
                    card.adjustSize()
                    card.show()
                else:
                    card.hide()

        def show_overlay(self) -> None:
            self.show()
            _enable_click_through(self)

else:  # pragma: no cover - 未安装 PySide6 时的占位实现

    class OverlayWindow:  # type: ignore[no-redef]
        """PySide6 缺失时的占位类：实例化即给出明确错误。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(MISSING_PYSIDE_MESSAGE)

        def set_status(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(MISSING_PYSIDE_MESSAGE)

        def set_offers(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(MISSING_PYSIDE_MESSAGE)

        def show_overlay(self) -> None:
            raise RuntimeError(MISSING_PYSIDE_MESSAGE)


__all__ = ["OverlayWindow", "PYSIDE6_AVAILABLE", "MISSING_PYSIDE_MESSAGE",
           "APP_NAME", "RECOMMEND_REASON"]
