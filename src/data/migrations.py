"""SQLite 迁移定义。

术语注意：
    scope       数据统计口径，例如 "aggregate" / "kr:ibsg"
    data_tier   统计段位区间（ibsg = Iron/Bronze/Silver/Gold）
    opgg_augment_tier   OP.GG 海克斯 tier（0~5 欢迎度档位）
绝不使用单独的 "tier" 列表示统计段位。
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

#: 迁移列表：(版本号, SQL 语句序列)
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS augment_stats_cache (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                champion_id        INTEGER NOT NULL,
                augment_id         INTEGER NOT NULL,
                performance        REAL,
                popular            REAL,
                opgg_augment_tier  INTEGER,
                source             TEXT NOT NULL DEFAULT 'opgg',
                scope              TEXT NOT NULL DEFAULT 'aggregate',
                patch              TEXT,
                fetched_at         TEXT NOT NULL,
                UNIQUE (champion_id, augment_id, source, scope, patch)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_augment_stats_champion "
            "ON augment_stats_cache (champion_id, patch)",
            """
            CREATE TABLE IF NOT EXISTS champion_cache (
                champion_id  INTEGER PRIMARY KEY,
                alias        TEXT,
                name_cn      TEXT,
                name_en      TEXT,
                updated_at   TEXT NOT NULL
            )
            """,
        ],
    ),
]

LATEST_VERSION = max(version for version, _ in MIGRATIONS)


def current_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def apply_migrations(connection: sqlite3.Connection,
                     migrations: Iterable[tuple[int, list[str]]] = tuple(MIGRATIONS)
                     ) -> int:
    """按版本号顺序执行未应用的迁移，返回最终版本号。"""
    version = current_version(connection)
    for number, statements in sorted(migrations, key=lambda item: item[0]):
        if number <= version:
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (number,),
        )
        connection.commit()
        version = number
    return version


__all__ = ["MIGRATIONS", "LATEST_VERSION", "apply_migrations", "current_version"]
