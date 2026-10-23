"""SQLite 连接与初始化。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from src.data.migrations import LATEST_VERSION, apply_migrations, current_version
from src.utils import paths
from src.utils.logger import get_logger

logger = get_logger(__name__)

def default_db_path() -> Path:
    """正式版数据库一律放在用户可写目录。"""
    return paths.database_path()


class Database:
    """轻量 SQLite 封装（单连接 + 互斥锁，可跨线程使用）。

    实测（2026-09-21 正式版日志）：GUI 形态下数据库在主线程创建、
    却由常驻引擎线程读写，默认 ``check_same_thread=True`` 会让**所有**
    缓存读写直接失败：

        [OPGG] 读取 SQLite 失败: SQLite objects created in a thread can
        only be used in that same thread.

    因此这里显式允许跨线程，并用锁把同一连接的访问串行化。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.version = 0

    # -- 生命周期 -----------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("数据库尚未初始化，请先调用 initialize()")
        return self._connection

    def initialize(self) -> sqlite3.Connection:
        """建库、建表、跑迁移。可重复调用。"""
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        previous = current_version(connection)
        self.version = apply_migrations(connection)
        self._connection = connection
        logger.info("SQLite ready: %s (schema v%d -> v%d, latest v%d)",
                    self.path, previous, self.version, LATEST_VERSION)
        return connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # -- 便捷操作 -----------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self.connection.execute(sql, params)
            self.connection.commit()
            return cursor

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(sql, params).fetchall())

    def executemany(self, sql: str, params_seq) -> None:
        """批量写入 + 提交（跨线程安全）。"""
        with self._lock:
            self.connection.executemany(sql, params_seq)
            self.connection.commit()

    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [row["name"] for row in rows]

    def __enter__(self) -> "Database":
        self.initialize()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["Database", "default_db_path"]
