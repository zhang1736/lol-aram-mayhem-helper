"""OP.GG 强度/欢迎度显示 + 识别状态机回归测试（问题一 / 问题二）。

问题一（真机日志 2026-09-22 13:07）：OP.GG 请求失败（ConnectError）+ 没有缓存
    → stats=None → 卡片只剩名称，强度/欢迎度没有数值；而且失败后不会重试。
问题二（真机日志 13:08:57~13:09:07）：卡面只要有像素变化（动画/光照）就进
    CHANGED_PENDING → 显示"识别中…"，0.75s 后 OCR 回来又变回名称 → 反复抖动。

修复后的规则：
    * 识别状态：RECOGNIZED / CHANGED_PENDING（只有真换牌才 pending）
    * 数据状态：READY / MISSING（缺失显示 "—"，绝不用"识别中…"表示）
    * OP.GG 按英雄取一次，本地按 augment_id 匹配；失败不清空名称，并低频重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RESPONSE = PROJECT_ROOT / "research" / "opgg_api" / "augments_157.json"

from src.core.models import AugmentStats, DataScope  # noqa: E402
from src.core.state_hub import HUB  # noqa: E402
from src.data.repository import AugmentStatsRepository  # noqa: E402
from src.live import AugmentOfferLike, build_offer_payload  # noqa: E402
from src.ocr.card_reader import CardReading  # noqa: E402
from src.opgg.client import OpggError  # noqa: E402
from src.opgg.provider import OpggProvider  # noqa: E402
from src.overlay.widgets import (  # noqa: E402
    NO_DATA_TEXT,
    format_popular_line,
    format_performance_line,
)
from tests.replay_harness import (  # noqa: E402,F401
    CARDS,
    NAMES,
    FakeClock,
    ReplayScreen,
    cut_at_game_end,
    isolated_home,
    pending_events,
    real_ocr_engine,
    run_replay,
)


# --------------------------------------------------------------------- 工具
#: 正式 UI 禁止出现的字样（只允许出现在 debug/log 里）
FORBIDDEN_UI_WORDS = ("识别中", "正在识别", "等待识别", "OCR 中", "数据识别中")

#: data_state 里表示"还没有数值"的取值（只用于 Debug/日志，UI 统一显示 "—"）
MISSING_STATES = {"MISSING", "LOADING", "ERROR", "NO_STATS", "NOT_REQUESTED"}


def assert_no_process_state(text: str) -> None:
    for word in FORBIDDEN_UI_WORDS:
        assert word not in text, "正式 UI 不允许出现 %r：%r" % (word, text)


class FailingOpggClient:
    """模拟真机里的 ConnectError（OP.GG 请求失败）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_champion_augments(self, champion_id):  # noqa: ARG002
        self.calls += 1
        raise OpggError("OP.GG 请求失败: ConnectError: ")

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        self.calls += 1
        raise OpggError("OP.GG 请求失败: ConnectError: ")

    async def aclose(self) -> None:
        pass


class RecordingOpggClient:
    """按英雄返回真实结构的响应（用 Day 1 保存的真实 JSON）。"""

    def __init__(self, per_champion: dict[int, dict] | None = None) -> None:
        self.per_champion = per_champion or {}
        self.requested: list[int] = []

    async def get_champion_augments(self, champion_id):
        self.requested.append(champion_id)
        payload = self.per_champion.get(champion_id)
        if payload is None:
            raise OpggError("no data for champion %s" % champion_id)
        return payload

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        raise OpggError("hero stats not needed in this test")

    async def aclose(self) -> None:
        pass


def _real_payload() -> dict:
    if not SAMPLE_RESPONSE.exists():
        pytest.skip("缺少 research/opgg_api/augments_157.json 样本")
    return json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))


def _simple_payload(rows: list[tuple[int, float, float]]) -> dict:
    """构造与真实接口同形的响应：data[].{id, tier, performance, popular}。"""
    return {"data": [{"id": aid, "tier": 1, "performance": perf, "popular": pop}
                     for aid, perf, pop in rows]}


def _reading(slot: int, augment_id: int, name: str | None = None) -> CardReading:
    return CardReading(index=slot + 1, raw_text=name or NAMES.get(augment_id, "x"),
                       normalized=name or NAMES.get(augment_id, "x"),
                       augment_id=augment_id, name_cn=name or NAMES.get(augment_id),
                       confidence=1.0, method="exact")


