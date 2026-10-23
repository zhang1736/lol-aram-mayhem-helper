"""独立诊断命令：python -m src.lcu.diagnostic

输出 LeagueClientUx 发现状态、端口、region/locale 与 gameflow 阶段。
**不输出 token**（只输出长度）。

    python -m src.lcu.diagnostic            一次性诊断
    python -m src.lcu.diagnostic --watch N  持续轮询 N 秒；进入 ChampSelect 时
                                            抓取 localPlayerCellId / myTeam / championId
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from src.config import load_settings
from src.lcu.champion import ChampionDetector
from src.lcu.client import LcuClient
from src.lcu.credential import LcuCredentialProvider, LcuCredentials
from src.lcu.gameflow import GamePhase, GameflowMonitor
from src.utils.logger import force_utf8_stdio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAPTURE_PATH = os.path.join(PROJECT_ROOT, "research", "lcu_champselect_capture.json")


def _print_block(title: str, value: object) -> None:
    print(f"{title}:")
    print(f"{value}")
    print()


def run() -> int:
    force_utf8_stdio()
    settings = load_settings()
    provider = LcuCredentialProvider(settings.lcu.process_name)

    credentials: LcuCredentials | None = provider.find_credentials()
    if credentials is None:
        _print_block("LeagueClientUx", "NOT FOUND")
        print("提示：请确认英雄联盟客户端已启动并登录。")
        return 1

    _print_block("LeagueClientUx", "FOUND")
    _print_block("PID", credentials.pid if credentials.pid is not None else "-")
    _print_block("Port", credentials.port)
    _print_block("Region", credentials.region or "-")
    _print_block("Locale", credentials.locale or "-")
    _print_block("Token", f"已获取（长度 {credentials.token_length}，不显示内容）")

    client = LcuClient(credentials, timeout=settings.lcu.request_timeout)
    connected = client.connect()
    _print_block("LCU", "CONNECTED" if connected else "FAILED")
    if not connected:
        print(f"错误: {client.last_error}")
        return 1

    async def _phase() -> str:
        monitor = GameflowMonitor(client)
        try:
            return (await monitor.get_phase()).value
        finally:
            await client.aclose()

    _print_block("Gameflow", asyncio.run(_phase()))
    return 0


async def _capture_champ_select(detector: ChampionDetector) -> None:
    """进入 ChampSelect 时抓取并落盘。"""
    info = await detector.get_champ_select_info()
    if info is None:
        return
    champion = await detector.resolve_champion(info.resolved_champion_id)
    payload = {
        "localPlayerCellId": info.local_player_cell_id,
        "myTeam": [
            {
                "cellId": m.cell_id,
                "championId": m.champion_id,
                "championPickIntent": m.champion_pick_intent,
                "assignedPosition": m.assigned_position,
                "summonerId": m.summoner_id,
                "isLocalPlayer": m.is_local_player,
            }
            for m in info.my_team
        ],
        "myChampionId": info.resolved_champion_id,
        "champion": champion.model_dump() if champion else None,
        "timerPhase": info.timer_phase,
    }
    with open(CAPTURE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print("")
    print("=== CHAMP SELECT CAPTURED ===")
    print("localPlayerCellId: %s" % info.local_player_cell_id)
    for member in payload["myTeam"]:
        print("  cellId=%-3s championId=%-5s pickIntent=%-5s pos=%-7s %s"
              % (member["cellId"], member["championId"], member["championPickIntent"],
                 member["assignedPosition"] or "-",
                 "<== 本地玩家" if member["isLocalPlayer"] else ""))
    if champion:
        print("MY CHAMPION: id=%s alias=%s name=%s"
              % (champion.champion_id, champion.alias, champion.name))
    print("saved -> %s" % CAPTURE_PATH)


async def _watch(minutes: float) -> int:
    settings = load_settings()
    provider = LcuCredentialProvider(settings.lcu.process_name)
    credentials = provider.find_credentials()
    if credentials is None:
        print("LeagueClientUx: NOT FOUND")
        return 1
    client = LcuClient(credentials, timeout=settings.lcu.request_timeout)
    if not client.connect():
        print("LCU: FAILED (%s)" % client.last_error)
        return 1

    monitor = GameflowMonitor(client)
    detector = ChampionDetector(client, monitor)
    deadline = asyncio.get_event_loop().time() + minutes * 60
    captured = False
    last: GamePhase | None = None
    try:
        while asyncio.get_event_loop().time() < deadline:
            phase = await monitor.get_phase()
            if phase is not last:
                print("[%s] phase = %s" % (credentials.port, phase.value))
                last = phase
            if phase is GamePhase.CHAMP_SELECT and not captured:
                await _capture_champ_select(detector)
                captured = True
            await asyncio.sleep(2)
    finally:
        await client.aclose()
    print("watch 结束 (captured=%s)" % captured)
    return 0


def _parse_watch(argv: list[str]) -> float:
    if "--watch" not in argv:
        return 0.0
    try:
        return float(argv[argv.index("--watch") + 1])
    except (IndexError, ValueError):
        return 0.0


if __name__ == "__main__":
    watch_minutes = _parse_watch(sys.argv[1:])
    if watch_minutes > 0:
        sys.exit(asyncio.run(_watch(watch_minutes)))
    sys.exit(run())
