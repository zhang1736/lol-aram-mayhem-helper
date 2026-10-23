"""应用状态机（Day 2 只依据 gameflow-phase + champ-select session）。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from src.lcu.gameflow import GamePhase
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AppState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    WAITING = "WAITING"
    CHAMP_SELECT = "CHAMP_SELECT"
    CHAMPION_LOCKED = "CHAMPION_LOCKED"
    GAME_LOADING = "GAME_LOADING"
    IN_GAME = "IN_GAME"
    AUGMENT_SELECT = "AUGMENT_SELECT"
    GAME_END = "GAME_END"


#: 允许的状态迁移（Day 2 基线；后续 augment screen detector 会补充 AUGMENT_SELECT 入口）
ALLOWED_TRANSITIONS: dict[AppState, set[AppState]] = {
    AppState.DISCONNECTED: {AppState.CONNECTED},
    AppState.CONNECTED: {AppState.WAITING, AppState.DISCONNECTED},
    AppState.WAITING: {AppState.CHAMP_SELECT, AppState.DISCONNECTED},
    AppState.CHAMP_SELECT: {AppState.CHAMPION_LOCKED, AppState.WAITING, AppState.DISCONNECTED},
    AppState.CHAMPION_LOCKED: {AppState.GAME_LOADING, AppState.WAITING, AppState.DISCONNECTED},
    AppState.GAME_LOADING: {AppState.IN_GAME, AppState.WAITING, AppState.DISCONNECTED},
    AppState.IN_GAME: {AppState.AUGMENT_SELECT, AppState.GAME_END, AppState.DISCONNECTED},
    AppState.AUGMENT_SELECT: {AppState.IN_GAME, AppState.GAME_END, AppState.DISCONNECTED},
    AppState.GAME_END: {AppState.WAITING, AppState.DISCONNECTED},
}

#: gameflow 阶段 -> 应用状态
PHASE_TO_STATE: dict[GamePhase, AppState] = {
    GamePhase.NONE: AppState.WAITING,
    GamePhase.LOBBY: AppState.WAITING,
    GamePhase.MATCHMAKING: AppState.WAITING,
    GamePhase.READY_CHECK: AppState.WAITING,
    GamePhase.CHAMP_SELECT: AppState.CHAMP_SELECT,
    GamePhase.GAME_START: AppState.GAME_LOADING,
    GamePhase.IN_PROGRESS: AppState.IN_GAME,
    GamePhase.RECONNECT: AppState.GAME_LOADING,
    # 对局结束阶段（实测：客户端打完一局给的是这些值，而不是字面 "None"）
    GamePhase.PRE_END_OF_GAME: AppState.GAME_END,
    GamePhase.END_OF_GAME: AppState.GAME_END,
    GamePhase.WAITING_FOR_STATS: AppState.GAME_END,
    GamePhase.TERMINATED_IN_ERROR: AppState.GAME_END,
    GamePhase.UNKNOWN: AppState.WAITING,
}


class StateMachine:
    """极简状态机：不合法迁移会被拒绝并记录 WARNING。"""

    def __init__(self, state: AppState = AppState.DISCONNECTED) -> None:
        self._state = state
        self.history: list[AppState] = [state]

    @property
    def state(self) -> AppState:
        return self._state

    def can_transition(self, target: AppState) -> bool:
        if target == self._state:
            return True
        return target in ALLOWED_TRANSITIONS.get(self._state, set())

    def transition(self, target: AppState, force: bool = False) -> bool:
        if target == self._state:
            return True
        if not force and not self.can_transition(target):
            logger.warning("非法状态迁移被拒绝: %s -> %s", self._state.value, target.value)
            return False
        logger.info("State: %s -> %s", self._state.value, target.value)
        self._state = target
        self.history.append(target)
        return True

    # -- 业务驱动 -----------------------------------------------------------
    def update_from_phase(self, phase: GamePhase,
                          champion_locked: Optional[bool] = None) -> AppState:
        """依据 gameflow 阶段推进状态机。

        gameflow 是外部权威观测值：程序可能在选人/对局中途才被启动，
        因此这里允许「跳跃式」推进（会记录日志），
        而 transition() 本身仍然严格校验非法迁移。
        """
        if self._state is AppState.DISCONNECTED:
            self.transition(AppState.CONNECTED, force=True)

        idle_phases = (GamePhase.NONE, GamePhase.LOBBY, GamePhase.MATCHMAKING,
                       GamePhase.READY_CHECK, GamePhase.UNKNOWN,
                       GamePhase.PRE_END_OF_GAME, GamePhase.END_OF_GAME,
                       GamePhase.WAITING_FOR_STATS, GamePhase.TERMINATED_IN_ERROR)
        session_states = (AppState.IN_GAME, AppState.AUGMENT_SELECT,
                          AppState.GAME_LOADING, AppState.CHAMPION_LOCKED,
                          AppState.CHAMP_SELECT)

        if phase in idle_phases and self._state in session_states:
            self.transition(AppState.GAME_END, force=True)

        target = PHASE_TO_STATE.get(phase, AppState.WAITING)
        if target is AppState.CHAMP_SELECT and champion_locked:
            target = AppState.CHAMPION_LOCKED

        if not self.can_transition(target):
            logger.info("状态跳跃: %s -> %s (gameflow=%s)",
                        self._state.value, target.value, phase.value)
        self.transition(target, force=True)
        return self._state

    def mark_disconnected(self) -> None:
        self.transition(AppState.DISCONNECTED, force=True)


__all__ = ["AppState", "StateMachine", "ALLOWED_TRANSITIONS", "PHASE_TO_STATE"]
