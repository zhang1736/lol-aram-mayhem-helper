"""OP.GG schema 漂移检测测试。"""

from __future__ import annotations

import asyncio

import pytest

from src.data.repository import AugmentStatsRepository
from src.opgg.client import OpggError
from src.opgg.parser import (
    REQUIRED_AUGMENT_FIELDS,
    SchemaMismatchError,
    parse_augment_stats,
    parse_augments,
)
from src.opgg.provider import OpggProvider


def _row(**overrides) -> dict:
    row = {"id": 1077, "tier": 0, "performance": 97.24, "popular": 6.82}
    row.update(overrides)
    return row


def test_valid_payload_parses() -> None:
    stats = parse_augment_stats({"data": [_row()]}, patch="16.18")
    assert len(stats) == 1
    assert stats[0].performance == 97.24
    assert stats[0].popular == 6.82
    assert stats[0].opgg_augment_tier == 0
    assert stats[0].scope.label() == "OP.GG Aggregate"


def test_empty_data_raises_instead_of_silent_empty_list() -> None:
    with pytest.raises(SchemaMismatchError):
        parse_augments({"data": []})


def test_missing_data_key_raises() -> None:
    with pytest.raises(SchemaMismatchError):
        parse_augments({"payload": []})


def test_data_not_a_list_raises() -> None:
    with pytest.raises(SchemaMismatchError):
        parse_augments({"data": {"id": 1}})


@pytest.mark.parametrize("field", REQUIRED_AUGMENT_FIELDS)
def test_missing_required_field_raises(field: str) -> None:
    row = _row()
    row.pop(field)
    with pytest.raises(SchemaMismatchError):
        parse_augments({"data": [row]})


def test_missing_popular_is_not_treated_as_zero() -> None:
    """字段缺失必须报错，绝不能默认成 popular = 0。"""
    row = _row()
    row.pop("popular")
    with pytest.raises(SchemaMismatchError) as excinfo:
        parse_augments({"data": [row]})
    assert "popular" in str(excinfo.value)


def test_null_performance_is_rejected() -> None:
    with pytest.raises(SchemaMismatchError):
        parse_augments({"data": [_row(performance=None)]})


def test_zero_popular_is_valid_data() -> None:
    """popular = 0 是合法数值（占比极低），不是缺失。"""
    stats = parse_augment_stats({"data": [_row(popular=0)]})
    assert stats[0].popular == 0.0
    assert stats[0].popular is not None


def test_tier_may_be_null() -> None:
    stats = parse_augment_stats({"data": [_row(tier=None)]})
    assert stats[0].opgg_augment_tier is None


def test_invalid_json_raises_schema_error() -> None:
    with pytest.raises(SchemaMismatchError):
        parse_augments("{not json")


def test_provider_survives_schema_drift(temp_database) -> None:
    """schema 漂移时 provider 只记日志、不抛出，并降级为 MISSING。"""

    class _DriftedClient:
        async def get_champion_augments(self, champion_id):
            return {"data": []}

        async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):
            raise OpggError("no hero stats")

    provider = OpggProvider(client=_DriftedClient(),
                            stats_repository=AugmentStatsRepository(temp_database))
    result = asyncio.run(provider.preload_champion(131))
    assert result.augment_count == 0
    assert provider.last_error
