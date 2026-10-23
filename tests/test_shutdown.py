"""退出生命周期测试（对应「托盘退出后进程仍在」这个 Bug）。"""

from __future__ import annotations

import os
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def _controller(qapp, isolated_home, args=None):
    import argparse

    from src.config import load_settings
    from src.ui.app_ui import AppController

    args = args or argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5,
        scan_interval=0.25, stats_repository=None)
    return AppController(load_settings(), args)


def test_shutdown_stops_resident_loop(qapp, isolated_home) -> None:
    """shutdown 必须置位 stop 事件，让常驻循环退出。"""
    controller = _controller(qapp, isolated_home)
    assert controller.control.stop.is_set() is False
    controller.shutdown()
    assert controller.control.stop.is_set() is True
    assert controller.shutting_down is True


def test_shutdown_joins_engine_thread_with_timeout(qapp, isolated_home) -> None:
    """引擎线程必须在有限时间内被 join（不能无限等待）。"""
    controller = _controller(qapp, isolated_home)
    started = threading.Event()

    def fake_engine() -> None:
        started.set()
        time.sleep(30)          # 故意不响应 stop

    controller.engine_thread = threading.Thread(target=fake_engine, daemon=True)
    controller.engine_thread.start()
    started.wait(2)

    t0 = time.monotonic()
    controller.shutdown()
    elapsed = time.monotonic() - t0
    assert elapsed < 6.0, "shutdown 不能因为线程不退出而卡住"


def test_shutdown_closes_overlay(qapp, isolated_home) -> None:
    controller = _controller(qapp, isolated_home)
    overlay = controller.ensure_overlay()
    overlay.set_offers([{"augment_id": 1}])
    controller.shutdown()
    assert controller.overlay_window is None


def test_shutdown_stops_timers(qapp, isolated_home) -> None:
    from PySide6 import QtCore

    controller = _controller(qapp, isolated_home)
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(100)
    controller._timers.append(timer)
    controller.shutdown()
    assert controller._timers == []
    assert timer.isActive() is False


def test_shutdown_calls_qt_quit(qapp, isolated_home, monkeypatch) -> None:
    controller = _controller(qapp, isolated_home)
    called = {"quit": 0}
    original = qapp.quit

    def spy(*args, **kwargs):
        called["quit"] += 1
        return original()

    monkeypatch.setattr(controller, "app", type("A", (), {"quit": spy})())
    controller.shutdown()
    assert called["quit"] == 1


def test_shutdown_releases_single_instance_lock(qapp, isolated_home) -> None:
    """退出必须释放互斥体，否则立刻重启会提示"已经在运行"。"""
    from src.ui.single_instance import MUTEX_NAME, SingleInstance

    # 正式版 EXE 正在运行时它本身就持有全局互斥体：此时无法在本机验证
    probe = SingleInstance(MUTEX_NAME)
    if not probe.acquire():
        pytest.skip("正式版 LOL 海克斯助手 正在运行（全局互斥体被占用），跳过")
    probe.release()

    controller = _controller(qapp, isolated_home)
    assert controller.instance.acquire() is True
    controller.shutdown()
    again = SingleInstance(controller.instance.name)
    assert again.acquire() is True, "退出后互斥体必须可再次获取"
    again.release()


def test_shutdown_is_idempotent(qapp, isolated_home) -> None:
    controller = _controller(qapp, isolated_home)
    controller.shutdown()
    controller.shutdown()          # 二次调用不能抛异常
    assert controller.shutting_down is True


def test_close_window_hides_to_tray(qapp, isolated_home) -> None:
    """点主窗口 X 只隐藏到托盘，不退出。"""
    from PySide6 import QtGui

    from src.ui.main_window import MainWindow

    window = MainWindow(lambda: None, lambda: None, lambda: None, lambda: None,
                        lambda: None)
    window.show()
    event = QtGui.QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() is False, "普通关闭必须被忽略"
    assert window.isVisible() is False


def test_tray_exit_really_quits(qapp, isolated_home) -> None:
    """托盘退出走 force_quit，closeEvent 必须接受（真正退出）。"""
    from PySide6 import QtGui

    from src.ui.main_window import MainWindow

    window = MainWindow(lambda: None, lambda: None, lambda: None, lambda: None,
                        lambda: None)
    window.show()
    window.set_force_quit()
    event = QtGui.QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() is True


def test_tray_quit_wired_to_controller_shutdown(qapp, isolated_home) -> None:
    """托盘「退出」必须连到 controller.shutdown，而不是 app.quit。"""
    import inspect

    from src.ui import app_ui

    source = inspect.getsource(app_ui)
    assert "on_quit=controller.request_shutdown" in source


def test_engine_never_touches_qt() -> None:
    """回归：引擎线程不得直接操作 Qt（曾导致托盘退出死锁）。"""
    import ast
    import inspect

    from src import live

    source = inspect.getsource(live)
    tree = ast.parse(source)          # 只看真实代码，忽略注释/文档字符串

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            called.add(node.attr)
        if isinstance(node, ast.Import):
            for alias in node.names:
                called.add(alias.name.split(".")[0])
        if isinstance(node, ast.ImportFrom) and node.module:
            called.add(node.module.split(".")[0])

    for forbidden in ("processEvents", "launch_overlay", "PySide6", "QtWidgets"):
        assert forbidden not in called, "引擎侧不得调用/导入 %s" % forbidden


def test_overlay_sink_only_publishes_to_hub(isolated_home) -> None:
    """引擎侧 Overlay 出口只写 HUB，不持有任何 Qt 对象。"""
    from src import live
    from src.core.state_hub import StateHub

    sink = live.HubOverlay()
    assert sink.available is False
    sink.set_offers([{"augment_id": 1077}])
    snap = live.HUB.snapshot()
    assert snap.offers == [{"augment_id": 1077}]
    assert snap.overlay_visible is True
    sink.close()
    assert live.HUB.snapshot().offers == []
