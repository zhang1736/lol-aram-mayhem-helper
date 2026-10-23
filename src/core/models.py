"""领域模型。

命名注意：
    DataScope.tier       统计数据口径段位区间（ibsg = Iron/Bronze/Silver/Gold）
    AugmentStats.opgg_augment_tier   OP.GG 响应里的 tier（0~5 欢迎度档位）
两者语义完全不同，禁止合并成一个 tier。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DataScope(BaseModel):
    """统计数据范围。

    英雄统计接口可以按 region/tier 过滤；海克斯统计接口**不能**，
    因此后者必须记为 aggregate，绝不能伪装成 kr/ibsg。
    """

    region: str = "unknown"
    tier: str = "unknown"
    aggregate: bool = False

    @classmethod
    def aggregate_scope(cls) -> "DataScope":
        return cls(region="unknown", tier="unknown", aggregate=True)

    @classmethod
    def from_settings(cls, region: str, tier: str) -> "DataScope":
        return cls(region=region, tier=tier, aggregate=False)

    def label(self) -> str:
        if self.aggregate:
            return "OP.GG Aggregate"
        return f"{self.region} / {self.tier}"


class ChampionInfo(BaseModel):
    champion_id: int
    alias: Optional[str] = None
    #: LCU champion-summary 提供的显示名（国服为中文，例如 "亚索"）
    name: Optional[str] = None
    name_cn: Optional[str] = None
    name_en: Optional[str] = None

    @property
    def opgg_slug(self) -> Optional[str]:
        """OP.GG 的 URL slug = LCU alias 全小写（Day 1 实测）。"""
        return self.alias.lower() if self.alias else None

    @property
    def display_name(self) -> Optional[str]:
        return self.name_cn or self.name or self.alias


class AugmentInfo(BaseModel):
    """腾讯 KIWI 海克斯字典条目（原样字段，不做加工）。

    level 是海克斯等级：kSilver / kGold / kPrismatic。
    **不要**与 data.tier（统计段位 ibsg）混淆。
    """

    augment_id: int
    name_cn: Optional[str] = None
    name_en: Optional[str] = None
    level: Optional[str] = None
    mode: Optional[str] = None
    desc: Optional[str] = None
    tooltip: Optional[str] = None
    icon: Optional[str] = None
    is_pbe: bool = False
    is_new: bool = False

    @property
    def rarity(self) -> Optional[str]:
        """kSilver/kGold/kPrismatic -> Silver/Gold/Prismatic。"""
        if not self.level:
            return None
        return self.level[1:] if self.level.startswith("k") else self.level

    def modes(self) -> list[str]:
        return [m.strip() for m in (self.mode or "").split(",") if m.strip()]

    def in_mode(self, mode: str) -> bool:
        return mode.upper() in [m.upper() for m in self.modes()]


class AugmentStats(BaseModel):
    """OP.GG 海克斯统计。

    字段严格对齐接口实际返回：
        performance -> 强度
        popular     -> 欢迎度（该英雄内部占比，同一英雄所有条目加总 = 100）
    禁止命名为 win_rate / pick_rate。
    """

    augment_id: int
    performance: Optional[float] = None
    popular: Optional[float] = None
    opgg_augment_tier: Optional[int] = None

    source: str = "opgg"
    scope: DataScope = Field(default_factory=DataScope.aggregate_scope)
    patch: Optional[str] = None
    fetched_at: datetime = Field(default_factory=utc_now)

    #: 展示名（来自游戏字典，非 OP.GG 提供）
    name_cn: Optional[str] = None
    name_en: Optional[str] = None


class AugmentOffer(BaseModel):
    """当前屏幕上的一个海克斯选项。"""

    augment_id: int
    name_cn: Optional[str] = None
    name_en: Optional[str] = None
    performance: Optional[float] = None
    popular: Optional[float] = None
    matched: bool = True


class OPGGAugmentStats(AugmentStats):
    """OP.GG 海克斯统计（Day 4 对外命名）。

    字段与接口一一对应：
        performance        -> 强度
        popular            -> 欢迎度
        opgg_augment_tier  -> OP.GG 的 tier（0~5），**不是**统计段位
    """

    @property
    def scope_label(self) -> str:
        return self.scope.label()


class DiagnosticSnapshot(BaseModel):
    """诊断/主界面展示用的快照。"""

    lcu_connected: bool = False
    lcu_port: Optional[int] = None
    lcu_region: Optional[str] = None
    lcu_locale: Optional[str] = None
    gameflow_phase: str = "Unknown"
    champion_id: Optional[int] = None
    champion_name: Optional[str] = None
    patch: Optional[str] = None


__all__ = [
    "DataScope",
    "ChampionInfo",
    "AugmentInfo",
    "AugmentStats",
    "AugmentOffer",
    "DiagnosticSnapshot",
    "utc_now",
]
