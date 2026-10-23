"""单局会话状态。

术语（ARAM: Mayhem 实机确认）：
    stage        第几次海克斯（1~4，对应 Lv.3/7/11/15）
    offerVersion 当前牌面的版本号；每次有卡槽被刷新就 +1
                 （以前的 rollIndex 已改名，避免与"整组刷新"混淆）
    changedSlots 本次变化（被刷新）的卡槽下标，0 基
    rerolledSlots 本阶段内已经刷新过的卡槽集合

三卡槽是**独立刷新**的：一次刷新只换一个槽。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

#: 海克斯发生等级
AUGMENT_STAGES: dict[int, int] = {1: 3, 2: 7, 3: 11, 4: 15}

SLOT_COUNT = 3


def stage_from_level(level: int | None) -> int | None:
    """等级 -> 海克斯阶段。

    Day 4 暂定区间（后续有更多实战数据再验证边界）：
        3 <= level < 7   -> 1
        7 <= level < 11  -> 2
        11 <= level < 15 -> 3
        level >= 15      -> 4
        其它（<3）       -> None（还没到第一次海克斯）
    """
    if level is None:
        return None
    if level >= 15:
        return 4
    if level >= 11:
        return 3
    if level >= 7:
        return 2
    if level >= 3:
        return 1
    return None


@dataclass
class GameSession:
    #: 当前生效英雄（= 候选英雄，或进入对局后由 Live Client 确定的最终英雄）
    champion_id: Optional[int] = None
    champion_alias: Optional[str] = None
    champion_name: Optional[str] = None

    #: ChampSelect 阶段的**动态候选**英雄：玩家换英雄时必须跟着变
    candidate_champion_id: Optional[int] = None
    candidate_champion_alias: Optional[str] = None
    candidate_champion_name: Optional[str] = None

    #: 进入 InProgress 之后，由 Live Client Data API 确定的**最终**英雄（权威来源）
    final_champion_id: Optional[int] = None
    final_champion_alias: Optional[str] = None
    final_champion_name: Optional[str] = None

    stage: Optional[int] = None
    level: Optional[int] = None

    #: 牌面版本号（原 rollIndex）
    offer_version: int = 0

    current_offers: list[int] = field(default_factory=list)
    previous_offers: list[int] = field(default_factory=list)

    #: 最近一次变化的卡槽（0 基）与本阶段已刷新过的卡槽
    changed_slots: list[int] = field(default_factory=list)
    rerolled_slots: set[int] = field(default_factory=set)

    seen_augments: set[int] = field(default_factory=set)

    #: 预留：Day 4 不在此计算推荐，仅保留字段
    recommended_augment_id: Optional[int] = None

    # -- 英雄 ---------------------------------------------------------------
    def set_champion(self, champion_id: Optional[int], alias: Optional[str] = None,
                     name: Optional[str] = None) -> None:
        """设置当前生效英雄（不改动 candidate / final 语义）。"""
        self.champion_id = champion_id
        self.champion_alias = alias
        self.champion_name = name

    def set_candidate_champion(self, champion_id: Optional[int],
                               alias: Optional[str] = None,
                               name: Optional[str] = None) -> None:
        """ChampSelect 阶段的候选英雄（允许被下一次选择覆盖）。"""
        self.candidate_champion_id = champion_id
        self.candidate_champion_alias = alias
        self.candidate_champion_name = name
        self.set_champion(champion_id, alias, name)

    def set_final_champion(self, champion_id: Optional[int],
                           alias: Optional[str] = None,
                           name: Optional[str] = None) -> None:
        """进入对局后由 Live Client API 确定的最终英雄（权威）。"""
        self.final_champion_id = champion_id
        self.final_champion_alias = alias
        self.final_champion_name = name
        self.set_champion(champion_id, alias, name)

    @property
    def active_champion_id(self) -> Optional[int]:
        """本 Session 用于查询 OP.GG 的英雄：final 优先，其次 candidate。"""
        return self.final_champion_id or self.candidate_champion_id or self.champion_id

    # -- 等级/阶段 ----------------------------------------------------------
    @property
    def expected_level(self) -> Optional[int]:
        return AUGMENT_STAGES.get(self.stage) if self.stage else None

    def set_level(self, level: int | None) -> None:
        """由 Live Client API 的等级更新 stage（不改动 offer_version）。"""
        self.level = level
        stage = stage_from_level(level)
        if stage is not None and stage != self.stage:
            if self.stage is not None and stage < self.stage:
                logger.warning("stage 回退被忽略: %s -> %s (level=%s)",
                               self.stage, stage, level)
                return
            self.stage = stage
            logger.info("Level %s -> stage %s (该阶段等级 = %s)",
                        level, stage, AUGMENT_STAGES.get(stage))

    # -- 牌面 ---------------------------------------------------------------
    def begin_round(self, stage: int, offers: Iterable[int]) -> None:
        """进入新一次海克斯选择：offerVersion 归 1，清空槽位统计。"""
        self._archive_current_offers()
        self.stage = stage
        self.level = AUGMENT_STAGES.get(stage)
        self.offer_version = 1
        self.current_offers = list(offers)
        self.changed_slots = []
        self.rerolled_slots = set()
        self.seen_augments.update(self.current_offers)

    def apply_slot_update(self, offer_version: int, changed_slots: Iterable[int],
                          offers: Iterable[int]) -> None:
        """按逐槽刷新更新牌面（stage 不变，只记变化的槽）。"""
        self._archive_current_offers()
        self.offer_version = offer_version
        self.changed_slots = sorted(changed_slots)
        self.current_offers = list(offers)
        self.rerolled_slots.update(self.changed_slots)
        self.seen_augments.update(self.current_offers)

    def _archive_current_offers(self) -> None:
        if self.current_offers:
            self.previous_offers = list(self.current_offers)

    def all_slots_rerolled(self) -> bool:
        return self.rerolled_slots == set(range(SLOT_COUNT))

    # -- 生命周期 -----------------------------------------------------------
    def reset(self) -> None:
        """整局结束：清空一切，绝不把上一局数据带到下一局。"""
        self.champion_id = None
        self.champion_alias = None
        self.champion_name = None
        self.candidate_champion_id = None
        self.candidate_champion_alias = None
        self.candidate_champion_name = None
        self.final_champion_id = None
        self.final_champion_alias = None
        self.final_champion_name = None
        self.stage = None
        self.level = None
        self.offer_version = 0
        self.current_offers = []
        self.previous_offers = []
        self.changed_slots = []
        self.rerolled_slots = set()
        self.seen_augments = set()
        self.recommended_augment_id = None

    @property
    def is_empty(self) -> bool:
        return self.champion_id is None and not self.current_offers and self.stage is None


__all__ = ["GameSession", "AUGMENT_STAGES", "stage_from_level", "SLOT_COUNT"]
