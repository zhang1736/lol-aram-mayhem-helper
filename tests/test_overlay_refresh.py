"""刷新单槽时 Overlay 的"已确认快照 + 原子更新"回归测试。

实机修过的两个 Bug（2026-09-22）：
  1. 刷新动画中间帧 OCR 失败 → 连续 3 次"牌面不完整" → 整块 Overlay 被隐藏
  2. 单槽刷新期间界面显示"识别中…"，0.75s 后又变回来 → 用户能看到跳动

现在的契约（用户要求）：
    * 正式 UI **只显示已确认快照**；后台识别过程绝不进入界面
    * 刷新期间保持上一份完整快照（名称/强度/欢迎度/★推荐都不动）
    * 新卡确认后**一次性原子替换**整份快照，再重新计算推荐
    * 只有"三个槽一个都确认不了"才允许隐藏 Overlay
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.replay_harness import (  # noqa: E402
    CARDS,
    NAMES,
    SCREENSHOT,
    FakeClock,
    ReplayScreen,
    cut_at_game_end,
    isolated_home,          # noqa: F401 - pytest fixture 需要在这里可见
    real_ocr_engine,        # noqa: F401
    run_replay,
)

OLD_SNAPSHOT = tuple(CARDS)                 # A / B / C
NEW_SNAPSHOT = (1325, 1325, 1079)           # A / D / C（刷新中间槽后）
FORBIDDEN_UI_WORDS = ("识别中", "正在识别", "等待识别", "OCR 中", "数据识别中")


def _snapshots(events: list[dict]) -> list[tuple]:
    return [tuple(o["augment_id"] for o in e["offers"]) for e in events]


def _reroll_middle(index: int, screen: ReplayScreen, clock) -> None:
    clock.advance(0.30)
    if index == 2:
        screen.reroll(dst=1, src=0)         # 玩家刷新中间那个槽


def _no_process_state(events: list[dict]) -> None:
    for event in events:
        for item in event["offers"]:
            for text in (item.get("name_cn") or "",
                         "强度：%s" % item.get("performance", ""),
                         "欢迎度：%s" % item.get("popular", "")):
                for word in FORBIDDEN_UI_WORDS:
                    assert word not in text, "正式 UI 不允许出现 %r：%r" % (word, text)


# ------------------------------------------------- 1) 真实回放：A/B/C -> A/D/C
def test_refresh_replay_keeps_overlay_visible_throughout(monkeypatch,
                                                        temp_database,
                                                        real_ocr_engine) -> None:
    """刷新中间槽：Overlay 全程可见，界面只出现两份稳定快照。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=_reroll_middle, clock=clock)
    events = cut_at_game_end(result["events"])

    assert events, "必须有牌面事件"
    assert _snapshots(events)[0] == OLD_SNAPSHOT
    assert _snapshots(events)[-1] == NEW_SNAPSHOT
    assert set(_snapshots(events)) <= {OLD_SNAPSHOT, NEW_SNAPSHOT}, _snapshots(events)

    # ① 从未 hide，也从未清空
    assert not [e for e in events if e.get("overlay_visible") is False]
    assert not [e for e in events if e.get("offers") == []]
    # ② 全程三张卡 + 没有过程状态文字
    for event in events:
        assert len(event["offers"]) == 3
    _no_process_state(events)
    # ③ offer_version 递增 + 只重算了变化槽
    final = events[-1]
    assert final["offer_version"] == 2
    assert final["changed_slots"] == [1]


# ------------------------------------------------------------ 2) 指定用例
def test_refresh_does_not_hide_overlay(monkeypatch, temp_database,
                                       real_ocr_engine) -> None:
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=_reroll_middle, clock=clock)["events"])
    assert all(e.get("overlay_visible") is not False for e in events)
    assert all(e.get("offers") != [] for e in events)


