"""SQLite 建库、迁移与仓库读写测试。"""

from __future__ import annotations

import threading

from src.core.models import AugmentStats, ChampionInfo, DataScope
from src.data.database import Database
from src.data.migrations import LATEST_VERSION
from src.data.repository import AugmentStatsRepository, ChampionRepository


def test_database_creates_expected_tables(temp_database: Database) -> None:
    tables = temp_database.table_names()
    assert "augment_stats_cache" in tables
    assert "champion_cache" in tables
    assert "schema_migrations" in tables


def test_database_migration_version(temp_database: Database) -> None:
    assert temp_database.version == LATEST_VERSION


def test_initialize_is_idempotent(tmp_path) -> None:
    path = tmp_path / "again.db"
    first = Database(path)
    first.initialize()
    first.close()
    second = Database(path)
    second.initialize()
    assert second.version == LATEST_VERSION
    second.close()


def test_augment_stats_roundtrip(temp_database: Database) -> None:
    repo = AugmentStatsRepository(temp_database)
    stats = [
        AugmentStats(augment_id=1077, performance=97.24, popular=6.82,
                     opgg_augment_tier=0, scope=DataScope.aggregate_scope()),
        AugmentStats(augment_id=1336, performance=91.46, popular=5.96,
                     opgg_augment_tier=0, scope=DataScope.aggregate_scope()),
    ]
    assert repo.upsert_many(157, stats, patch="16.18") == 2
    assert repo.count() == 2

    row = repo.get(157, 1077, patch="16.18")
    assert row is not None
    assert row["performance"] == 97.24
    assert row["popular"] == 6.82
    assert row["opgg_augment_tier"] == 0
    assert row["scope"] == "OP.GG Aggregate"
    assert row["patch"] == "16.18"


def test_augment_stats_upsert_updates_existing_row(temp_database: Database) -> None:
    repo = AugmentStatsRepository(temp_database)
    repo.upsert_many(157, [AugmentStats(augment_id=1077, popular=1.0)], patch="16.18")
    repo.upsert_many(157, [AugmentStats(augment_id=1077, popular=2.0)], patch="16.18")
    assert repo.count() == 1
    row = repo.get(157, 1077, patch="16.18")
    assert row is not None
    assert row["popular"] == 2.0


def test_augment_stats_missing_returns_none(temp_database: Database) -> None:
    repo = AugmentStatsRepository(temp_database)
    assert repo.get(157, 999999) is None


def test_champion_cache_roundtrip(temp_database: Database) -> None:
    repo = ChampionRepository(temp_database)
    repo.upsert(ChampionInfo(champion_id=157, alias="Yasuo",
                             name_cn="亚索", name_en="Yasuo"))
    saved = repo.get(157)
    assert saved is not None
    assert saved.alias == "Yasuo"
    assert saved.opgg_slug == "yasuo"
    assert repo.count() == 1


def test_data_scope_and_opgg_tier_are_separate_concepts() -> None:
    """统计段位（data tier）与 OP.GG 海克斯档位必须分属不同字段。"""
    scope = DataScope.from_settings("kr", "ibsg")
    stats = AugmentStats(augment_id=1, opgg_augment_tier=4, scope=scope)
    assert scope.tier == "ibsg"
    assert stats.opgg_augment_tier == 4
    assert stats.scope.label() == "kr / ibsg"


def test_aggregate_scope_label() -> None:
    scope = DataScope.aggregate_scope()
    assert scope.aggregate is True
    assert scope.label() == "OP.GG Aggregate"


def test_database_is_usable_from_another_thread(temp_database: Database) -> None:
    """GUI 形态：数据库在主线程 initialize()，却由常驻引擎线程读写。

    回归（2026-09-21 真机日志）：
        [OPGG] 读取 SQLite 失败: SQLite objects created in a thread can only
        be used in that same thread.
    """
    repo = AugmentStatsRepository(temp_database)
    errors: list = []

    def worker() -> None:
        try:
            repo.upsert_many(157, [AugmentStats(
                augment_id=1077, performance=97.24, popular=6.82,
                scope=DataScope.aggregate_scope())], patch="16.18")
            assert repo.count() == 1
            assert repo.get(157, 1077, patch="16.18")["popular"] == 6.82
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker, name="engine-thread")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "跨线程读写 SQLite 阻塞"
    assert errors == [], "跨线程读写 SQLite 失败: %s" % errors
