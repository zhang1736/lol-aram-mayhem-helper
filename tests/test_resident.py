"""常驻模式相关：LCU 重连、对局清理、多局不串数据。"""

from __future__ import annotations

import asyncio

from src.config import load_settings
from src.core.augment_flow import AugmentFlow
from src.core.game_session import GameSession
from src.core.models import AugmentStats, DataScope
from src.data.repository import AugmentStatsRepository
from src.opgg.provider import OpggProvider


class _FakeCredentials:
    port = 1234


class _FakeClient:
    def __init__(self) -> None:
        self.credentials = _FakeCredentials()


def test_lcu_reconnect(monkeypatch) -> None:
    """LCU 掉线后应持续重试，直到重新发现 LeagueClientUx。"""
    from src import live

    settings = load_settings()
    settings.lcu.reconnect_interval = 0.01
    calls = {"n": 0}

    def fake_discover(_settings):
        calls["n"] += 1
        if calls["n"] < 3:
            return None, False
        return _FakeClient(), True

    monkeypatch.setattr(live, "discover_and_connect", fake_discover)
    client = asyncio.run(live.wait_for_client(settings, timeout_minutes=0.05))
    assert client is not None
    assert calls["n"] >= 3


def test_lcu_reconnect_timeout_returns_none(monkeypatch) -> None:
    from src import live

    settings = load_settings()
    settings.lcu.reconnect_interval = 0.01
    monkeypatch.setattr(live, "discover_and_connect", lambda _s: (None, False))
    assert asyncio.run(live.wait_for_client(settings, timeout_minutes=0.02)) is None


def test_game_reset_clears_session_but_keeps_cache(temp_database) -> None:
    """游戏结束：清 session / offers / reroll；缓存必须保留。"""
    repository = AugmentStatsRepository(temp_database)
    provider = OpggProvider(stats_repository=repository)
    repository.upsert_many(131, [AugmentStats(
        augment_id=2064, performance=74.63, popular=1.54,
        scope=DataScope.aggregate_scope())], patch="16.18")
    asyncio.run(provider.preload_champion(131))
    assert provider.get_cached(131, 2064) is not None

    session = GameSession()
    session.set_champion(131, alias="Diana", name="皎月女神")
    session.begin_round(1, [2064, 1116, 1180])
    session.apply_slot_update(2, [1], [2064, 1013, 1180])
    flow = AugmentFlow()
    flow.register_offers(1, (2064, 1116, 1180))

    session.reset()
    flow.reset()

    assert session.is_empty
    assert session.current_offers == []
    assert session.offer_version == 0
    assert session.rerolled_slots == set()
    assert session.recommended_augment_id is None
    assert flow.offer_version == 0
    assert flow.stage is None
    assert provider.get_cached(131, 2064) is not None
    assert repository.count() >= 1


def test_multi_game_session_reset(temp_database) -> None:
    """Game1 Diana -> Game2 Varus：第二局不得继承第一局任何状态。"""
    session = GameSession()
    flow = AugmentFlow()

    # ---- Game 1: Diana ----
    session.set_champion(131, alias="Diana", name="皎月女神")
    session.set_level(3)
    session.begin_round(1, [2064, 1116, 1180])
    flow.register_offers(session.stage, (2064, 1116, 1180))
    session.apply_slot_update(2, [1], [2064, 1013, 1180])
    flow.register_offers(session.stage, (2064, 1013, 1180))
    session.set_level(7)
    session.begin_round(2, [1029, 2137, 1211])
    session.recommended_augment_id = 1211

    # ---- Game End ----
    session.reset()
    flow.reset()

    # ---- Game 2: Varus ----
    session.set_champion(110, alias="Varus", name="惩戒之箭")
    session.set_level(3)
    session.begin_round(1, [1325, 1115, 1079])
    update = flow.register_offers(session.stage, (1325, 1115, 1079))

    assert session.champion_id == 110
    assert session.champion_alias == "Varus"
    assert session.stage == 1
    assert session.offer_version == 1
    assert session.current_offers == [1325, 1115, 1079]
    assert session.rerolled_slots == set()
    assert session.recommended_augment_id is None
    assert flow.offer_version == 1
    assert flow.rerolled_slots == set()
    assert update.changed_slots == []
    assert 2064 not in session.current_offers
    assert 2064 not in session.seen_augments
