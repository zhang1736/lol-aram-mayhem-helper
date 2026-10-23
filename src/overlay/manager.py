"""Overlay 生命周期管理（延迟导入 PySide6）。"""

from __future__ import annotations

import sys

from enum import Enum

from src.utils.logger import get_logger

logger = get_logger(__name__)


class OverlayMode(str, Enum):
    """悬浮窗模式。

    PREVIEW 期间，真实游戏的清理逻辑（GAME_END / 非 AUGMENT_SELECT /
    连续 3 次识别失败 / Offer 消失 / Stage 变化 / GameSession reset）
    都**不允许**清除悬浮窗。
    """

    HIDDEN = "HIDDEN"
    PREVIEW = "PREVIEW"
    LIVE = "LIVE"


class OverlayUnavailableError(RuntimeError):
    """PySide6 未安装时抛出。"""


def launch_overlay(screen_index: int = 0, enabled: bool = True,
                   opacity: int = 100, offset_x: int = 0, offset_y: int = 0):
    """创建并显示 Overlay 窗口。

    未安装 PySide6 时抛出 OverlayUnavailableError（调用方负责优雅降级）。
    """
    if not enabled:
        return None
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except ImportError as exc:
        raise OverlayUnavailableError(
            "PySide6 is not installed. "
            "Install it before launching the overlay."
        ) from exc

    # 关键：QApplication 必须先于任何 QWidget 创建，
    # 否则 Qt 会直接 fail-fast（0xC0000409）。
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication(sys.argv)

    from src.overlay.window import OverlayWindow

    window = OverlayWindow(screen_index=screen_index, opacity=opacity,
                           offset_x=offset_x, offset_y=offset_y)
    window.show_overlay()
    logger.info("Overlay 已启动 (screen_index=%s)", screen_index)
    return window


class OverlayManager:
    """持有 Overlay 窗口并同步状态文本。"""

    def __init__(self, window=None) -> None:
        self.window = window

    @property
    def available(self) -> bool:
        return self.window is not None

    def update(self, lcu_connected: bool, phase: str | None = None) -> None:
        if self.window is None:
            return
        self.window.set_status(lcu_connected, phase)

    def set_offers(self, offers) -> None:
        """更新三张海克斯卡片。"""
        self.update_offers(offers)

    def update_offers(self, offers, pending_slots=()) -> None:
        """更新三张卡片（只接受"已确认快照"）。

        `pending_slots` 只是内部调试标记，**不改变任何 UI 文案**，
        也**不会**隐藏整块 Overlay。
        """
        if self.window is None:
            return
        try:
            updater = getattr(self.window, "update_offers", None)
            if updater is not None:
                updater(offers, pending_slots)
            else:  # pragma: no cover - 旧窗口实现的兜底
                self.window.set_offers(offers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("更新 Overlay 失败: %s", exc)

    def close(self) -> None:
        if self.window is not None:
            try:
                self.window.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 Overlay 失败: %s", exc)
            self.window = None


__all__ = ["launch_overlay", "OverlayManager", "OverlayUnavailableError",
           "OverlayMode"]