def _provider(client, ttl_hours: float = 6.0) -> OpggProvider:
    from src.config import load_settings

    settings = load_settings()
    settings.cache.opgg_ttl_hours = ttl_hours
    return OpggProvider(client=client, settings=settings,
                        ttl_seconds=ttl_hours * 3600.0)


def _card_texts(offers: list[dict]) -> list[tuple[str, str, str]]:
    """用真实 OverlayWindow 渲染一遍，读回卡片上的三行文字。"""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtWidgets

    from src.overlay.window import OverlayWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = OverlayWindow(screen_index=0, opacity=100, offset_x=0, offset_y=0)
    try:
        window.show_overlay()
        window.set_offers(offers)
        out = []
        for index in range(3):
            if index >= len(offers):
                break
            card = window._cards[index]           # noqa: SLF001 - 测试读回真实控件
            out.append((card.name.text(), card.performance.text(), card.popular.text()))
        _ = app
        return out
    finally:
        window.hide()
        window.close()


# ------------------------------------------------- 1) 已识别 + OP.GG 加载中
def test_recognized_name_stays_while_opgg_loading() -> None:
    """OP.GG 还没回来：名称保持，强度/欢迎度 "—"，不能显示"识别中…"。"""
    provider = _provider(FailingOpggClient())
    provider.begin_session()
    asyncio.run(provider.activate_champion(131, alias="Diana", name="皎月女神"))

    # 该英雄没有任何数据（网络失败 + 无缓存）
    assert provider.query_augment_stats(131, 1325).stats is None

    slot_offers = [AugmentOfferLike(_reading(i, aid), None)
                   for i, aid in enumerate(CARDS)]
    payload, recommended = build_offer_payload(slot_offers)
    assert recommended == []                       # 没数据 → 没有推荐

    rendered = _card_texts(payload)
    assert rendered[0][0] == "玻璃大炮"             # 名称保持
    assert rendered[0][1] == format_performance_line(None) == "强度：—"
    assert rendered[0][2] == format_popular_line(None) == "欢迎度：—"
    for name, perf, pop in rendered:
        for text in (name, perf, pop):
            assert_no_process_state(text)
    assert all(o["data_state"] == "MISSING" for o in payload)


# ---------------------------------------------------------- 2) OCR 单帧失败
def test_ocr_frame_failure_keeps_recognized_id(monkeypatch, temp_database,
                                              real_ocr_engine) -> None:
    """已识别的槽遇到动画/光照变化时：不 OCR、保持 augmentID、不显示识别中…。"""
    screen = ReplayScreen()
    clock = FakeClock()

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index >= 3:
            screen.animate(0, delta=4)             # slot0 只是光照变化

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock, max_reads=6)
    events = cut_at_game_end(result["events"])
    assert not pending_events(events), "动画/光照变化不允许进入识别中…"
    for event in events:
        names = [o["name_cn"] for o in event["offers"]]
        assert names == [NAMES[aid] for aid in CARDS], names
        assert [o["augment_id"] for o in event["offers"]] == list(CARDS)
    # 只做过首轮 3 次 OCR（动画帧被识别为 cosmetic，不再 OCR）
    assert result["engine"].per_slot() == {0: 1, 1: 1, 2: 1}


def test_reroll_unconfirmed_keeps_last_snapshot(monkeypatch, temp_database,
                                               real_ocr_engine) -> None:
    """真换牌但新卡认不出来：界面必须保持上一份完整快照，绝不出现"识别中"。"""
    screen = ReplayScreen()
    clock = FakeClock()
    fail_slots: set[int] = set()

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index == 2:
            screen.reroll(dst=1, src=0)
            fail_slots.add(1)                      # 新卡面一直认不出来

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock, fail_slots=fail_slots,
                        max_reads=8)
    events = cut_at_game_end(result["events"])
    assert events, "必须有牌面事件"
    for event in events:
        assert len(event["offers"]) == 3
        # 界面始终是"上一份完整快照"：三张卡都在、数值都在、没有过程状态
        assert [o["augment_id"] for o in event["offers"]] == [1325, 1115, 1079]
        for item in event["offers"]:
            assert item["name_cn"] in NAMES.values()
            assert item["data_state"] in ({"READY"} | MISSING_STATES)
        assert event["overlay_visible"] is True
    # 数据到位后最终必须是 READY（数值显示出来）
    assert events[-1]["offers"][0]["data_state"] == "READY"
    # 后台确实在重试（内部状态，不进入 UI）
    assert result["engine"].per_slot()[1] >= 2


