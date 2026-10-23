"""缓存新鲜度判定。

patch + champion + augment 只是缓存 **Key**，不能决定缓存永久有效：
实测同一 patch 内 OP.GG 数据也会漂移，所以必须配合 TTL。

三种状态：
    FRESH   未过期           -> 直接使用
    STALE   已过期但存在      -> 先用旧值，后台刷新（不阻塞海克斯界面）
    MISSING 不存在           -> 等网络
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class CacheStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"

    @property
    def label(self) -> str:
        return {
            CacheStatus.FRESH: "正常（缓存命中）",
            CacheStatus.STALE: "使用缓存，后台刷新",
            CacheStatus.MISSING: "等待网络数据",
        }[self]


@dataclass(frozen=True)
class CacheLookup:
    status: CacheStatus
    age_seconds: Optional[float] = None
    fetched_at: Optional[datetime] = None

    @property
    def usable(self) -> bool:
        """FRESH / STALE 都可以先拿来显示。"""
        return self.status is not CacheStatus.MISSING


def parse_timestamp(value) -> Optional[datetime]:
    """把 SQLite 里存的 ISO 字符串解析成带时区的 datetime。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(fetched_at, now: Optional[datetime] = None) -> Optional[float]:
    parsed = parse_timestamp(fetched_at)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - parsed).total_seconds())


def evaluate(fetched_at, ttl_seconds: float,
             now: Optional[datetime] = None) -> CacheLookup:
    """根据 fetched_at 与 TTL 判定缓存状态。"""
    age = age_seconds(fetched_at, now)
    if age is None:
        return CacheLookup(CacheStatus.MISSING)
    status = CacheStatus.FRESH if age <= ttl_seconds else CacheStatus.STALE
    parsed = parse_timestamp(fetched_at)
    return CacheLookup(status=status, age_seconds=age, fetched_at=parsed)


__all__ = [
    "CacheStatus", "CacheLookup", "evaluate", "age_seconds", "parse_timestamp",
]
