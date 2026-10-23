"""海克斯识别流程状态机与推荐计算。

重要：ARAM: Mayhem 是**三卡槽独立刷新**机制（实机确认）：
    [A,B,C] -> [A,D,C] -> [A,D,E] -> [F,D,E]
每次刷新只替换被点的那一个卡槽，因此：
    * 不做"整组三张连续一致"的防抖（会把正常刷新吃掉）
    * 以 slot 为单位检测变化，只重算变化的槽
    * 版本号叫 offerVersion（不再是 rollIndex）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional, Sequence

from src.utils.logger import get_logger

logger = get_logger(__name__)

SLOT_COUNT = 3
#: 同一对状态反复切换时的日志节流窗口（秒）——只影响日志，不影响状态本身
TRANSITION_LOG_WINDOW = 10.0
#: 每个卡槽在一次海克斯选择内最多允许刷新一次
MAX_REROLLS_PER_SLOT = 1


class AugmentFlowState(str, Enum):
    WAIT_AUGMENT = "WAIT_AUGMENT"   # 等待海克斯界面出现
    SCANNING = "SCANNING"           # 正在 OCR
    STABLE = "STABLE"               # 连续多次一致
    QUERYING = "QUERYING"           # 查本地缓存 / OP.GG
    SHOWING = "SHOWING"             # Overlay 展示中


@dataclass
class OfferUpdate:
    """一次牌面更新结果。"""

    stage: Optional[int]
    augment_round: int
    offer_version: int
    changed_slots: list[int] = field(default_factory=list)
    rerolled_slots: list[int] = field(default_factory=list)
    offers: list[Optional[int]] = field(default_factory=list)
    is_new_stage: bool = False
    is_new_round: bool = False

    def describe(self) -> str:
        return ("Round=%s Stage=%s OfferVersion=%s SlotRerolled=%s Offers=%s"
                % (self.augment_round, self.stage, self.offer_version,
                   self.changed_slots, self.offers))


class AugmentFlow:
    """流程状态 + offerVersion + 逐槽刷新追踪。"""

    def __init__(self) -> None:
        self.state = AugmentFlowState.WAIT_AUGMENT
        self.stage: Optional[int] = None
        self.augment_round: int = 0
        self.offer_version: int = 0
        self.last_signature: Optional[tuple] = None
        self._last_transition_at: dict[tuple[str, str], float] = {}

        #: 本阶段内已发生变化的卡槽（0 基）
        self.changed_slots: list[int] = []
        #: 本阶段内已经被刷过的卡槽集合
        self.rerolled_slots: set[int] = set()
        #: 每个卡槽的刷新次数
        self.slot_reroll_counts: dict[int, int] = {}

    def set_state(self, state: AugmentFlowState) -> None:
        if state is self.state:
            return
        now = time.monotonic()
        key = (self.state.value, state.value)
        previous = self._last_transition_at.get(key, 0.0)
        if now - previous >= TRANSITION_LOG_WINDOW:
            logger.info("AugmentFlow: %s -> %s", self.state.value, state.value)
            self._last_transition_at[key] = now
        else:
            # 同一对状态在窗口内反复切换：保持状态，但日志降级（避免刷屏）
            logger.debug("AugmentFlow: %s -> %s（节流）", self.state.value, state.value)
        self.state = state

    # -- 牌面更新 -----------------------------------------------------------
    def register_offers(self, stage: Optional[int],
                        signature: Sequence,
                        augment_round: Optional[int] = None) -> OfferUpdate:
        """记录一次确认的牌面。

        规则：
          * stage 变化        -> 新一轮，offerVersion 归 1，清空 slot 统计
          * 同一 stage 内变化  -> offerVersion + 1，变化的槽加入 rerolledSlots
          * 完全相同           -> offerVersion 不变
        """
        sig = tuple(signature)
        changed: list[int] = []
        round_changed = (augment_round is not None
                         and augment_round != self.augment_round)
        is_new_stage = stage != self.stage
        is_new_round = is_new_stage or round_changed

        if is_new_round:
            self.stage = stage
            if augment_round is not None:
                self.augment_round = int(augment_round)
            elif self.augment_round <= 0:
                self.augment_round = 1
            else:
                self.augment_round += 1
            self.offer_version = 1
            self.rerolled_slots = set()
            self.slot_reroll_counts = {}
            self.last_signature = None
        elif self.offer_version == 0:
            self.offer_version = 1
        elif self.last_signature is not None:
            changed = [i for i in range(min(len(sig), len(self.last_signature)))
                       if sig[i] != self.last_signature[i]]
            if changed:
                self.offer_version += 1
                for slot in changed:
                    count = self.slot_reroll_counts.get(slot, 0) + 1
                    self.slot_reroll_counts[slot] = count
                    if count > MAX_REROLLS_PER_SLOT:
                        logger.warning("卡槽 %d 刷新次数 %d 超过预期（每槽最多 %d 次）",
                                       slot, count, MAX_REROLLS_PER_SLOT)
                    self.rerolled_slots.add(slot)

        self.changed_slots = changed
        self.last_signature = sig

        update = OfferUpdate(
            stage=self.stage,
            augment_round=self.augment_round,
            offer_version=self.offer_version,
            changed_slots=list(changed),
            rerolled_slots=sorted(self.rerolled_slots),
            offers=[sig[i] if i < len(sig) else None for i in range(SLOT_COUNT)],
            is_new_stage=is_new_stage,
            is_new_round=is_new_round,
        )
        logger.info("%s", update.describe())
        return update

    @property
    def all_slots_rerolled(self) -> bool:
        """三个卡槽是否都已刷新过（应可得到 [0, 1, 2]）。"""
        return self.rerolled_slots == set(range(SLOT_COUNT))

    def reset(self) -> None:
        self.state = AugmentFlowState.WAIT_AUGMENT
        self.stage = None
        self.augment_round = 0
        self.offer_version = 0
        self.last_signature = None
        self.changed_slots = []
        self.rerolled_slots = set()
        self.slot_reroll_counts = {}
        self._last_transition_at.clear()


def recommended_augment_ids(offers) -> list[int]:
    """推荐 = 当前三个选项中欢迎度(popular)最高者。

    - 不使用 performance / win_rate / 任何加权公式
    - popular 缺失(None)的选项不参与比较，也不当作 0
    - 并列最高时全部返回（UI 标注为并列推荐）
    """
    valid = [o for o in offers if getattr(o, "popular", None) is not None]
    if not valid:
        return []
    best = max(o.popular for o in valid)
    return [o.augment_id for o in valid if o.popular == best]


def recommendation_reason(scope_label: str = "OP.GG Aggregate") -> str:
    return f"按当前英雄 {scope_label} 欢迎度最高"


__all__ = [
    "AugmentFlowState",
    "AugmentFlow",
    "OfferUpdate",
    "recommended_augment_ids",
    "recommendation_reason",
    "SLOT_COUNT",
    "MAX_REROLLS_PER_SLOT",
]
