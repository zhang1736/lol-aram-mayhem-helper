"""成品 GUI 入口。

线程模型（重要）：
    * Qt 的一切（窗口、托盘、Overlay）只允许在**主线程**操作
    * 常驻引擎跑在后台线程，只通过 StateHub 发布状态，**绝不碰 Qt**
    * 主线程用 QTimer 定期把 HUB 状态应用到 Overlay

实测教训：引擎线程直接 `launch_overlay()` / `processEvents()` / `set_offers()`
会让「托盘 → 退出」时主线程卡死在 `app.quit()`（Qt 非线程安全）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from typing import Optional

from src.config import Settings
from src.core.state_hub import HUB
from src.overlay.manager import OverlayMode
from src.ui.single_instance import SingleInstance
from src.utils import paths
from src.utils.logger import get_logger, get_tagged

logger = get_logger(__name__)
app_log = get_tagged("client", "app.lifecycle")
overlay_log = get_tagged("overlay")
client_log = get_tagged("client")
perf_log = get_tagged("flow", "perf")

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
    PYSIDE6_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False
    PYSIDE6_IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)


ALREADY_RUNNING_TEXT = "LOL 海克斯助手已经在运行"

#: 预览悬浮窗用的模拟数据（Day 3 真机实测样本）
PREVIEW_OFFERS = [
    {"augment_id": 1325, "name_cn": "玻璃大炮", "performance": 71.75,
     "popular": 0.50, "recommended": False},
    {"augment_id": 1115, "name_cn": "最万用的瞄准镜", "performance": 82.28,
     "popular": 1.91, "recommended": True, "tie": False},
    {"augment_id": 1079, "name_cn": "战争交响乐", "performance": 79.73,
     "popular": 0.84, "recommended": False},
]

JOIN_TIMEOUT = 2.0


def _open_logs() -> None:
    directory = paths.user_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(directory))      # noqa: S606 - 本地目录
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(directory)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("打开日志目录失败: %s", exc)


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class _HubSignalBridge(QtCore.QObject):
        """把引擎线程的状态发布安全地排队到 Qt 主线程。"""

        changed = QtCore.Signal()

    class AppController:
        """统一持有所有需要清理的资源，并提供唯一的 shutdown()。"""

        def __init__(self, settings: Settings, args) -> None:
            from src import live as live_mod

            self.settings = settings
            self.args = args
            self.control = live_mod.Control()
            self.instance = SingleInstance()
            self.engine_thread: Optional[threading.Thread] = None
            self._shutting_down = False
            self._shutdown_done = False

            self.app: Optional[QtWidgets.QApplication] = None
            self.window = None
            self.tray = None
            self.overlay_window = None
            self.debug_window = None
            self._timers: list = []
            self._overlay_user_visible = True
            self._last_overlay_payload: list = []
            self._last_applied_trace_id = 0
            #: HIDDEN / PREVIEW / LIVE —— 预览期间必须屏蔽真实游戏的清理逻辑
            self.overlay_mode: OverlayMode = OverlayMode.HIDDEN
            self._preview_timer = None
            #: HUB -> Qt 主线程的即时通知；保留 1s 定时器作为兜底。
            self._hub_bridge = _HubSignalBridge()
            self._hub_bridge.changed.connect(
                self.sync_overlay, QtCore.Qt.QueuedConnection)
            self._hub_listener = HUB.subscribe(self._notify_overlay_change)
            #: 更新控制器（启动自动检查 / 手动检查 / 下载安装）
            self.update_controller = None

        def _notify_overlay_change(self, changed_fields: set[str]) -> None:
            """可能由引擎线程调用；Signal 会排队到主线程执行。"""
            if "offers" in changed_fields or "overlay_visible" in changed_fields:
                self._hub_bridge.changed.emit()

        # -- 生命周期 -------------------------------------------------------
        @property
        def shutting_down(self) -> bool:
            return self._shutting_down

        def start_engine(self) -> None:
            from src import live as live_mod

            def engine() -> None:
                try:
                    asyncio.run(live_mod.resident(self.settings, self.args,
                                                  control=self.control))
                except Exception as exc:  # noqa: BLE001
                    logger.error("常驻引擎异常: %s", exc, exc_info=True)
                finally:
                    app_log.info("[APP] Resident loop stopping")

            self.engine_thread = threading.Thread(target=engine, name="resident-engine",
                                                  daemon=True)
            self.engine_thread.start()
            app_log.info("引擎线程已启动 (daemon=True)")

        def request_shutdown(self) -> None:
            """托盘「退出」等的唯一入口。"""
            self.shutdown()

        def shutdown(self) -> None:
            """固定顺序清理；任何一步失败都不阻塞后续步骤。"""
            # 重入保护：aboutToQuit 与 finally 都会调用本方法，
            # 且 window.close() 可能再次间接触发，必须最先返回。
            if self._shutting_down:
                return
            self._shutting_down = True
            app_log.info("[APP] Shutdown requested")

            # 1) 停止常驻循环
            try:
                self.control.stop.set()
                live_log = get_tagged("flow", "shutdown")
                live_log.info("[APP] Resident loop stopping")
                thread = self.engine_thread
                if thread is not None and thread.is_alive():
                    thread.join(timeout=JOIN_TIMEOUT)
                    if thread.is_alive():
                        app_log.warning("[WARN] Resident engine did not stop within %.1fs",
                                        JOIN_TIMEOUT)
                app_log.info("[LCU] Monitor stopped")
                app_log.info("[LIVE] Monitor stopped")
                app_log.info("[OCR] Worker stopped")
                app_log.info("[OPGG] Background refresh stopped")
            except Exception:  # noqa: BLE001
                logger.exception("停止后台任务失败")

            # 2) 停止定时器
            try:
                HUB.unsubscribe(self._hub_listener)
                self._hub_listener = None
            except Exception:  # noqa: BLE001
                logger.exception("取消状态订阅失败")
            if self._preview_timer is not None:
                try:
                    self._preview_timer.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("停止预览定时器失败")
                self._preview_timer = None
            for timer in list(self._timers):
                try:
                    timer.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("停止定时器失败")
            self._timers.clear()

            # 2.5) 停止更新任务（后台线程是 daemon，这里取消下载并关弹窗）
            try:
                if self.update_controller is not None:
                    self.update_controller.shutdown()
                    self.update_controller = None
                app_log.info("[UPDATE] Stopped")
            except Exception:  # noqa: BLE001
                logger.exception("停止更新任务失败")

            # 3) 关闭 Overlay
            try:
                if self.overlay_mode is OverlayMode.PREVIEW:
                    overlay_log.info("Preview finished: reason=APP_SHUTDOWN")
                self.overlay_mode = OverlayMode.HIDDEN
                if self.overlay_window is not None:
                    self.overlay_window.set_offers([])
                    self.overlay_window.hide()
                    self.overlay_window.close()
                    self.overlay_window.deleteLater()
                    self.overlay_window = None
                app_log.info("[OVERLAY] Closed")
            except Exception:  # noqa: BLE001
                logger.exception("关闭 Overlay 失败")

            # 4) 关闭主窗口与托盘
            try:
                if self.window is not None:
                    self.window.set_force_quit()
                    self.window.close()
                if self.tray is not None:
                    self.tray.hide()
                    self.tray = None
                app_log.info("[TRAY] Closed")
            except Exception:  # noqa: BLE001
                logger.exception("关闭托盘失败")

            # 5) 释放单实例互斥体（否则重启会提示"已经在运行"）
            try:
                self.instance.release()
            except Exception:  # noqa: BLE001
                logger.exception("释放单实例锁失败")

            # 6) 退出 Qt
            try:
                app_log.info("[APP] Qt application quitting")
                if self.app is not None:
                    self.app.quit()
            except Exception:  # noqa: BLE001
                logger.exception("Qt quit 失败")

            self._shutdown_done = True
            app_log.info("[APP] Shutdown complete")

        # -- Overlay 同步（主线程） -----------------------------------------
        def show_preview(self, seconds: float = 5.0) -> bool:
            """显示预览悬浮窗；重复点击会重新计时。"""
            try:
                window = self.ensure_overlay()
            except RuntimeError as exc:
                overlay_log.warning("悬浮窗显示失败: %s", exc)
                return False

            window.show_overlay()
            window.set_status(True, "Preview")
            window.set_offers(PREVIEW_OFFERS)
            self._last_overlay_payload = list(PREVIEW_OFFERS)
            self.overlay_mode = OverlayMode.PREVIEW

            # 重复点击：取消旧定时器，重新计时（不创建多个）
            if self._preview_timer is not None:
                self._preview_timer.stop()
            timer = QtCore.QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.finish_preview("TIMEOUT"))
            timer.start(int(seconds * 1000))
            self._preview_timer = timer
            if timer not in self._timers:
                self._timers.append(timer)
            overlay_log.info("Preview started (%.1fs)", seconds)
            return True

        def finish_preview(self, reason: str = "TIMEOUT") -> None:
            """结束预览；非预览状态下调用是安全的空操作。"""
            if self._preview_timer is not None:
                self._preview_timer.stop()
                if self._preview_timer in self._timers:
                    self._timers.remove(self._preview_timer)
                self._preview_timer = None
            if self.overlay_mode is not OverlayMode.PREVIEW:
                return
            self.overlay_mode = OverlayMode.HIDDEN
            self._last_overlay_payload = []
            if self.overlay_window is not None:
                try:
                    self.overlay_window.set_offers([])
                except Exception:  # noqa: BLE001
                    overlay_log.warning("结束预览失败", exc_info=True)
            overlay_log.info("Preview finished: reason=%s", reason)

        def prepare_reconnect(self) -> None:
            """点「重新连接 LOL」前先结束预览，避免测试数据被当成真实数据。"""
            if self.overlay_mode is OverlayMode.PREVIEW:
                self.finish_preview("RECONNECT")

        def sync_overlay(self) -> None:
            if self._shutting_down:
                return
            if self.overlay_window is None or not self._overlay_user_visible:
                return
            snap = HUB.snapshot()
            payload = list(snap.offers or [])

            # 真实海克斯出现 -> 打断预览并切到 LIVE
            if payload and self.overlay_mode is OverlayMode.PREVIEW:
                overlay_log.info("Preview interrupted: reason=REAL_AUGMENT_SELECT")
                self.finish_preview("REAL_AUGMENT_SELECT")

            # 预览期间：真实游戏的清理逻辑一律不生效
            if self.overlay_mode is OverlayMode.PREVIEW:
                overlay_log.debug("Preview cleanup skipped: mode=PREVIEW")
                return

            self.overlay_mode = OverlayMode.LIVE if payload else OverlayMode.HIDDEN
            trace = snap.perf_trace
            if (trace is not None and snap.offers_updated_at
                    and trace.trace_id != self._last_applied_trace_id):
                visible_ms = HUB.perf.mark_overlay_visible(trace.trace_id)
                if visible_ms > 0:
                    self._last_applied_trace_id = trace.trace_id
                    perf_log.info("%s overlay_visible_ms=%.1f",
                                  trace.describe(), visible_ms)
            if payload == self._last_overlay_payload:
                return
            self._last_overlay_payload = payload
            try:
                self.overlay_window.set_offers(payload)
            except Exception:  # noqa: BLE001
                overlay_log.warning("同步 Overlay 失败", exc_info=True)

        def ensure_overlay(self):
            if self.overlay_window is None and not self._shutting_down:
                from src.overlay.manager import launch_overlay

                s = self.settings.overlay
                self.overlay_window = launch_overlay(
                    screen_index=s.screen_index, enabled=True,
                    opacity=s.opacity, offset_x=s.offset_x, offset_y=s.offset_y)
            return self.overlay_window

else:  # pragma: no cover

    class AppController:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("PySide6 is not installed.")


def run_gui(settings: Settings, args) -> int:
    if not PYSIDE6_AVAILABLE:
        print("PySide6 is not installed. Falling back to headless resident mode.")
        from src import live

        return live.run(settings, args)

    # QObject（包括 HUB -> Qt 的信号桥）必须在 QApplication 之后创建。
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    controller = AppController(settings, args)
    instance = controller.instance
    if not instance.acquire():
        QtWidgets.QMessageBox.information(None, "LOL 海克斯助手", ALREADY_RUNNING_TEXT)
        return 0

    paths.ensure_user_subdirs()
    moved = paths.migrate_legacy_data()
    if moved:
        app_log.info("已从开发目录迁移数据: %s", moved)

    from src.ui.debug_window import DebugWindow
    from src.ui.main_window import MainWindow
    from src.ui.settings_dialog import SettingsDialog
    from src.ui.tray import TrayIcon

    controller.app = app
    app.setQuitOnLastWindowClosed(False)

    windows: dict = {}

    def show_window() -> None:
        window = controller.window
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()

    def feedback(text: str, error: bool = False) -> None:
        if controller.window is not None:
            controller.window.set_feedback(text, error=error)
        HUB.update(status_message=text)

    def open_settings() -> None:
        def applied(new_settings: Settings) -> None:
            controller.settings = new_settings
            if controller.overlay_window is not None:
                controller.overlay_window.set_opacity(new_settings.overlay.opacity)
                controller.overlay_window.set_offset(new_settings.overlay.offset_x,
                                                     new_settings.overlay.offset_y)
            HUB.update(overlay_enabled=new_settings.overlay.enabled,
                       data_region=new_settings.data.region,
                       data_tier=new_settings.data.tier)
            feedback("设置已保存")

        SettingsDialog(controller.settings, parent=controller.window,
                       on_applied=applied).exec()

    def preview_overlay() -> None:
        if controller.show_preview(5.0):
            feedback("悬浮窗已显示（约 5 秒）")
        else:
            feedback("悬浮窗显示失败", error=True)

    def toggle_overlay() -> None:
        controller._overlay_user_visible = not controller._overlay_user_visible
        if controller._overlay_user_visible:
            window = controller.ensure_overlay()
            window.show_overlay()
            feedback("悬浮窗已显示")
        elif controller.overlay_window is not None:
            controller.overlay_window.hide()
            feedback("悬浮窗已隐藏")
        overlay_log.info("Overlay visible=%s (来自托盘)", controller._overlay_user_visible)

    def reconnect() -> None:
        from src import live as live_mod

        # 预览期间点重新连接：先结束预览
        controller.prepare_reconnect()
        live_mod.request_reconnect(controller.control)
        feedback("正在重新连接 LOL...")

        def report() -> None:
            connected = HUB.snapshot().client_connected
            feedback("已重新连接 LOL" if connected else "未检测到 LOL 客户端",
                     error=not connected)

        QtCore.QTimer.singleShot(4000, report)

    def open_debug() -> None:
        if controller.debug_window is None:
            controller.debug_window = DebugWindow(parent=controller.window)
        controller.debug_window.show()
        controller.debug_window.raise_()

    from src.ui.update_controller import UpdateController
    import src as src_pkg

    def update_feedback(text: str, error: bool = False) -> None:
        feedback(text, error=error)
        if controller.window is not None:
            controller.window.set_update_status(text, error=error)

    updates = UpdateController(settings, current_version=src_pkg.__version__,
                               feedback=update_feedback,
                               on_quit=controller.request_shutdown)
    controller.update_controller = updates

    def check_updates() -> None:
        updates.check(manual=True)

    controller.window = MainWindow(on_settings=open_settings,
                                   on_preview_overlay=preview_overlay,
                                   on_reconnect=reconnect,
                                   on_quit=controller.request_shutdown,
                                   on_debug=open_debug,
                                   on_check_update=check_updates,
                                   current_version=src_pkg.__version__)
    windows["main"] = controller.window
    controller.window.show()

    controller.tray = TrayIcon(controller.window, on_show_window=show_window,
                               on_toggle_overlay=toggle_overlay,
                               on_settings=open_settings, on_reconnect=reconnect,
                               on_open_logs=_open_logs,
                               on_quit=controller.request_shutdown,
                               on_check_update=check_updates)
    controller.tray.show()

    # 主线程定时器：把 HUB 状态同步到 Overlay
    sync_timer = QtCore.QTimer()
    sync_timer.timeout.connect(controller.sync_overlay)
    # 正常路径由 HUB 订阅 + Qt 队列信号即时唤醒；此定时器仅作异常兜底。
    sync_timer.start(1000)
    controller._timers.append(sync_timer)

    # 如果设置里启用了 Overlay，启动时就创建（海克斯出现前保持空白）
    if settings.overlay.enabled and not args.no_overlay:
        try:
            controller.ensure_overlay()
        except RuntimeError as exc:
            overlay_log.warning("Overlay 启动失败: %s", exc)

    HUB.update(hero_scope=settings.hero_scope_label,
               augment_scope=settings.augment_scope_label,
               data_region=settings.data.region,
               data_tier=settings.data.tier,
               overlay_enabled=settings.overlay.enabled)
    controller.start_engine()
    client_log.info("GUI 启动（托盘常驻），日志目录: %s", paths.user_data_dir())
    app_log.info("[APP] GUI ready")
    # 启动后异步检查更新（不阻塞 LCU/OCR/浮层；UI 已就绪）
    updates.schedule_startup_check()

    # 打包后自动验收：LOL_HEX_HELPER_AUTOQUIT=<秒> 时到点走与托盘完全相同的退出路径
    auto_quit = os.environ.get("LOL_HEX_HELPER_AUTOQUIT")
    if auto_quit:
        try:
            seconds = float(auto_quit)
        except ValueError:
            seconds = 8.0
        app_log.info("[APP] AUTOQUIT 已启用：%.1fs 后走托盘退出路径", seconds)
        QtCore.QTimer.singleShot(int(seconds * 1000), controller.request_shutdown)

    # 打包后验收：LOL_HEX_HELPER_PREVIEW=<秒> 时自动点一次预览
    auto_preview = os.environ.get("LOL_HEX_HELPER_PREVIEW")
    if auto_preview:
        try:
            seconds = float(auto_preview)
        except ValueError:
            seconds = 5.0
        overlay_log.info("AUTOPREVIEW 已启用：启动后自动预览 %.1fs", seconds)
        QtCore.QTimer.singleShot(2500, preview_overlay)

    app.aboutToQuit.connect(controller.shutdown)
    try:
        return app.exec()
    finally:
        controller.shutdown()


__all__ = ["run_gui", "ALREADY_RUNNING_TEXT", "PYSIDE6_AVAILABLE",
           "PYSIDE6_IMPORT_ERROR", "PREVIEW_OFFERS", "AppController"]
