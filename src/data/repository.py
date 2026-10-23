"""数据仓库：海克斯统计缓存 + 英雄字典缓存。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from src.core.models import AugmentStats, ChampionInfo, DataScope
from src.data.database import Database
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AugmentStatsRepository:
    """augment_stats_cache 读写。

    缓存键：champion_id + augment_id + source + scope + patch
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_many(self, champion_id: int, stats: Iterable[AugmentStats],
                    patch: Optional[str] = None) -> int:
        rows = []
        for item in stats:
            rows.append((
                champion_id,
                item.augment_id,
                item.performance,
                item.popular,
                item.opgg_augment_tier,
                item.source,
                item.scope.label(),
                patch or item.patch,
                (item.fetched_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            ))
        if not rows:
            return 0
        self.database.executemany(
            """
            INSERT INTO augment_stats_cache
                (champion_id, augment_id, performance, popular, opgg_augment_tier,
                 source, scope, patch, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (champion_id, augment_id, source, scope, patch)
            DO UPDATE SET
                performance = excluded.performance,
                popular = excluded.popular,
                opgg_augment_tier = excluded.opgg_augment_tier,
                fetched_at = excluded.fetched_at
            """,
            rows,
        )
        logger.debug("写入海克斯统计缓存 %d 条 (champion_id=%s)", len(rows), champion_id)
        return len(rows)

    def get(self, champion_id: int, augment_id: int, patch: Optional[str] = None,
            scope: str = "OP.GG Aggregate") -> Optional[dict]:
        rows = self.database.query(
            """
            SELECT * FROM augment_stats_cache
            WHERE champion_id = ? AND augment_id = ? AND scope = ?
              AND (? IS NULL OR patch = ?)
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (champion_id, augment_id, scope, patch, patch),
        )
        return dict(rows[0]) if rows else None

    def list_for_champion(self, champion_id: int, patch: Optional[str] = None) -> list[dict]:
        rows = self.database.query(
            """
            SELECT * FROM augment_stats_cache
            WHERE champion_id = ? AND (? IS NULL OR patch = ?)
            """,
            (champion_id, patch, patch),
        )
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self.database.query("SELECT COUNT(*) AS c FROM augment_stats_cache")[0]
        return int(row["c"])


class ChampionRepository:
    """champion_cache 读写。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, champion: ChampionInfo) -> None:
        self.database.execute(
            """
            INSERT INTO champion_cache (champion_id, alias, name_cn, name_en, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (champion_id) DO UPDATE SET
                alias = excluded.alias,
                name_cn = excluded.name_cn,
                name_en = excluded.name_en,
                updated_at = excluded.updated_at
            """,
            (champion.champion_id, champion.alias, champion.name_cn,
             champion.name_en, _iso_now()),
        )

    def upsert_many(self, champions: Iterable[ChampionInfo]) -> int:
        count = 0
        for champion in champions:
            self.upsert(champion)
            count += 1
        return count

    def get(self, champion_id: int) -> Optional[ChampionInfo]:
        rows = self.database.query(
            "SELECT * FROM champion_cache WHERE champion_id = ?", (champion_id,)
        )
        if not rows:
            return None
        row = rows[0]
        return ChampionInfo(
            champion_id=row["champion_id"],
            alias=row["alias"],
            name_cn=row["name_cn"],
            name_en=row["name_en"],
        )

    def count(self) -> int:
        row = self.database.query("SELECT COUNT(*) AS c FROM champion_cache")[0]
        return int(row["c"])


__all__ = ["AugmentStatsRepository", "ChampionRepository"]
