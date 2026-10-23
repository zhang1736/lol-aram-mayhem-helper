"""研究用：持续抓取 ChampSelect，直到拿到非 0 的 championId。

修正 Day 3 第一次抓取的问题：ARAM 选人在 BAN_PICK 阶段开始时
localPlayerCellId 有效但 championId 仍为 0，单次快照会漏掉英雄。

    python research/champselect_watch2.py [分钟数]
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.lcu.champion import ChampionDetector  # noqa: E402
from src.lcu.client import LcuClient  # noqa: E402
from src.lcu.credential import LcuCredentialProvider  # noqa: E402
from src.lcu.endpoints import CHAMP_SELECT_SESSION  # noqa: E402
from src.lcu.gameflow import GamePhase, GameflowMonitor  # noqa: E402

OUT_JSON = os.path.join(PROJECT_ROOT, "research", "lcu_champselect_capture_v2.json")
OUT_LOG = os.path.join(PROJECT_ROOT, "research", "lcu_champselect_watch2.log")


async def main(minutes: float) -> int:
    credentials = LcuCredentialProvider().find_credentials()
    if credentials is None:
        print("LeagueClientUx NOT FOUND")
        return 1
    client = LcuClient(credentials)
    if not client.connect():
        print("LCU FAILED: %s" % client.last_error)
        return 1

    monitor = GameflowMonitor(client)
    detector = ChampionDetector(client, monitor)
    deadline = asyncio.get_event_loop().time() + minutes * 60
    best = None
    last_phase = None
    try:
        while asyncio.get_event_loop().time() < deadline:
            phase = await monitor.get_phase()
            if phase is not last_phase:
                print("%s phase=%s" % (time.strftime("%H:%M:%S"), phase.value), flush=True)
                last_phase = phase
            if phase is GamePhase.CHAMP_SELECT:
                status, raw = await client.get_json(CHAMP_SELECT_SESSION)
                if status == 200 and isinstance(raw, dict):
                    cell = raw.get("localPlayerCellId")
                    champion_id = None
                    for member in raw.get("myTeam") or []:
                        if member.get("cellId") == cell:
                            champion_id = member.get("championId") or None
                    if champion_id:
                        info = await detector.resolve_champion(champion_id)
                        best = {
                            "localPlayerCellId": cell,
                            "myChampionId": champion_id,
                            "champion": info.model_dump() if info else None,
                            "myTeam": raw.get("myTeam"),
                            "timerPhase": (raw.get("timer") or {}).get("phase"),
                        }
                        with io.open(OUT_JSON, "w", encoding="utf-8") as fh:
                            json.dump(best, fh, ensure_ascii=False, indent=1)
                        print("CAPTURED championId=%s alias=%s name=%s"
                              % (champion_id, info.alias if info else "?",
                                 info.name if info else "?"), flush=True)
                        break
                    else:
                        print("  champ-select tick: cell=%s championId=0 (尚未分配)"
                              % cell, flush=True)
            await asyncio.sleep(2)
    finally:
        await client.aclose()
    return 0 if best else 2


if __name__ == "__main__":
    arg = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    sys.exit(asyncio.run(main(arg)))