# ----------------------------------------------------- 3/4) OP.GG 失败 / 成功
def test_opgg_failure_keeps_name_and_dash_values(monkeypatch, temp_database,
                                                real_ocr_engine) -> None:
    """OP.GG 整局失败：名称照常显示，数值 "—"，Overlay 必须一直 LIVE。"""
    screen = ReplayScreen()
    provider = _provider(FailingOpggClient())

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        provider=provider, max_reads=4)
    events = cut_at_game_end(result["events"])
    assert events, "必须有牌面事件"
    for event in events:
        assert event["overlay_visible"] is True
        assert len(event["offers"]) == 3
        for item in event["offers"]:
            assert item["name_cn"] in NAMES.values()      # 名称来自字典，不依赖 OP.GG
            assert item["performance"] is None and item["popular"] is None
            assert item["data_state"] in MISSING_STATES
            assert item["pending"] is False
    assert not [e for e in events if e.get("offers") == []]


def test_opgg_values_are_displayed() -> None:
    """OP.GG 返回 {id, performance, popular} → 强度和欢迎度正确显示。"""
    client = RecordingOpggClient({131: _simple_payload([
        (1325, 63.75, 0.49), (1115, 83.97, 0.68), (1079, 88.23, 1.68)])})
    provider = _provider(client)
    provider.begin_session()

    async def activate_and_preload():
        await provider.activate_champion(131, alias="Diana", name="皎月女神")

    asyncio.run(activate_and_preload())
    assert client.requested == [131], "只允许按英雄请求一次"

    slot_offers = []
    for index, aid in enumerate(CARDS):
        stats = provider.query_augment_stats(131, aid).stats
        assert stats is not None and stats.popular is not None
        slot_offers.append(AugmentOfferLike(_reading(index, aid), stats))
    payload, recommended = build_offer_payload(slot_offers)

    rendered = _card_texts(payload)
    assert rendered[0] == ("玻璃大炮", "强度：63.75", "欢迎度：0.49%")
    assert rendered[1] == ("最万用的瞄准镜", "强度：83.97", "欢迎度：0.68%")
    assert rendered[2] == ("战争交响乐", "强度：88.23", "欢迎度：1.68%")
    assert all(o["data_state"] == "READY" for o in payload)
    assert recommended == [1079]                      # popular 最大


def test_real_opgg_sample_matches_three_offer_ids() -> None:
    """用 Day 1 保存的真实响应：三个 augmentId 必须都能匹配到数值。"""
    client = RecordingOpggClient({157: _real_payload()})
    provider = _provider(client)
    provider.begin_session()
    asyncio.run(provider.activate_champion(157, alias="Yasuo", name="疾风剑豪"))

    matched = {}
    for aid in (1077, 1336, 1328):
        stats = provider.query_augment_stats(157, aid).stats
        assert stats is not None, "真实响应里应该有 augment_id=%s" % aid
        matched[aid] = (stats.performance, stats.popular)
    assert matched[1077] == (97.24, 6.82)
    # 不存在的 augment：必须 not found，而不是给一个假值
    assert provider.query_augment_stats(157, 999999).stats is None


def test_recommendation_picks_largest_popular() -> None:
    """popular 分别 10 / 25 / 18 → 推荐第二个。"""
    rows = [(1325, 70.0, 10.0), (1115, 60.0, 25.0), (1079, 90.0, 18.0)]
    stats = {aid: AugmentStats(augment_id=aid, performance=perf, popular=pop,
                              scope=DataScope.aggregate_scope())
             for aid, perf, pop in rows}
    slot_offers = [AugmentOfferLike(_reading(i, aid), stats[aid])
                   for i, aid in enumerate(CARDS)]
    payload, recommended = build_offer_payload(slot_offers)
    assert recommended == [1115]
    assert [o["recommended"] for o in payload] == [False, True, False]
    # performance 不参与推荐：slot2 强度最高但没有被推荐
    assert payload[2]["performance"] == 90.0


