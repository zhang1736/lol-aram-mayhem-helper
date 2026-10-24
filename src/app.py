"""应用入口。

    python -m src.app                    状态检查（连一次 LCU，打印口径）
    python -m src.app --live             跑一局实机闭环
    python -m src.app --resident         常驻：一局接一局，掉线自动重连
    python -m src.app --overlay-smoke 3  Overlay 自检

PySide6 未安装时不会崩溃：只打印提示，核心服务照常运行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import src as src_pkg
from src.config import Settings, load_settings
from src.core.game_session import GameSession
from src.core.state_machine import AppState, StateMachine
from src.data.database import Database
from src.data.repository import AugmentStatsRepository
from src.lcu.client import LcuClient
from src.lcu.credential import LcuCredentialProvider
from src.lcu.gameflow import GameflowMonitor, GamePhase
from src.overlay.manager import OverlayManager, OverlayUnavailableError, launch_overlay
from src.overlay.window import MISSING_PYSIDE_MESSAGE
from src.update.bootstrap import ensure_embedded_updater
from src.update.startup import write_startup_marker
from src.utils.logger import force_utf8_stdio, get_logger, get_tagged, setup_logging

logger = get_logger(__name__)
client_log = get_tagged("client")
lcu_log = get_tagged("lcu")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="src.app",
                                     description="LOL ARAM: Mayhem 海克斯数据助手")
    parser.add_argument("--live", action="store_true", help="跑一局实机闭环")
    parser.add_argument("--resident", action="store_true",
                        help="常驻模式：等待客户端 -> 一局接一局，不因对局结束退出")
    parser.add_argument("--once", action="store_true", help="常驻模式下只跑一局就退出")
    parser.add_argument("--no-gui", action="store_true",
                        help="常驻模式不启动托盘 GUI（纯后台）")
    parser.add_argument("--gui", action="store_true", help="强制启用托盘 GUI")
    parser.add_argument("--no-overlay", action="store_true", help="不启动 Overlay")
    parser.add_argument("--overlay-smoke", type=float, default=0.0, metavar="SECONDS",
                        help="启动 Overlay 并在 N 秒后自动退出（自检用）")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="状态检查模式下持续轮询 gameflow 阶段 N 秒")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    parser.add_argument("--champ-timeout", type=float, default=180.0,
                        help="等待选人 championId 的超时秒数")
    parser.add_argument("--champ-poll-interval", type=float, default=1.0,
                        help="选人阶段轮询候选英雄的间隔秒数（换英雄检测）")
    parser.add_argument("--scan-interval", type=float, default=0.25,
                        help="三卡 OCR 扫描间隔")
    parser.add_argument("--level-poll-interval", type=float, default=0.5,
                        help="对局中 Live Client 等级轮询间隔（秒）")
    parser.add_argument("--watchdog-interval", type=float, default=0.35,
                        help="海克斯扫描窗口外的低频检测间隔（秒）")
    parser.add_argument("--wait-game", type=float, default=180.0, metavar="MINUTES",
                        help="等待对局出现的分钟数")
    parser.add_argument("--wait-client", type=float, default=30.0, metavar="MINUTES",
                        help="等待 LeagueClientUx 的分钟数")
    return parser.parse_args(argv)


def _print_status(connected: bool, phase: GamePhase | None,
                  port: int | None, settings: Settings) -> None:
    print(f"LCU: {'Connected' if connected else 'Disconnected'}")
    if port:
        print(f"Port: {port}")
    if phase is not None:
        print(f"Gameflow: {phase.value}")
    # 两个 scope 必须分开显示，绝不混用
    print(f"英雄统计: {settings.hero_scope_label}")
    print(f"海克斯统计: {settings.augment_scope_label}")
    print(f"OP.GG Cache TTL: {settings.cache.opgg_ttl_hours:g} hours")


def _discover_and_connect(settings: Settings):
    credentials = LcuCredentialProvider(settings.lcu.process_name).find_credentials()
    if credentials is None:
        client_log.info("未发现 LeagueClientUx.exe")
        return None, False
    client = LcuClient(credentials, timeout=settings.lcu.request_timeout)
    return client, client.connect()


async def _read_phase(client: LcuClient, seconds: float, interval: float) -> GamePhase:
    monitor = GameflowMonitor(client)
    if seconds <= 0:
        return await monitor.get_phase()
    deadline = asyncio.get_event_loop().time() + seconds
    phase = GamePhase.UNKNOWN
    while asyncio.get_event_loop().time() < deadline:
        phase = await monitor.get_phase()
        await asyncio.sleep(interval)
    return phase


def _run_async(coro):
    return asyncio.run(coro)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    force_utf8_stdio()
    settings = load_settings()
    setup_logging(settings.log_file,
                  "DEBUG" if settings.debug.enabled else settings.logging.level,
                  root=settings.config_dir)

    logger.info("=== LOL ARAM: Mayhem Helper 启动 ===")
    logger.info("版本: %s", src_pkg.__version__)
    ensure_embedded_updater()
    logger.info("英雄统计口径: %s", settings.hero_scope_label)
    logger.info("海克斯统计口径: %s（该接口无法按 region/tier 过滤）",
                settings.augment_scope_label)
    logger.info("OP.GG 缓存 TTL: %g 小时", settings.cache.opgg_ttl_hours)

    database = Database(settings.database_file)
    database.initialize()
    write_startup_marker(src_pkg.__version__)

    try:
        if args.live or args.resident:
            args.stats_repository = AugmentStatsRepository(database)
            args.settings = settings

            # 成品形态：--resident 默认带托盘 GUI（除非显式 --no-gui）
            wants_gui = args.gui or (args.resident and not args.no_gui)
            if wants_gui:
                from src.ui.app_ui import PYSIDE6_AVAILABLE, PYSIDE6_IMPORT_ERROR, run_gui

                if PYSIDE6_AVAILABLE:
                    return run_gui(settings, args)
                logger.warning("PySide6 不可用，退回无 GUI 常驻模式: %s",
                               PYSIDE6_IMPORT_ERROR or "unknown")

            from src import live

            return live.run(settings, args)

        client, connected = _discover_and_connect(settings)
        phase = GamePhase.UNKNOWN
        if connected and client is not None:
            async def _phase_and_close() -> GamePhase:
                try:
                    return await _read_phase(client, args.watch, args.interval)
                finally:
                    await client.aclose()

            phase = _run_async(_phase_and_close())

        _print_status(connected, phase if connected else None,
                      client.credentials.port if (connected and client) else None,
                      settings)

        session = GameSession()
        machine = StateMachine(AppState.CONNECTED if connected else AppState.DISCONNECTED)
        if connected:
            machine.update_from_phase(phase)
            logger.info("State: %s | Session empty: %s", machine.state.value, session.is_empty)

        if settings.overlay.enabled and not args.no_overlay:
            try:
                window = launch_overlay(
                    screen_index=settings.overlay.screen_index, enabled=True,
                    opacity=settings.overlay.opacity,
                    offset_x=settings.overlay.offset_x,
                    offset_y=settings.overlay.offset_y)
            except OverlayUnavailableError:
                print("")
                print(MISSING_PYSIDE_MESSAGE)
            except RuntimeError as exc:
                print("")
                print(str(exc))
            else:
                manager = OverlayManager(window)
                manager.update(connected, phase.value if connected else None)
                print("Overlay: Ready")
                if args.overlay_smoke > 0:
                    from PySide6 import QtCore, QtWidgets

                    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
                    QtCore.QTimer.singleShot(int(args.overlay_smoke * 1000), app.quit)
                    app.exec()
                else:
                    from PySide6 import QtWidgets

                    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
                    print("Overlay running. Ctrl+C to quit.")
                    app.exec()
        return 0 if connected else 1
    finally:
        database.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
