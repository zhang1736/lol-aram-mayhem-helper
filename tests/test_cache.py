"""TTL 判定、stale fallback、missing 缓存测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.data.cache import CacheStatus, evaluate, age_seconds, parse_timestamp
from src.data.repository import AugmentStatsRepository
from src.opgg.client import OpggError
from src.opgg.provider import OpggProvider


class _BrokenClient:
    """所有请求都失败的客户端。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_champion_augments(self, champion_id):
        self.calls += 1
        raise OpggError("simulated network failure")

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):
        raise OpggError("simulated network failure")


def test_cache_ttl_boundary() -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    ttl = 6 * 3600.0

    fresh = evaluate(now - timedelta(hours=5, minutes=59), ttl, now)
    assert fresh.status is CacheStatus.FRESH
    assert fresh.usable

    stale = evaluate(now - timedelta(hours=6, minutes=1), ttl, now)
    assert stale.status is CacheStatus.STALE
    assert stale.usable               # STALE 仍然可以先拿来显示

    missing = evaluate(None, ttl, now)
    assert missing.status is CacheStatus.MISSING
    assert not missing.usable


def test_age_and_parse_helpers() -> None:
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-date") is None
    assert age_seconds(None) is None
    stamp = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert age_seconds(stamp) >= 29 * 60


def test_stale_cache_fallback(temp_database) -> None:
    """缓存过期 + 网络失败 -> 先用旧缓存，再刷新也失败时仍然不抛异常。"""
    repository = AugmentStatsRepository(temp_database)
    from src.core.models import AugmentStats, DataScope

    old = AugmentStats(augment_id=1077, performance=90.0, popular=5.0,
                       opgg_augment_tier=0,
                       scope=DataScope.aggregate_scope(),
                       fetched_at=datetime.now(timezone.utc) - timedelta(hours=48))
    repository.upsert_many(131, [old], patch="16.18")

    client = _BrokenClient()
    provider = OpggProvider(client=client, stats_repository=repository,
                            ttl_seconds=6 * 3600.0)

    # 1) 过期缓存先拿来用（不等网络）
    first = asyncio.run(provider.get_augment_stats(131, 1077))
    assert first.stats is not None
    assert first.stats.performance == 90.0
    assert first.status is CacheStatus.STALE
    assert first.source == "sqlite"

    # 2) 强制刷新：网络失败，但旧缓存仍在，且异常不外抛
    preload = asyncio.run(provider.preload_champion(131, force=True))
    assert preload.augment_count >= 1
    assert preload.status is CacheStatus.STALE
    assert client.calls >= 1
    assert provider.last_error
    again = asyncio.run(provider.get_augment_stats(131, 1077))
    assert again.stats is not None and again.stats.performance == 90.0


def test_missing_cache_reports_missing(temp_database) -> None:
    """无缓存 + 网络失败 -> MISSING，且不抛异常。"""
    repository = AugmentStatsRepository(temp_database)
    provider = OpggProvider(client=_BrokenClient(), stats_repository=repository)
    result = asyncio.run(provider.get_augment_stats(131, 1077))
    assert result.stats is None
    assert result.status is CacheStatus.MISSING
    assert result.source == "none"


def test_fresh_cache_skips_network(temp_database) -> None:
    """FRESH 时不应再访问网络。"""
    repository = AugmentStatsRepository(temp_database)
    from src.core.models import AugmentStats, DataScope

    repository.upsert_many(131, [AugmentStats(
        augment_id=1077, performance=91.0, popular=5.5,
        scope=DataScope.aggregate_scope())], patch="16.18")

    client = _BrokenClient()
    provider = OpggProvider(client=client, stats_repository=repository)
    result = asyncio.run(provider.preload_champion(131))
    assert client.calls == 0
    assert result.status in (CacheStatus.FRESH, CacheStatus.STALE)
