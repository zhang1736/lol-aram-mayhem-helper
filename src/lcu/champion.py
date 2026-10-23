"""选人阶段英雄识别（Day 2 只做 API 层，不含 OCR）。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from src.core.models import ChampionInfo
from src.lcu.client import LcuClient
from src.lcu.endpoints import (
    CHAMPION_SUMMARY,
    CHAMP_SELECT_CURRENT_CHAMPION,
    CHAMP_SELECT_SESSION,
)
from src.lcu.gameflow import GamePhase, GameflowMonitor
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ChampSelectMember:
    cell_id: int | None
    champion_id: int | None
    champion_pick_intent: int | None
    assigned_position: str
    summoner_id: int | None
    is_local_player: bool = False


@dataclass(frozen=True)
class ChampSelectInfo:
    local_player_cell_id: int | None
    champion_id: int | None
    champion_pick_intent: int | None
    my_team: list[ChampSelectMember]
    timer_phase: str | None = None

    @property
    def resolved_champion_id(self) -> int | None:
        """已锁定则用 championId，否则退回预选 pickIntent。"""
        return self.champion_id or self.champion_pick_intent


class ChampionDetector:
    """只在 ChampSelect 阶段访问选人接口，避免无意义轮询。"""

    def __init__(self, client: LcuClient, gameflow: GameflowMonitor | None = None) -> None:
        self.client = client
        self.gameflow = gameflow or GameflowMonitor(client)
        self._champion_index: dict[int, dict] | None = None
        self._champion_info_cache: list[ChampionInfo] | None = None

    # -- 选人会话 -----------------------------------------------------------
    async def get_champ_select_session(self) -> dict | None:
        """返回选人会话；404（不在选人）属正常，返回 None。"""
        status, data = await self.client.get_json(CHAMP_SELECT_SESSION)
        if status == 404:
            logger.debug("不在选人阶段（champ-select session 404，属正常）")
            return None
        if status != 200 or not isinstance(data, dict):
            logger.debug("champ-select session 返回 %s", status)
            return None
        return data

    async def get_champ_select_info(self) -> ChampSelectInfo | None:
        """定位 localPlayerCellId -> myTeam -> championId（选人会话明细）。"""
        session = await self.get_champ_select_session()
        if session is None:
            return None

        local_cell = session.get("localPlayerCellId")
        logger.info("[CHAMP_SELECT] session detected")
        logger.info("[CHAMPION] localPlayerCellId = %s", local_cell)
        members: list[ChampSelectMember] = []
        local_champion: int | None = None
        local_intent: int | None = None

        for raw in session.get("myTeam") or []:
            is_local = raw.get("cellId") == local_cell
            member = ChampSelectMember(
                cell_id=raw.get("cellId"),
                champion_id=raw.get("championId"),
                champion_pick_intent=raw.get("championPickIntent"),
                assigned_position=raw.get("assignedPosition") or "",
                summoner_id=raw.get("summonerId"),
                is_local_player=is_local,
            )
            members.append(member)
            if is_local:
                local_champion = member.champion_id or None
                local_intent = member.champion_pick_intent or None

        info = ChampSelectInfo(
            local_player_cell_id=local_cell,
            champion_id=local_champion,
            champion_pick_intent=local_intent,
            my_team=members,
            timer_phase=(session.get("timer") or {}).get("phase"),
        )
        logger.info("[CHAMPION] championId = %s (pickIntent=%s)",
                    info.champion_id, info.champion_pick_intent)
        return info

    async def get_current_champion_id(self) -> int | None:
        """便捷接口：优先 current-champion，回退选人会话。"""
        status, data = await self.client.get_json(CHAMP_SELECT_CURRENT_CHAMPION)
        if status == 200 and isinstance(data, int) and data > 0:
            return data
        info = await self.get_champ_select_info()
        return info.resolved_champion_id if info else None

    async def should_query(self) -> bool:
        """只有处于 ChampSelect 才应该调用选人接口。"""
        return await self.gameflow.get_phase() is GamePhase.CHAMP_SELECT

    async def wait_for_champion_id(
        self,
        timeout: float = 180.0,
        interval: float = 2.0,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Optional[int]:
        """持续轮询选人会话，直到拿到非 0 的 championId。

        实测：ARAM 选人在 BAN_PICK 阶段 championId 为 0 属正常，
        必须继续等待，不能写入 GameSession。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if should_continue is not None and not should_continue():
                logger.debug("wait_for_champion_id 提前退出")
                return None
            info = await self.get_champ_select_info()
            if info is not None:
                champion_id = info.resolved_champion_id
                if champion_id:
                    logger.info("[CHAMPION] 已确定 championId = %s", champion_id)
                    return champion_id
                logger.debug("championId 仍为 0（cellId=%s, timer=%s），继续等待",
                             info.local_player_cell_id, info.timer_phase)
            await asyncio.sleep(interval)
        logger.info("wait_for_champion_id 超时（%.0fs）", timeout)
        return None

    # -- 英雄字典 -----------------------------------------------------------
    async def get_champion_summary(self) -> list[ChampionInfo]:
        """championId -> ChampionInfo（来自 LCU，无需网络）。

        一次运行只请求一次 champion-summary.json（内存缓存）。
        """
        if self._champion_info_cache is not None:
            return self._champion_info_cache
        if self._champion_index is not None:
            return self._to_info_list(self._champion_index)

        status, data = await self.client.get_json(CHAMPION_SUMMARY)
        index: dict[int, dict] = {}
        if status == 200 and isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                    index[entry["id"]] = entry
        else:
            logger.warning("champion-summary 获取失败: HTTP %s", status)
        self._champion_index = index
        self._champion_info_cache = self._to_info_list(index)
        logger.info("[CHAMPION] champion-summary 载入 %d 条（仅请求一次）", len(index))
        return self._champion_info_cache

    @staticmethod
    def _to_info_list(index: dict[int, dict]) -> list[ChampionInfo]:
        return [
            ChampionInfo(
                champion_id=cid,
                alias=entry.get("alias"),
                name=entry.get("name"),
                name_cn=entry.get("name"),
                name_en=entry.get("alias"),
            )
            for cid, entry in sorted(index.items())
        ]

    async def resolve_champion(self, champion_id: int | None) -> ChampionInfo | None:
        """把 championId 解析成 ChampionInfo(id / alias / name)。"""
        if not champion_id:
            return None
        summary = await self.get_champion_summary()
        for info in summary:
            if info.champion_id == champion_id:
                return info
        return None

    async def get_current_champion(self) -> ChampionInfo | None:
        """当前玩家的英雄信息：championId -> alias -> name。"""
        champ_id = await self.get_current_champion_id()
        return await self.resolve_champion(champ_id)


__all__ = ["ChampionDetector", "ChampSelectInfo", "ChampSelectMember"]
