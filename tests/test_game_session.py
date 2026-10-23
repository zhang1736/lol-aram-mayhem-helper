"""GameSession 生命周期测试（offerVersion / 逐槽刷新）。"""

from __future__ import annotations

from src.core.game_session import AUGMENT_STAGES, GameSession, stage_from_level


def test_new_session_is_empty() -> None:
    session = GameSession()
    assert session.is_empty
    assert session.offer_version == 0
    assert session.current_offers == []


def test_set_champion() -> None:
    session = GameSession()
    session.set_champion(131, alias="Diana", name="皎月女神")
    assert session.champion_id == 131
    assert session.champion_alias == "Diana"
    assert not session.is_empty


def test_begin_round_sets_stage_and_offer_version() -> None:
    session = GameSession()
    session.begin_round(stage=1, offers=[10, 11, 12])
    assert session.stage == 1
    assert session.offer_version == 1
    assert session.level == AUGMENT_STAGES[1] == 3
    assert session.current_offers == [10, 11, 12]
    assert session.changed_slots == []
    assert session.rerolled_slots == set()
    assert session.seen_augments == {10, 11, 12}


def test_per_slot_reroll_keeps_stage_and_bumps_offer_version() -> None:
    """实机确认的三卡槽独立刷新：[A,B,C] -> [A,D,C] -> [A,D,E] -> [F,D,E]。"""
    session = GameSession()
    session.begin_round(stage=1, offers=[1, 2, 3])

    session.apply_slot_update(2, [1], [1, 9, 3])
    assert session.stage == 1
    assert session.offer_version == 2
    assert session.changed_slots == [1]
    assert session.rerolled_slots == {1}
    assert session.previous_offers == [1, 2, 3]
    assert session.current_offers == [1, 9, 3]

    session.apply_slot_update(3, [2], [1, 9, 8])
    assert session.offer_version == 3
    assert session.rerolled_slots == {1, 2}

    session.apply_slot_update(4, [0], [7, 9, 8])
    assert session.offer_version == 4
    assert session.rerolled_slots == {0, 1, 2}
    assert session.all_slots_rerolled() is True


def test_second_stage_resets_offer_version_and_slots() -> None:
    session = GameSession()
    session.begin_round(stage=1, offers=[1, 2, 3])
    session.apply_slot_update(2, [1], [1, 9, 3])
    session.begin_round(stage=2, offers=[30, 31, 32])
    assert session.stage == 2
    assert session.offer_version == 1
    assert session.level == 7
    assert session.rerolled_slots == set()


def test_seen_augments_accumulates_across_rerolls() -> None:
    session = GameSession()
    session.begin_round(stage=1, offers=[1, 2, 3])
    session.apply_slot_update(2, [1], [1, 9, 3])
    session.apply_slot_update(3, [2], [1, 9, 8])
    assert session.seen_augments == {1, 2, 3, 9, 8}


def test_reset_clears_everything() -> None:
    session = GameSession()
    session.set_champion(131, alias="Diana")
    session.begin_round(stage=1, offers=[1, 2, 3])
    session.apply_slot_update(2, [0], [9, 2, 3])
    session.reset()
    assert session.is_empty
    assert session.stage is None
    assert session.offer_version == 0
    assert session.rerolled_slots == set()
    assert session.seen_augments == set()


def test_stage_from_level_boundaries() -> None:
    assert stage_from_level(None) is None
    assert stage_from_level(2) is None
    assert stage_from_level(3) == 1
    assert stage_from_level(6) == 1
    assert stage_from_level(7) == 2
    assert stage_from_level(10) == 2
    assert stage_from_level(11) == 3
    assert stage_from_level(14) == 3
    assert stage_from_level(15) == 4
    assert stage_from_level(18) == 4


def test_set_level_ignores_regression() -> None:
    session = GameSession()
    session.set_level(7)
    assert session.stage == 2
    session.set_level(4)          # 异常回退
    assert session.stage == 2
    assert session.level == 4
