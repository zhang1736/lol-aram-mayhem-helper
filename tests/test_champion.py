"""ChampionDetector 测试（在线部分需要真实客户端）。"""

from __future__ import annotations

import asyncio

from src.lcu.champion import ChampionDetector
from src.lcu.client import LcuClient
from src.lcu.endpoints import CHAMP_SELECT_SESSION

from tests.conftest import requires_lcu


@requires_lcu
def test_champ_select_session_handles_not_selecting(lcu_credentials) -> None:
    """不在选人时必须是 None，而不是异常。"""
    client = LcuClient(lcu_credentials)
    assert client.connect() is True

    async def _probe():
        try:
            detector = ChampionDetector(client)
            session = await detector.get_champ_select_session()
            info = await detector.get_champ_select_info()
            status, _ = await client.get_json(CHAMP_SELECT_SESSION)
            return session, info, status
        finally:
            await client.aclose()

    session, info, status = asyncio.run(_probe())
    # 要么不在选人（404），要么真的拿到了会话
    if status == 404:
        assert session is None
        assert info is None
    else:
        assert isinstance(session, dict)
        assert info is not None
        assert info.local_player_cell_id is not None


@requires_lcu
def test_champion_summary_resolves_alias_and_name(lcu_credentials) -> None:
    """championId -> alias -> name，全部来自 LCU，无需网络。"""
    client = LcuClient(lcu_credentials)
    assert client.connect() is True

    async def _load():
        try:
            detector = ChampionDetector(client)
            summary = await detector.get_champion_summary()
            yasuo = await detector.resolve_champion(157)
            summary_again = await detector.get_champion_summary()
            return summary, yasuo, summary_again
        finally:
            await client.aclose()

    summary, yasuo, summary_again = asyncio.run(_load())
    assert len(summary) > 100
    assert summary is summary_again, "champion-summary 只应请求一次（内存缓存）"
    assert yasuo is not None
    assert yasuo.champion_id == 157
    assert yasuo.alias == "Yasuo"
    assert yasuo.name, "LCU champion-summary 提供中文名"
    assert yasuo.opgg_slug == "yasuo"


@requires_lcu
def test_resolve_unknown_champion_returns_none(lcu_credentials) -> None:
    client = LcuClient(lcu_credentials)
    assert client.connect() is True

    async def _probe():
        try:
            return await ChampionDetector(client).resolve_champion(999999)
        finally:
            await client.aclose()

    assert asyncio.run(_probe()) is None