def test_refresh_one_slot_keeps_other_two_visible(monkeypatch, temp_database,
                                                 real_ocr_engine) -> None:
    """刷新期间另外两张卡的内容/数值必须原样保持。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=_reroll_middle, clock=clock)["events"])
    for event in events:
        assert len(event["offers"]) == 3, "永远保持三张卡"
        assert event["offers"][0]["augment_id"] == 1325
        assert event["offers"][2]["augment_id"] == 1079
        assert event["offers"][0]["name_cn"] == NAMES[1325]
        assert event["offers"][2]["name_cn"] == NAMES[1079]


def test_refresh_pending_slot_does_not_clear_overlay(monkeypatch, temp_database,
                                                    real_ocr_engine) -> None:
    """新卡一直没确认出来：界面必须一直是旧的完整快照（不隐藏、不显示过程状态）。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()

    def on_call(index, screen, clock):
        clock.advance(0.10)                  # 一直没过 0.75s → 一直处于后台确认
        if index == 2:
            screen.reroll(dst=1, src=0)

    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=on_call, clock=clock, max_reads=8)["events"])
    assert _snapshots(events), "必须有牌面事件"
    for event in events:
        assert event["overlay_visible"] is True
        assert len(event["offers"]) == 3
        assert tuple(o["augment_id"] for o in event["offers"]) == OLD_SNAPSHOT
    _no_process_state(events)


def test_refresh_ocr_failure_keeps_overlay(monkeypatch, temp_database,
                                          real_ocr_engine) -> None:
    """刷新后的新卡 OCR 一直失败：保留旧快照，不隐藏、不出现"识别中"。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    fail_slots: set[int] = set()

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index == 2:
            screen.reroll(dst=1, src=0)
            fail_slots.add(1)                # 新卡面始终认不出来

    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=on_call, clock=clock, fail_slots=fail_slots,
        max_reads=8)["events"])
    assert not [e for e in events if e.get("overlay_visible") is False]
    for event in events:
        assert len(event["offers"]) == 3
        assert tuple(o["augment_id"] for o in event["offers"]) in (OLD_SNAPSHOT,
                                                                   NEW_SNAPSHOT)
    _no_process_state(events)


def test_refresh_success_updates_only_changed_slot(monkeypatch, temp_database,
                                                  real_ocr_engine) -> None:
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=_reroll_middle, clock=clock)
    events = cut_at_game_end(result["events"])
    final = events[-1]
    assert tuple(o["augment_id"] for o in final["offers"]) == NEW_SNAPSHOT
    assert final["changed_slots"] == [1], "只有 slot1 变化"
    assert final["rerolled_slots"] == [1]
    # 刷新过程中只对 slot1 做过额外 OCR（另外两槽全程没有重新识别）
    per_slot = result["engine"].per_slot()
    assert per_slot[1] >= 2
    assert per_slot[0] == 1 and per_slot[2] == 1, per_slot


def test_recommendation_is_atomic(monkeypatch, temp_database, real_ocr_engine) -> None:
    """推荐绑定"完整确认快照"：刷新期间保持旧推荐，确认后一次性换成新的。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=_reroll_middle, clock=clock)["events"])

    by_snapshot: dict[tuple, list[int]] = {}
    for event in events:
        if "recommended" not in event:
            continue                     # 只更新 offers 的那一条（推荐随后一条补齐）
        by_snapshot[tuple(o["augment_id"] for o in event["offers"])] = event["recommended"]
    # 旧快照 [A,B,C] popular 0.49/1.93/0.84 → 推荐 B(1115)
    assert by_snapshot[OLD_SNAPSHOT] == [1115]
    # 新快照 [A,D,C] popular 0.49/0.49/0.84 → 推荐 C(1079)
    assert by_snapshot[NEW_SNAPSHOT] == [1079]
    # 不允许出现"半更新"的推荐（例如刷新中推荐变成 [1079] 但卡片还是旧快照）
    for event in events:
        if "recommended" not in event:
            continue
        snap = tuple(o["augment_id"] for o in event["offers"])
        if any(o["data_state"] != "READY" for o in event["offers"]):
            continue                     # OP.GG 还没到 → 暂时没有 ★ 是正常的
        expected = [1115] if snap == OLD_SNAPSHOT else [1079]
        assert event["recommended"] == expected, (snap, event["recommended"])


def test_offer_version_increments(monkeypatch, temp_database, real_ocr_engine) -> None:
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    events = cut_at_game_end(run_replay(
        monkeypatch, temp_database, real_ocr_engine, screen,
        on_call=_reroll_middle, clock=clock)["events"])
    versions = [e["offer_version"] for e in events if "offer_version" in e]
    assert versions[0] == 1
    assert versions[-1] == 2


def test_game_end_still_hides_overlay(monkeypatch, temp_database,
                                      real_ocr_engine) -> None:
    """对局结束仍然必须清空 Overlay。"""
    screen = ReplayScreen(SCREENSHOT)
    clock = FakeClock()
    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=lambda i, s, c: c.advance(0.30), clock=clock,
                        max_reads=4)
    events = result["events"]
    assert events[-1].get("gameflow") == "None"
    assert events[-1].get("overlay_visible") is False
    assert events[-1].get("offers") == []