def test_data_state_distinguishes_loading_error_and_no_stats() -> None:
    """§4B：数据状态必须能区分 加载中 / 失败 / 无该条统计 / 就绪。"""
    from src.opgg.provider import StatsState

    no_stats = [AugmentOfferLike(_reading(i, aid), None)
                for i, aid in enumerate(CARDS)]
    for state, expected in ((StatsState.LOADING, "LOADING"),
                            (StatsState.ERROR, "ERROR"),
                            (StatsState.NOT_REQUESTED, "NOT_REQUESTED")):
        payload, _ = build_offer_payload(no_stats, stats_state=state)
        assert all(o["data_state"] == expected for o in payload), (state, payload)

    # 数据已就绪，但该 augment 没有记录 → NO_STATS（而不是 LOADING/ERROR）
    stats = AugmentStats(augment_id=1325, performance=70.88, popular=0.49,
                         scope=DataScope.aggregate_scope())
    partial = [AugmentOfferLike(_reading(0, 1325), stats),
               AugmentOfferLike(_reading(1, 1115), None),
               AugmentOfferLike(_reading(2, 1079), None)]
    payload, recommended = build_offer_payload(partial, stats_state=StatsState.READY)
    assert payload[0]["data_state"] == "READY"
    assert payload[1]["data_state"] == "NO_STATS"
    assert recommended == [1325]


# ------------------------------------------------------- 6/7) reroll 与隐藏
def test_reroll_keeps_other_slots_recognized(monkeypatch, temp_database,
                                            real_ocr_engine) -> None:
    """单槽 reroll：界面保持旧快照，识别完成后一次性原子替换为 A/D/C。"""
    screen = ReplayScreen()
    clock = FakeClock()

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index == 2:
            screen.reroll(dst=1, src=0)

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock)
    events = cut_at_game_end(result["events"])
    # 只允许出现两种稳定牌面：[A,B,C] 与 [A,D,C]，中间不允许任何第三种
    seen = {tuple(o["augment_id"] for o in e["offers"]) for e in events}
    assert seen <= {(1325, 1115, 1079), (1325, 1325, 1079)}, seen
    for event in events:
        for item in event["offers"]:
            assert_no_process_state(item["name_cn"] or "")
            assert item["data_state"] in ({"READY"} | MISSING_STATES)
    final = events[-1]
    assert [o["augment_id"] for o in final["offers"]] == [1325, 1325, 1079]


def test_opgg_loading_never_hides_overlay(monkeypatch, temp_database,
                                         real_ocr_engine, caplog) -> None:
    """OP.GG 正在请求/失败期间，overlay 隐藏条件只能是"界面不存在"。"""
    caplog.set_level(logging.INFO)
    screen = ReplayScreen()
    clock = FakeClock()

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index == 2:
            screen.reroll(dst=1, src=2)            # 真换牌 → 短暂 pending

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock,
                        provider=_provider(FailingOpggClient()), max_reads=8)
    events = cut_at_game_end(result["events"])
    assert not [e for e in events if e.get("overlay_visible") is False]
    assert not [e for e in events if e.get("offers") == []]
    for event in events:
        assert len(event["offers"]) == 3


# ----------------------------------------------- 8/9) 英雄隔离与新局清空
def test_new_game_does_not_reuse_previous_champion_stats(temp_database) -> None:
    """新一局：active champion snapshot 必须清空，不能沿用上一局英雄数据。"""
    client = RecordingOpggClient({
        131: _simple_payload([(1325, 11.11, 9.99)]),
        110: _simple_payload([(1325, 70.88, 0.49)]),
    })
    provider = _provider(client)

    provider.begin_session()
    asyncio.run(provider.activate_champion(131, alias="Diana", name="皎月女神"))
    assert provider.query_augment_stats(131, 1325).stats.popular == 9.99

    provider.end_session()                        # 对局结束
    assert provider.active_champion_id is None
    assert provider.active_opgg_stats == {}
    provider.begin_session()                      # 新一局
    assert provider.query_augment_stats(110, 1325).stats is None   # 不能串到新局

    asyncio.run(provider.activate_champion(110, alias="Varus", name="惩戒之箭"))
    assert provider.query_augment_stats(110, 1325).stats.popular == 0.49
    # 缓存的 SQLite 数据仍在（清 Session ≠ 清 Cache）
    assert client.requested == [131, 110]


def test_champion_change_uses_new_champion_stats() -> None:
    """当前英雄改变后，必须使用新 championId 的 OP.GG 数据。"""
    client = RecordingOpggClient({
        131: _simple_payload([(1325, 11.11, 9.99)]),
        110: _simple_payload([(1325, 70.88, 0.49)]),
    })
    provider = _provider(client)
    provider.begin_session()
    asyncio.run(provider.activate_champion(131, alias="Diana", name="皎月女神"))
    assert provider.query_augment_stats(131, 1325).stats.performance == 11.11

    asyncio.run(provider.activate_champion(110, alias="Varus", name="惩戒之箭"))
    assert provider.active_champion_id == 110
    assert provider.query_augment_stats(110, 1325).stats.performance == 70.88
    # 上一局英雄不可再被当作当前英雄读取
    assert provider.query_augment_stats(131, 1325).stats is None


