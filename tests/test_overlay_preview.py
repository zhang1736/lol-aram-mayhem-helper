"""预览悬浮窗生命周期测试。

回归 Bug：预览显示后 172ms 就被 sync_overlay 用空 HUB 状态清掉。
"""

from __future__ import annotations

import os
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PREVIEW_SECONDS = 0.4          # 测试用短时长，避免真的等 5 秒


@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def _controller(qapp, isolated_home):
    import argparse

    from src.config import load_settings
    from src.ui.app_ui import AppController

    args = argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5,
        scan_interval=0.25, stats_repository=None)
    return AppController(load_settings(), args)


def _spin(seconds: float) -> None:
    """在测试里驱动 Qt 事件循环（不阻塞主线程）。"""
    from PySide6 import QtCore, QtTest

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        QtTest.QTest.qWait(20)


def test_preview_stays_visible_for_full_duration(qapp, isolated_home) -> None:
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    assert controller.show_preview(PREVIEW_SECONDS) is True
    assert controller.overlay_mode is OverlayMode.PREVIEW

    # 中途数百毫秒内不允许被任何清理逻辑关掉（回归点：曾被 172ms 清掉）
    for _ in range(3):
        controller.sync_overlay()
        _spin(0.08)
        assert controller.overlay_mode is OverlayMode.PREVIEW
        assert controller._last_overlay_payload, "预览数据不能在计时内被清空"
    controller.shutdown()


def test_preview_cleanup_after_duration(qapp, isolated_home) -> None:
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    controller.show_preview(PREVIEW_SECONDS)
    _spin(PREVIEW_SECONDS + 0.3)
    assert controller.overlay_mode is OverlayMode.HIDDEN
    assert controller._last_overlay_payload == []
    controller.shutdown()


def test_live_cleanup_does_not_hide_preview(qapp, isolated_home) -> None:
    """真实游戏的"空牌面"清理不得影响预览。"""
    from src.overlay.manager import OverlayMode
    from src.core.state_hub import HUB

    controller = _controller(qapp, isolated_home)
    controller.show_preview(PREVIEW_SECONDS)
    HUB.update(offers=[], overlay_visible=False)
    controller.sync_overlay()
    assert controller.overlay_mode is OverlayMode.PREVIEW
    assert controller._last_overlay_payload
    controller.shutdown()


def test_game_end_does_not_hide_preview(qapp, isolated_home) -> None:
    """GAME_END 会清 GameSession / offers，但不能清预览。"""
    from src.core.state_hub import HUB
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    controller.show_preview(PREVIEW_SECONDS)
    HUB.update(offers=[], overlay_visible=False, gameflow="None",
               stage=None, level=None, champion_id=None, offer_version=0,
               changed_slots=[], rerolled_slots=[], recommended=[])
    controller.sync_overlay()
    assert controller.overlay_mode is OverlayMode.PREVIEW
    controller.shutdown()


def test_preview_reclick_resets_timer(qapp, isolated_home) -> None:
    """预览期间再次点击：重新计时，且不产生多个定时器。"""
    controller = _controller(qapp, isolated_home)
    controller.show_preview(PREVIEW_SECONDS)
    first_timer = controller._preview_timer
    _spin(PREVIEW_SECONDS * 0.6)

    controller.show_preview(PREVIEW_SECONDS)      # 重新点击
    assert controller._preview_timer is not first_timer
    assert first_timer.isActive() is False
    assert len([t for t in controller._timers if t is not None]) <= 2

    # 从第二次点击开始重新计时
    _spin(PREVIEW_SECONDS * 0.6)
    assert controller._last_overlay_payload, "重新点击后不应在原定时间点消失"
    controller.shutdown()


def test_real_augment_select_interrupts_preview(qapp, isolated_home) -> None:
    """真实海克斯出现 -> 立即结束预览并切 LIVE，测试数据不能覆盖真实数据。"""
    from src.core.state_hub import HUB
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    controller.show_preview(5.0)                  # 故意长一点，验证被打断
    real = [{"augment_id": 1077, "name_cn": "灵魂虹吸", "performance": 97.24,
             "popular": 6.82, "recommended": True}]
    HUB.update(offers=real)
    controller.sync_overlay()
    assert controller.overlay_mode is OverlayMode.LIVE
    assert controller._last_overlay_payload == real
    controller.shutdown()


def test_reconnect_interrupts_preview(qapp, isolated_home) -> None:
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    controller.show_preview(5.0)
    assert controller.overlay_mode is OverlayMode.PREVIEW
    controller.prepare_reconnect()
    assert controller.overlay_mode is OverlayMode.HIDDEN
    controller.shutdown()


def test_app_shutdown_closes_preview(qapp, isolated_home) -> None:
    controller = _controller(qapp, isolated_home)
    controller.show_preview(30.0)
    assert controller._preview_timer is not None
    controller.shutdown()
    assert controller._preview_timer is None
    assert controller.overlay_window is None


def test_preview_button_reports_failure_when_overlay_unavailable(qapp, isolated_home,
                                                               monkeypatch) -> None:
    """悬浮窗创建失败时必须返回 False（提示"显示失败"而不是先报成功）。"""
    controller = _controller(qapp, isolated_home)

    def boom(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(controller, "ensure_overlay", boom)
    assert controller.show_preview(1.0) is False
