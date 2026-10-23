"""选人阶段等待 championId 的逻辑测试（不依赖真实客户端）。"""

from __future__ import annotations

import asyncio

from src.lcu.champion import ChampionDetector, ChampSelectInfo, ChampSelectMember


class _FakeClient:
    """按脚本返回选人会话。"""

    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.calls = 0

    async def get_json(self, path):
        self.calls += 1
        if not self._sessions:
            return 404, None
        return 200, self._sessions.pop(0)


def _session(champion_id: int, cell: int = 0):
    return {
        "localPlayerCellId": cell,
        "myTeam": [{"cellId": cell, "championId": champion_id,
                    "championPickIntent": 0, "assignedPosition": "",
                    "summonerId": 1}],
        "timer": {"phase": "BAN_PICK"},
    }


def test_wait_for_champion_id_keeps_polling_until_nonzero(monkeypatch) -> None:
    """已实测：BAN_PICK 阶段 championId = 0 属正常，必须继续等待。"""
    client = _FakeClient([_session(0), _session(0), _session(157)])
    detector = ChampionDetector(client)
    result = asyncio.run(detector.wait_for_champion_id(timeout=10, interval=0.01))
    assert result == 157
    assert client.calls >= 3


def test_wait_for_champion_id_returns_none_on_timeout() -> None:
    client = _FakeClient([_session(0)] * 50)
    detector = ChampionDetector(client)
    assert asyncio.run(detector.wait_for_champion_id(timeout=0.05,
                                                     interval=0.01)) is None


def test_wait_for_champion_id_honours_should_continue() -> None:
    client = _FakeClient([_session(0)] * 50)
    detector = ChampionDetector(client)
    result = asyncio.run(detector.wait_for_champion_id(
        timeout=5, interval=0.01, should_continue=lambda: False))
    assert result is None


def test_champ_select_info_resolved_champion_prefers_locked() -> None:
    info = ChampSelectInfo(
        local_player_cell_id=2,
        champion_id=157,
        champion_pick_intent=99,
        my_team=[ChampSelectMember(2, 157, 99, "", 1, True)],
    )
    assert info.resolved_champion_id == 157


def test_champ_select_info_falls_back_to_pick_intent() -> None:
    info = ChampSelectInfo(
        local_player_cell_id=2,
        champion_id=0,
        champion_pick_intent=99,
        my_team=[],
    )
    assert info.resolved_champion_id == 99
