"""Live Client Data API（游戏内，端口 2999）。

实测（Day 3）：
    对局中 https://127.0.0.1:2999/liveclientdata/activeplayer 可用；
    非对局时不可访问（连接被拒），属正常。

该 API 提供 gameflow / champ-select 都拿不到的信息：
    activePlayer.level      -> 海克斯阶段判定
    activePlayer.championName / rawChampionName -> 当前实际英雄
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)

LIVE_BASE = "https://127.0.0.1:2999/liveclientdata"
TIMEOUT = 4.0

_RAW_NAME_RE = re.compile(r"displayname_([A-Za-z0-9]+)$")


@dataclass
class LiveGameSnapshot:
    champion_name: Optional[str] = None      # 本地化名（国服为中文）
    champion_alias: Optional[str] = None     # 从 rawChampionName 解析，如 Varus
    level: Optional[int] = None
    game_time: Optional[float] = None
    player_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def alias_from_raw_champion_name(raw: str | None) -> Optional[str]:
    """'game_character_displayname_Varus' -> 'Varus'。"""
    if not raw:
        return None
    match = _RAW_NAME_RE.search(raw)
    if match:
        return match.group(1)
    if "_" in raw:
        tail = raw.rsplit("_", 1)[-1]
        return tail or None
    return raw or None


class LiveClientDataSource:
    """对局内数据源。不可用时所有方法返回 None，不抛异常。

    实测（2026-09-22）：每轮都 `async with httpx.AsyncClient(...)` 相当于
    每次请求都重新建连接 + TLS 握手，12~16ms/次（首次 694ms）。
    现在改为长期复用同一个 client，由调用方在结束时 `aclose()`。
    """

    def __init__(self, base_url: str = LIVE_BASE, timeout: float = TIMEOUT) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(verify=False, timeout=self.timeout)
        return self._client

    async def _get(self, path: str) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            response = await self._ensure_client().get(url)
        except Exception as exc:  # noqa: BLE001 - 未在对局中时属正常
            logger.debug("Live Client API 不可用 (%s): %s", path, type(exc).__name__)
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def get_all_game_data(self) -> Optional[dict]:
        return await self._get("/allgamedata")

    async def get_active_player(self) -> Optional[dict]:
        return await self._get("/activeplayer")

    async def get_snapshot(self) -> Optional[LiveGameSnapshot]:
        """当前对局快照；不在对局中返回 None。"""
        data = await self.get_all_game_data()
        if not isinstance(data, dict):
            return None
        active = data.get("activePlayer") or {}
        players = data.get("allPlayers") or []
        game = data.get("gameData") or {}

        champion_name = active.get("championName") or None
        alias = None
        for player in players:
            if not isinstance(player, dict):
                continue
            if player.get("summonerName") == active.get("summonerName") or \
                    player.get("riotIdGameName") == active.get("riotIdGameName"):
                champion_name = player.get("championName") or champion_name
                alias = alias_from_raw_champion_name(player.get("rawChampionName"))
                break
        if alias is None:
            alias = alias_from_raw_champion_name(active.get("rawChampionName"))

        level = active.get("level")
        return LiveGameSnapshot(
            champion_name=champion_name,
            champion_alias=alias,
            level=int(level) if isinstance(level, (int, float)) else None,
            game_time=game.get("gameTime"),
            player_count=len(players),
            raw=data,
        )

    async def aclose(self) -> None:
        """释放复用的 HTTP 连接池。"""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 Live Client 连接失败: %s", exc)
        self._client = None


__all__ = ["LiveClientDataSource", "LiveGameSnapshot", "alias_from_raw_champion_name", "LIVE_BASE"]