# ------------------------------------------------- 6/7) 失败重试与"数值晚到"
class FlakyOpggClient:
    """前 N 次请求失败（模拟真机 13:07 的 ConnectError），之后成功。"""

    def __init__(self, payload: dict, failures: int = 1) -> None:
        self.payload = payload
        self.failures = failures
        self.requested: list[int] = []

    async def get_champion_augments(self, champion_id):
        self.requested.append(champion_id)
        if len(self.requested) <= self.failures:
            raise OpggError("OP.GG 请求失败: ConnectError: ")
        return self.payload

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        raise OpggError("OP.GG 请求失败: ConnectError: ")

    async def aclose(self) -> None:
        pass


def test_opgg_failure_schedules_retry_and_recovers(temp_database) -> None:
    """第一次失败 → ERROR + 按退避安排重试；第二次成功 → READY。"""
    from src.opgg.provider import StatsState

    client = FlakyOpggClient(_simple_payload([(1325, 70.0, 0.5)]), failures=1)
    provider = _provider(client)
    provider.retry_backoff = (0.01, 0.02, 0.05)      # 测试用快节奏

    async def flow():
        provider.begin_session()
        await provider.activate_champion_background(131, alias="Diana")
        await asyncio.sleep(0.1)
        assert provider.stats_state(131) is StatsState.ERROR
        assert client.requested == [131]
        # 失败后必须已经排好下一次重试时间（不是"永久缓存失败"）
        assert 131 in provider._next_retry_at      # noqa: SLF001 - 验证退避已排期
        # 到点后重试（第二次请求成功）
        assert provider.maybe_retry(131, now=time.monotonic() + 1) is True
        await asyncio.sleep(0.1)
        return provider.stats_state(131), client.requested

    state, requested = asyncio.run(flow())
    assert state is StatsState.READY
    assert len(requested) >= 2, requested
    assert provider.query_augment_stats(131, 1325).stats.popular == 0.5
    # READY 之后不再重复请求
    assert provider.maybe_retry(131, now=time.monotonic() + 999) is False


def test_retry_backoff_is_1s_2s_5s(temp_database) -> None:
    """§C：第一次失败 1s、第二次 2s、之后 5s；READY 后不再请求。"""
    provider = _provider(FailingOpggClient())
    delays = [provider._schedule_retry(131) for _ in range(4)]   # noqa: SLF001
    assert delays == [1.0, 2.0, 5.0, 5.0]


def test_opgg_late_success_updates_overlay_values(monkeypatch, temp_database,
                                                 real_ocr_engine) -> None:
    """OP.GG 第一次失败、随后成功：卡片先显示 "—"，数值到了自动补上（不用 reroll）。"""
    screen = ReplayScreen()
    clock = FakeClock()
    client = FlakyOpggClient(_simple_payload([
        (1325, 63.75, 0.49), (1115, 83.97, 0.68), (1079, 88.23, 1.68)]),
        failures=1)
    provider = _provider(client)
    provider.retry_backoff = (0.01, 0.02, 0.05)

    def on_call(index, screen, clock):
        clock.advance(0.30)

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock, provider=provider,
                        max_reads=14)          # 多跑几轮，等重试成功
    events = cut_at_game_end(result["events"])
    assert events, "必须有牌面事件"
    # 早期：名称在、数值是 —
    early = events[0]
    assert early["offers"][0]["name_cn"] == "玻璃大炮"
    assert early["offers"][0]["data_state"] in MISSING_STATES
    # 后期：数值自己出现（不需要 reroll）
    ready_states = [e for e in events
                    if all(o["data_state"] == "READY" for o in e["offers"])]
    assert ready_states, "重试成功后必须自动补上强度/欢迎度"
    final = ready_states[-1]
    by_id = {o["augment_id"]: o for o in final["offers"]}
    assert by_id[1325]["performance"] == 63.75
    assert by_id[1325]["popular"] == 0.49
    assert by_id[1079]["popular"] == 1.68
    assert final["recommended"] == [1079]
    # 全程 Overlay 没被隐藏，且名称从未变成"识别中…"
    assert not [e for e in events if e.get("overlay_visible") is False]
    for event in events:
        assert all(o["name_cn"] for o in event["offers"])
        assert not any(o["pending"] for o in event["offers"])


__all__: list[str] = []
