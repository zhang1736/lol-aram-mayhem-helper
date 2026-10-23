"""推荐算法、offerVersion 与三卡槽独立刷新测试。"""

from __future__ import annotations

from src.core.augment_flow import (
    MAX_REROLLS_PER_SLOT,
    AugmentFlow,
    AugmentFlowState,
    recommendation_reason,
    recommended_augment_ids,
)
from src.core.models import AugmentOffer


def _offer(augment_id: int, popular):
    return AugmentOffer(augment_id=augment_id, popular=popular)


# ---------------------------------------------------------------- 推荐算法
def test_recommendation_is_max_popular() -> None:
    offers = [_offer(1325, 0.49), _offer(1115, 1.93), _offer(1079, 0.84)]
    assert recommended_augment_ids(offers) == [1115]


def test_recommendation_ignores_performance() -> None:
    """强度更高但欢迎度低，不能被推荐。"""
    offers = [
        AugmentOffer(augment_id=1, performance=110.0, popular=3.0),
        AugmentOffer(augment_id=2, performance=98.0, popular=8.0),
    ]
    assert recommended_augment_ids(offers) == [2]


def test_recommendation_tie_returns_all() -> None:
    offers = [_offer(1, 6.82), _offer(2, 6.82), _offer(3, 4.05)]
    assert sorted(recommended_augment_ids(offers)) == [1, 2]


def test_recommendation_skips_none_popular() -> None:
    offers = [_offer(1, 6.82), _offer(2, None), _offer(3, 4.05)]
    assert recommended_augment_ids(offers) == [1]


def test_recommendation_all_missing_returns_empty() -> None:
    assert recommended_augment_ids([_offer(1, None), _offer(2, None)]) == []
    assert recommended_augment_ids([]) == []


def test_zero_popular_is_a_real_value_not_missing() -> None:
    """接口返回的 0 是真实数值，必须参与比较。"""
    assert recommended_augment_ids([_offer(1, 0.0), _offer(2, None)]) == [1]


def test_reason_mentions_scope() -> None:
    assert "OP.GG Aggregate" in recommendation_reason()


# ------------------------------------------------------- 三卡槽独立刷新
def test_first_offer_of_stage_is_version_one() -> None:
    flow = AugmentFlow()
    flow.set_state(AugmentFlowState.SCANNING)
    update = flow.register_offers(1, (100, 200, 300))
    assert update.offer_version == 1
    assert update.changed_slots == []
    assert update.is_new_stage is True


def test_single_slot_reroll_increments_offer_version() -> None:
    """实机确认：[A,B,C] -> [A,D,C]：只有 slot 1 变化。"""
    flow = AugmentFlow()
    flow.register_offers(1, (100, 200, 300))

    update = flow.register_offers(1, (100, 900, 300))
    assert update.offer_version == 2
    assert update.changed_slots == [1]
    assert update.rerolled_slots == [1]
    assert update.stage == 1
    assert update.is_new_stage is False


def test_three_slots_rerolled_one_by_one() -> None:
    """完整序列 [A,B,C] -> [A,D,C] -> [A,D,E] -> [F,D,E]。

    结束时 rerolledSlots 应该是 [0, 1, 2]。
    """
    flow = AugmentFlow()
    flow.register_offers(1, (1, 2, 3))
    assert flow.register_offers(1, (1, 9, 3)).changed_slots == [1]
    assert flow.register_offers(1, (1, 9, 8)).changed_slots == [2]
    assert flow.register_offers(1, (7, 9, 8)).changed_slots == [0]

    assert flow.offer_version == 4
    assert flow.rerolled_slots == {0, 1, 2}
    assert flow.all_slots_rerolled is True
    assert flow.stage == 1


def test_unchanged_signature_does_not_bump_version() -> None:
    flow = AugmentFlow()
    flow.register_offers(1, (1, 2, 3))
    assert flow.register_offers(1, (1, 2, 3)).offer_version == 1
    assert flow.register_offers(1, (1, 2, 3)).offer_version == 1


def test_new_stage_resets_version_and_rerolls() -> None:
    flow = AugmentFlow()
    flow.register_offers(1, (1, 2, 3))
    flow.register_offers(1, (1, 9, 3))
    update = flow.register_offers(2, (30, 31, 32))
    assert update.offer_version == 1
    assert update.is_new_stage is True
    assert flow.rerolled_slots == set()
    assert flow.slot_reroll_counts == {}


def test_slot_reroll_counts_tracked() -> None:
    flow = AugmentFlow()
    flow.register_offers(1, (1, 2, 3))
    flow.register_offers(1, (1, 9, 3))
    assert flow.slot_reroll_counts == {1: 1}
    # 同一个槽再次变化：超过每槽 1 次的预期（记 WARNING 但仍记录）
    flow.register_offers(1, (1, 7, 3))
    assert flow.slot_reroll_counts == {1: 2}
    assert MAX_REROLLS_PER_SLOT == 1


def test_offer_update_describe_format() -> None:
    flow = AugmentFlow()
    update = flow.register_offers(2, (1029, 2137, 1211))
    text = update.describe()
    assert "Stage=2" in text
    assert "OfferVersion=1" in text
    assert "SlotRerolled=[]" in text
    assert "Offers=[1029, 2137, 1211]" in text


def test_reset_clears_tracking() -> None:
    flow = AugmentFlow()
    flow.register_offers(1, (1, 2, 3))
    flow.register_offers(1, (1, 9, 3))
    flow.reset()
    assert flow.offer_version == 0
    assert flow.stage is None
    assert flow.rerolled_slots == set()
