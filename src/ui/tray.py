"""系统托盘图标与右键菜单。"""

from __future__ import annotations

from typing import Callable

from src.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover
    from PySide6 import QtGui, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtGui = QtWidgets = None
    PYSIDE6_AVAILABLE = False


def _build_icon():
    """没有 ico 资源时画一个简洁的占位图标（避免 FileNotFoundError）。"""
    from pathlib import Path

    from src.utils import paths

    icon_path = Path(paths.resource_dir()) / "assets" / "app.ico"
    if icon_path.exists():
        icon = QtGui.QIcon(str(icon_path))
        if not icon.isNull():
            return icon

    pixmap = QtGui.QPixmap(32, 32)
    pixmap.fill(QtGui.QColor(0, 0, 0, 0))
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtGui.QColor(240, 200, 90))
    painter.setPen(QtGui.QColor(40, 40, 40))
    painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
    painter.setPen(QtGui.QColor(30, 30, 30))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(11)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), QtGui.Qt.AlignCenter, "H")
    painter.end()
    return QtGui.QIcon(pixmap)


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class TrayIcon(QtWidgets.QSystemTrayIcon):
        """托盘：显示主窗口 / Overlay 开关 / 设置 / 重连 / 日志 / 退出。"""

        def __init__(self, parent,
                     on_show_window: Callable[[], None],
                     on_toggle_overlay: Callable[[], None],
                     on_settings: Callable[[], None],
                     on_reconnect: Callable[[], None],
                     on_open_logs: Callable[[], None],
                     on_quit: Callable[[], None],
                     on_check_update: Callable[[], None] | None = None) -> None:
            super().__init__(_build_icon(), parent)
            self.setToolTip("LOL 海克斯助手")
            self._on_toggle_overlay = on_toggle_overlay

            menu = QtWidgets.QMenu()
            actions = [
                ("显示主窗口", on_show_window),
                ("检查更新", on_check_update),
                ("显示/隐藏 Overlay", self._toggle_overlay),
                (None, None),
                ("设置", on_settings),
                ("重新连接 LOL", on_reconnect),
                ("查看日志", on_open_logs),
                (None, None),
                ("退出", on_quit),
            ]
            for text, slot in actions:
                if text is None:
                    menu.addSeparator()
                    continue
                action = menu.addAction(text)
                if slot is not None:
                    action.triggered.connect(slot)
            self.setContextMenu(menu)
            self.activated.connect(self._on_activated)
            self._on_show_window = on_show_window

        def _toggle_overlay(self) -> None:
            self._on_toggle_overlay()

        def _on_activated(self, reason) -> None:
            if reason in (QtWidgets.QSystemTrayIcon.DoubleClick,
                          QtWidgets.QSystemTrayIcon.Trigger):
                self._on_show_window()

        def notify(self, title: str, message: str) -> None:
            try:
                self.showMessage(title, message, _build_icon(), 4000)
            except Exception as exc:  # noqa: BLE001
                logger.debug("托盘通知失败: %s", exc)

else:  # pragma: no cover

    class TrayIcon:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["TrayIcon"]