# ------------------------------------------------- 3) GUI 层 OverlayMode 断言
@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


def _controller(qapp, isolated_home):
    import argparse

    from src.config import load_settings
    from src.ui.app_ui import AppController

    args = argparse.Namespace(
        no_overlay=False, once=False, gui=True, resident=True,
        wait_client=30, wait_game=30, champ_timeout=5, scan_interval=0.25,
        level_poll_interval=1.0, watchdog_interval=1.0, stats_repository=None)
    controller = AppController(load_settings(), args)
    controller.ensure_overlay()
    return controller


def _payload(cards=CARDS, pending=(), performance=None, popular=None) -> list[dict]:
    from tests.replay_harness import REAL_STATS

    rows = []
    for index, cid in enumerate(cards):
        perf, pop = REAL_STATS.get(cid, (performance, popular))
        rows.append({"slot": index, "augment_id": cid, "name_cn": NAMES[cid],
                     "performance": perf, "popular": pop,
                     "recommended": index == 1, "tie": False,
                     "pending": index in pending,
                     "data_state": "READY"})
    return rows


def test_gui_overlay_stays_live_and_never_shows_process_state(qapp,
                                                             isolated_home) -> None:
    """核心断言：LIVE → LIVE → LIVE；且卡片文字里绝不出现"识别中"。"""
    from src.core.state_hub import HUB
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    try:
        HUB.update(offers=_payload(), overlay_visible=True)
        controller.sync_overlay()
        assert controller.overlay_mode is OverlayMode.LIVE

        # 刷新中：调用方**仍然传上一份快照**（pending 只是内部标记）
        HUB.update(offers=_payload(pending={1}), overlay_visible=True)
        controller.sync_overlay()
        assert controller.overlay_mode is OverlayMode.LIVE
        cards = controller.overlay_window._cards    # noqa: SLF001
        names = [card.name.text() for card in cards]
        assert names == [NAMES[1325], NAMES[1115], NAMES[1079]], names
        for card in cards:
            for text in (card.name.text(), card.performance.text(), card.popular.text()):
                for word in FORBIDDEN_UI_WORDS:
                    assert word not in text

        HUB.update(offers=_payload(NEW_SNAPSHOT), overlay_visible=True)
        controller.sync_overlay()
        assert controller.overlay_mode is OverlayMode.LIVE
        assert [o["augment_id"] for o in controller._last_overlay_payload] == \
            list(NEW_SNAPSHOT)
    finally:
        HUB.update(offers=[], overlay_visible=False)
        controller.shutdown()


def test_gui_card_shows_dash_when_opgg_missing(qapp, isolated_home) -> None:
    """OP.GG 没数据时卡片显示 "—"，且仍然是已确认的名称（不是过程状态）。"""
    from src.core.state_hub import HUB

    controller = _controller(qapp, isolated_home)
    try:
        payload = [{"slot": i, "augment_id": cid, "name_cn": NAMES[cid],
                    "performance": None, "popular": None, "recommended": False,
                    "tie": False, "pending": False, "data_state": "MISSING"}
                   for i, cid in enumerate(CARDS)]
        HUB.update(offers=payload, overlay_visible=True)
        controller.sync_overlay()
        cards = controller.overlay_window._cards        # noqa: SLF001
        assert cards[0].name.text() == "玻璃大炮"
        assert cards[0].performance.text() == "强度：—"
        assert cards[0].popular.text() == "欢迎度：—"
    finally:
        HUB.update(offers=[], overlay_visible=False)
        controller.shutdown()


def test_gui_overlay_hides_on_game_end(qapp, isolated_home) -> None:
    from src.core.state_hub import HUB
    from src.overlay.manager import OverlayMode

    controller = _controller(qapp, isolated_home)
    try:
        HUB.update(offers=_payload(), overlay_visible=True)
        controller.sync_overlay()
        assert controller.overlay_mode is OverlayMode.LIVE

        HUB.update(offers=[], overlay_visible=False, gameflow="None")
        controller.sync_overlay()
        assert controller.overlay_mode is OverlayMode.HIDDEN
        assert controller._last_overlay_payload == []
    finally:
        controller.shutdown()


__all__: list[str] = []
