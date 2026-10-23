"""主窗口：状态 / 数据 / 悬浮窗 三段。

面向普通用户：不显示 Gameflow 内部值、不显示英雄 ID、Stage 用「第 N 次海克斯」。
调试信息一律放在 Debug 页面。
"""

from __future__ import annotations

from typing import Callable, Optional

from src.core.state_hub import AppSnapshot, HUB
from src.ui.texts import (
    gameflow_text,
    hero_scope_text,
    hero_text,
    overlay_status_text,
    patch_text,
    stage_text,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class MainWindow(QtWidgets.QWidget):
        """关闭按钮 = 隐藏到托盘，不退出程序。"""

        FEEDBACK_MS = 4000

        def __init__(self, on_settings: Callable[[], None],
                     on_preview_overlay: Callable[[], None],
                     on_reconnect: Callable[[], None],
                     on_quit: Callable[[], None],
                     on_debug: Optional[Callable[[], None]] = None,
                     on_check_update: Optional[Callable[[], None]] = None,
                     current_version: str = "") -> None:
            super().__init__()
            self._on_quit = on_quit
            self._force_quit = False
            self._feedback_timer = QtCore.QTimer(self)
            self._feedback_timer.setSingleShot(True)
            self._feedback_timer.timeout.connect(lambda: self.feedback.setText(""))

            self.setWindowTitle("LOL 海克斯助手")
            self.setMinimumWidth(380)
            layout = QtWidgets.QVBoxLayout(self)

            title = QtWidgets.QLabel("LOL 海克斯助手")
            title.setStyleSheet("font-size:15px;font-weight:bold;")
            layout.addWidget(title)

            layout.addWidget(_section("状态"))
            self.client_value = _row(layout, "LOL Client")
            self.phase_value = _row(layout, "游戏状态")
            self.champion_value = _row(layout, "当前英雄")
            self.stage_value = _row(layout, "海克斯阶段")

            layout.addWidget(_section("数据"))
            self.hero_scope_value = _row(layout, "英雄统计")
            self.augment_scope_value = _row(layout, "海克斯统计")
            self.patch_value = _row(layout, "Patch")

            layout.addWidget(_section("悬浮窗"))
            self.overlay_value = _row(layout, "状态")

            layout.addWidget(_section("版本与更新"))
            self.version_value = _row(layout, "当前版本")
            self.version_value.setText("v%s" % (current_version or "-"))
            update_row = QtWidgets.QHBoxLayout()
            self.update_button = QtWidgets.QPushButton("检查更新")
            self.update_button.clicked.connect(on_check_update or (lambda: None))
            self.update_status = QtWidgets.QLabel("")
            self.update_status.setStyleSheet("color:#8c96a6;font-size:12px;")
            update_row.addWidget(self.update_button)
            update_row.addWidget(self.update_status, 1)
            layout.addLayout(update_row)

            buttons = QtWidgets.QGridLayout()
            for index, (text, slot) in enumerate([
                ("预览悬浮窗", on_preview_overlay),
                ("设置", on_settings),
                ("重新连接 LOL", on_reconnect),
                ("Debug", on_debug or (lambda: None)),
            ]):
                button = QtWidgets.QPushButton(text)
                button.clicked.connect(slot)
                buttons.addWidget(button, index // 2, index % 2)
            layout.addLayout(buttons)

            self.feedback = QtWidgets.QLabel("")
            self.feedback.setStyleSheet("color:#3f8f4f;font-size:12px;")
            layout.addWidget(self.feedback)

            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(500)
            self.refresh()

        # -- 反馈 -----------------------------------------------------------
        def set_feedback(self, text: str, error: bool = False) -> None:
            self.feedback.setStyleSheet(
                "color:%s;font-size:12px;" % ("#c0563f" if error else "#3f8f4f"))
            self.feedback.setText(text)
            self._feedback_timer.start(self.FEEDBACK_MS)

        def set_update_status(self, text: str, error: bool = False) -> None:
            """更新检查/下载状态（主窗口"版本与更新"区域）。"""
            self.update_status.setStyleSheet(
                "color:%s;font-size:12px;" % ("#c0563f" if error else "#3f8f4f"))
            self.update_status.setText(text)

        # -- 刷新 -----------------------------------------------------------
        def refresh(self) -> None:
            snap: AppSnapshot = HUB.snapshot()
            self.client_value.setText("● 已连接" if snap.client_connected else "○ 未连接")
            self.phase_value.setText(gameflow_text(snap.gameflow))
            self.champion_value.setText(hero_text(snap.champion_name, snap.champion_alias))
            self.stage_value.setText(stage_text(snap.stage, snap.level))
            self.hero_scope_value.setText(hero_scope_text(snap.data_region, snap.data_tier))
            self.augment_scope_value.setText(snap.augment_scope)
            self.patch_value.setText(patch_text(snap.data_patch))
            self.overlay_value.setText(overlay_status_text(
                snap.client_connected, bool(snap.offers), snap.overlay_visible))

        # -- 关闭 = 隐藏 -----------------------------------------------------
        def set_force_quit(self) -> None:
            self._force_quit = True

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
            if self._force_quit:
                event.accept()
                return
            event.ignore()
            self.hide()
            logger.info("主窗口已隐藏到托盘（程序继续常驻）")

    def _section(text: str):
        label = QtWidgets.QLabel("──────── %s ────────" % text)
        label.setStyleSheet("color:#8c96a6;margin-top:8px;")
        return label

    def _row(layout, label_text: str):
        row = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(label_text)
        name.setMinimumWidth(110)
        value = QtWidgets.QLabel("-")
        value.setStyleSheet("font-weight:bold;")
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(value)
        layout.addLayout(row)
        return value

else:  # pragma: no cover

    class MainWindow:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["MainWindow"]
