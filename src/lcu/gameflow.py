"""Gameflow 阶段监控。

只依赖 /lol-gameflow/v1/gameflow-phase：
实测表明 /lol-gameflow/v1/session 在多数非房间状态下本来就 404，
因此不能作为主要判据。
"""

from __future__ import annotations

from enum import Enum

from src.lcu.client import LcuClient
from src.lcu.endpoints import GAMEFLOW_PHASE, GAMEFLOW_SESSION
from src.utils.logger import get_logger

logger = get_logger(__name__)


class GamePhase(str, Enum):
    NONE = "None"
    LOBBY = "Lobby"
    MATCHMAKING = "Matchmaking"
    READY_CHECK = "ReadyCheck"
    CHAMP_SELECT = "ChampSelect"
    GAME_START = "GameStart"
    IN_PROGRESS = "InProgress"
    RECONNECT = "Reconnect"
    # 实测（2026-09-21 正式版日志）：一局打完后客户端依次给出
    #   InProgress -> Reconnect -> Unknown -> Lobby -> ChampSelect
    # 也就是说 **不会** 出现字面值 "None"，旧的"只在 None 才判对局结束"
    # 会导致下一局继续沿用上一局的 Session。
    PRE_END_OF_GAME = "PreEndOfGame"
    END_OF_GAME = "EndOfGame"
    WAITING_FOR_STATS = "WaitingForStats"
    TERMINATED_IN_ERROR = "TerminatedInError"
    UNKNOWN = "Unknown"

    @property
    def in_game(self) -> bool:
        """是否处于"对局进行中"（含加载与掉线重连）。"""
        return self in IN_GAME_PHASES

    @property
    def is_game_end(self) -> bool:
        """是否是"对局已结束"的客户端阶段。"""
        return self in GAME_END_PHASES

    @classmethod
    def from_raw(cls, raw: str | None) -> "GamePhase":
        if raw is None:
            return cls.UNKNOWN
        text = str(raw).strip().strip('"')
        if text in ("", "None"):
            return cls.NONE
        try:
            return cls(text)
        except ValueError:
            return cls.UNKNOWN


#: 对局进行中的阶段（英雄/等级以 Live Client API 为权威）
IN_GAME_PHASES: frozenset["GamePhase"] = frozenset({
    GamePhase.GAME_START,
    GamePhase.IN_PROGRESS,
    GamePhase.RECONNECT,
})

#: 对局已经结束的阶段：循环必须退出并清理 Session
GAME_END_PHASES: frozenset["GamePhase"] = frozenset({
    GamePhase.NONE,
    GamePhase.LOBBY,
    GamePhase.MATCHMAKING,
    GamePhase.READY_CHECK,
    GamePhase.PRE_END_OF_GAME,
    GamePhase.END_OF_GAME,
    GamePhase.WAITING_FOR_STATS,
    GamePhase.TERMINATED_IN_ERROR,
})


class GameflowMonitor:
    """读取 gameflow 阶段，并记录连接健康度。

    重要（多局失效根因）：`get_phase()` 失败时返回 UNKNOWN 是为了让状态机不崩，
    但调用方必须能区分"客户端真的处于 Unknown"和"LCU 连不上"，
    否则客户端重启（端口/token 变化）后会一直静默空等。
    """

    def __init__(self, client: LcuClient) -> None:
        self.client = client
        self.last_phase: GamePhase | None = None
        #: 连续读取失败次数（成功一次清零）
        self.consecutive_failures = 0
        self.last_error: str | None = None

    def _fail(self, message: str) -> GamePhase:
        self.consecutive_failures += 1
        self.last_error = message
        if self.consecutive_failures == 1:
            logger.warning("读取 gameflow-phase 失败: %s", message)
        elif self.consecutive_failures % 10 == 0:
            logger.warning("读取 gameflow-phase 已连续失败 %d 次: %s",
                           self.consecutive_failures, message)
        return GamePhase.UNKNOWN

    async def get_phase(self) -> GamePhase:
        """读取当前阶段。连接失败时返回 UNKNOWN。"""
        try:
            response = await self.client.get(GAMEFLOW_PHASE)
        except Exception as exc:  # noqa: BLE001 - 网络层异常不应打断状态机
            return self._fail("%s: %s" % (type(exc).__name__, exc))

        if response.status_code != 200:
            return self._fail("HTTP %s" % response.status_code)

        phase = GamePhase.from_raw(response.text)
        self.consecutive_failures = 0
        self.last_error = None
        if phase != self.last_phase:
            logger.info("Gameflow phase: %s", phase.value)
            self.last_phase = phase
        return phase

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0

    async def get_session(self) -> dict | None:
        """完整对局会话；不在房间/对局时返回 None（404 属正常）。"""
        status, data = await self.client.get_json(GAMEFLOW_SESSION)
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        return data


__all__ = ["GamePhase", "GameflowMonitor", "IN_GAME_PHASES", "GAME_END_PHASES"]
