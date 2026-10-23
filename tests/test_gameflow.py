"""Gameflow 阶段解析与状态机测试。"""

from __future__ import annotations

import asyncio

import pytest

from src.core.state_machine import AppState, StateMachine
from src.lcu.client import LcuClient
from src.lcu.gameflow import GamePhase, GameflowMonitor

from tests.conftest import requires_lcu


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("None", GamePhase.NONE),
        ('"None"', GamePhase.NONE),
        ("Lobby", GamePhase.LOBBY),
        ("ChampSelect", GamePhase.CHAMP_SELECT),
        ("InProgress", GamePhase.IN_PROGRESS),
        ("Reconnect", GamePhase.RECONNECT),
        ("SomethingNew", GamePhase.UNKNOWN),
        ("", GamePhase.NONE),
        (None, GamePhase.UNKNOWN),
    ],
)
def test_phase_from_raw(raw, expected) -> None:
    assert GamePhase.from_raw(raw) is expected


def test_game_phase_enum_values_match_lcu() -> None:
    assert GamePhase.NONE.value == "None"
    assert GamePhase.CHAMP_SELECT.value == "ChampSelect"
    assert GamePhase.IN_PROGRESS.value == "InProgress"


def test_state_machine_rejects_illegal_transition() -> None:
    machine = StateMachine(AppState.DISCONNECTED)
    assert machine.transition(AppState.IN_GAME) is False
    assert machine.state is AppState.DISCONNECTED
    assert machine.transition(AppState.CONNECTED) is True


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (GamePhase.NONE, AppState.WAITING),
        (GamePhase.LOBBY, AppState.WAITING),
        (GamePhase.CHAMP_SELECT, AppState.CHAMP_SELECT),
        (GamePhase.GAME_START, AppState.GAME_LOADING),
        (GamePhase.IN_PROGRESS, AppState.IN_GAME),
    ],
)
def test_state_machine_follows_gameflow(phase, expected) -> None:
    machine = StateMachine(AppState.CONNECTED)
    assert machine.update_from_phase(phase) is expected


def test_state_machine_game_end_returns_to_waiting() -> None:
    machine = StateMachine(AppState.CONNECTED)
    machine.update_from_phase(GamePhase.IN_PROGRESS)
    assert machine.state is AppState.IN_GAME
    machine.update_from_phase(GamePhase.NONE)
    assert machine.state is AppState.WAITING


@requires_lcu
def test_live_phase_is_readable(lcu_credentials) -> None:
    client = LcuClient(lcu_credentials)
    assert client.connect() is True

    async def _phase() -> GamePhase:
        try:
            return await GameflowMonitor(client).get_phase()
        finally:
            await client.aclose()

    assert isinstance(asyncio.run(_phase()), GamePhase)
