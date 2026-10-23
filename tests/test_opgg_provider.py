"""OP.GG 解析与 Provider 测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.data.database import Database
from src.data.repository import AugmentStatsRepository
from src.opgg.client import OpggClient, OpggError
from src.opgg.parser import parse_augment_stats, parse_augments, parse_meta_patch
from src.opgg.provider import OpggProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PAYLOAD = PROJECT_ROOT / "research" / "opgg_api" / "augments_157.json"

#: Day 3 真机实测的三个海克斯（英雄 Varus = 110）
#:
#: 注意：这里**不能**断言精确数值。OP.GG 每期数据都会刷新，
#: 实测 9-18 -> 9-21 同一批海克斯就漂移了
#:   1325: 70.88/0.49 -> 71.75/0.50
#:   1115: 81.77/1.93 -> 82.28/1.91
#:   1079: 80.07/0.84 -> 79.73/0.84
#: 冻结数值的断言只用于本地已保存的响应样本（见文件头部的 SAMPLE_PAYLOAD）。
REAL_OFFER_IDS = (1325, 1115, 1079)

#: 9-18 抓取时的数值，仅作记录
REAL_OFFERS_SNAPSHOT_20260918 = {1325: (70.88, 0.49), 1115: (81.77, 1.93),
                                 1079: (80.07, 0.84)}


def _payload() -> dict:
    if not SAMPLE_PAYLOAD.exists():
        pytest.skip("缺少 research/opgg_api/augments_157.json 样本")
    return json.loads(SAMPLE_PAYLOAD.read_text(encoding="utf-8"))


def test_parse_augments_keeps_real_fields_only() -> None:
    rows = parse_augments(_payload())
    assert len(rows) > 100
    assert set(rows[0]) == {"augment_id", "opgg_augment_tier", "performance", "popular"}
    soul_siphon = next(r for r in rows if r["augment_id"] == 1077)
    assert soul_siphon["performance"] == 97.24
    assert soul_siphon["popular"] == 6.82
    assert soul_siphon["opgg_augment_tier"] == 0


def test_parse_augment_stats_scope_is_aggregate() -> None:
    stats = parse_augment_stats(_payload(), patch="16.18")
    assert stats
    for item in stats[:50]:
        assert item.scope.aggregate is True
        assert item.scope.label() == "OP.GG Aggregate"
        assert item.scope.region == "unknown"
        assert item.patch == "16.18"
        assert item.source == "opgg"


def test_stats_never_expose_win_rate_or_pick_rate() -> None:
    stats = parse_augment_stats(_payload())
    dumped = stats[0].model_dump()
    assert "win_rate" not in dumped
    assert "pick_rate" not in dumped
    assert "performance" in dumped and "popular" in dumped


def test_parse_meta_patch_absent_in_augments_payload() -> None:
    """海克斯接口没有 meta，patch 必须为 None（不能伪造）。"""
    assert parse_meta_patch(_payload()) is None
    assert parse_meta_patch({"meta": {"version": "16.18"}}) == "16.18"


def test_provider_uses_sqlite_cache(temp_database: Database) -> None:
    repository = AugmentStatsRepository(temp_database)
    provider = OpggProvider(stats_repository=repository)
    stats = parse_augment_stats(_payload(), patch="16.18")
    repository.upsert_many(157, stats, patch="16.18")

    # 只走 SQLite 缓存（客户端不可用，不应发起网络请求）
    result = asyncio.run(provider.get_augment_stats(157, 1077))
    assert result.stats is not None
    assert result.stats.performance == 97.24
    assert result.stats.popular == 6.82
    assert result.stats.scope.label() == "OP.GG Aggregate"
    assert result.source in ("sqlite", "memory")

    offers = provider.offers_with_stats(157, [1077, 1336, 999999])
    assert offers[0]["popular"] == 6.82
    assert offers[2]["popular"] is None
    assert offers[2]["scope"] == "OP.GG Aggregate"
    assert offers[2]["cache_status"] in ("FRESH", "STALE", "MISSING")


def test_provider_reports_error_without_raising() -> None:
    class _BrokenClient:
        async def get_champion_augments(self, champion_id):
            raise OpggError("boom")

        async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):
            raise OpggError("boom")

    provider = OpggProvider(client=_BrokenClient())
    result = asyncio.run(provider.preload_champion(110))
    assert result.augment_count == 0
    assert result.status.value == "MISSING"
    assert provider.last_error


def _network_ok() -> bool:
    try:
        asyncio.run(OpggClient(timeout=15).get_champion_augments(110))
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _network_ok(), reason="OP.GG 不可访问，跳过在线测试")
def test_real_offers_regression_against_opgg() -> None:
    """Day 4 §17 回归：三个真实海克斯在当前英雄下必须都能查到，且字段合法。

    数值会随 OP.GG 刷新而漂移，因此只校验存在性与取值范围，
    不锁定具体数字。
    """
    provider = OpggProvider()
    result = asyncio.run(provider.preload_champion(110))
    if result.augment_count == 0:
        pytest.skip("OP.GG 当前不可达或返回空数据: %s" % provider.last_error)
    assert result.augment_count > 0
    assert result.status.value in ("FRESH", "STALE")
    for augment_id in REAL_OFFER_IDS:
        stats = provider.get_cached(110, augment_id)
        assert stats is not None, f"OP.GG 缺少 augmentId={augment_id}"
        assert isinstance(stats.performance, float)
        assert isinstance(stats.popular, float)
        assert 0.0 <= stats.performance <= 300.0
        assert 0.0 <= stats.popular <= 100.0
        assert stats.scope.label() == "OP.GG Aggregate"
        assert stats.opgg_augment_tier is None or isinstance(stats.opgg_augment_tier, int)


@pytest.mark.skipif(not _network_ok(), reason="OP.GG 不可访问，跳过在线测试")
def test_real_offers_drift_is_small() -> None:
    """与 9-18 快照相比，数值漂移应在合理范围内（只做健康检查）。"""
    provider = OpggProvider()
    result = asyncio.run(provider.preload_champion(110))
    if result.augment_count == 0:
        pytest.skip("OP.GG 当前不可达或返回空数据: %s" % provider.last_error)
    for augment_id, (performance, popular) in REAL_OFFERS_SNAPSHOT_20260918.items():
        stats = provider.get_cached(110, augment_id)
        if stats is None:
            pytest.skip(f"augmentId={augment_id} 已不在 OP.GG 列表中")
        assert abs(stats.performance - performance) < 15.0
        assert abs(stats.popular - popular) < 2.0
