"""v1.0.3 识别与浮层响应速度回归。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.core.state_hub import LatencySummary, PerfMonitor, PerfTrace, StateHub
from src.ocr.card_reader import CHANGE_CONFIRMATIONS_REQUIRED, MIN_SLOT_OCR_INTERVAL


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_read_metrics_split_screenshot_ocr_and_detection() -> None:
    from tests.test_ocr_gate import _reader

    clock = _Clock()
    capture, engine, reader = _reader(clock)
    offer = reader.read_once()

    assert offer.complete
    assert offer.metrics.screenshot_ms >= 0.0
    assert offer.metrics.ocr_ms > 0.0
    assert offer.metrics.total_ms >= offer.metrics.ocr_ms
    assert offer.metrics.ocr_calls == 3
    assert offer.metrics.slot_ocr_calls == [1, 1, 1]

    engine.calls.clear()
    static = reader.read_once()
    assert static.metrics.ocr_calls == 0
    assert engine.calls == []


def test_changed_slot_waits_for_one_stable_new_frame_not_full_075s() -> None:
    """真换牌：两帧一致即可识别，不再被旧 0.75s 固定等待卡住。"""
    from tests.test_ocr_gate import _reader

    clock = _Clock()
    capture, engine, reader = _reader(clock)
    reader.read_once()
    engine.calls.clear()
    capture.set_slot(1, 3)

    clock.advance(MIN_SLOT_OCR_INTERVAL)
    first = reader.read_once()
    assert engine.calls == []
    assert first.pending_slots == [1]

    clock.advance(0.25)
    confirmed = reader.read_once()
    assert engine.calls == [1]
    assert confirmed.readings[1].augment_id == 1079
    assert CHANGE_CONFIRMATIONS_REQUIRED == 2
    assert confirmed.metrics.stabilize_ms >= 250.0


def test_ocr_prewarm_is_singleton_and_does_not_block_caller(monkeypatch) -> None:
    from src.ocr import engine as engine_mod

    class FakeRapidOcr:
        created = 0

        def __init__(self, **kwargs) -> None:  # noqa: ARG002
            type(self).created += 1
            time.sleep(0.08)

    monkeypatch.setattr(engine_mod, "_load_rapidocr",
                        lambda: (FakeRapidOcr, "fake"))
    monkeypatch.setattr(engine_mod, "_SHARED_ENGINE", None)
    monkeypatch.setattr(engine_mod, "_PREWARM_THREAD", None)

    started = time.perf_counter()
    thread = engine_mod.start_ocr_prewarm()
    call_elapsed = time.perf_counter() - started
    assert call_elapsed < 0.05, "预热必须异步启动，不能阻塞 UI/引擎"

    # 与预热线程并发请求同一个实例，仍只能构造一次。
    engine_mod.shared_ocr_engine().ensure_ready()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert FakeRapidOcr.created == 1


def test_perf_trace_percentiles() -> None:
    monitor = PerfMonitor(interval=999)
    for total in (100.0, 120.0, 140.0, 160.0, 180.0):
        trace = monitor.new_trace(trigger="INITIAL")
        trace.total_ms = total
        monitor.record_trace(trace)
    summary = monitor.latency_summary("INITIAL")
    assert summary.count == 5
    assert summary.p50_ms == pytest.approx(140.0)
    assert summary.p95_ms == pytest.approx(180.0)
    assert summary.max_ms == pytest.approx(180.0)
    assert isinstance(summary, LatencySummary)


def test_state_hub_listener_can_be_removed() -> None:
    hub = StateHub()
    events: list[set[str]] = []
    token = hub.subscribe(events.append)
    hub.update(offers=[{"augment_id": 1}])
    hub.unsubscribe(token)
    hub.update(offers=[])
    assert len(events) == 1
    assert "offers" in events[0]


def test_initial_and_next_stage_are_marked_in_perf_trace(monkeypatch,
                                                         temp_database) -> None:
    from src import live as live_mod
    from tests.session_harness import (
        DIANA_OFFERS,
        FakeCardReader,
        FakeLiveClient,
        ScriptedLcuClient,
        install_fakes,
        make_args,
        seed_provider,
    )

    class RecorderHub(StateHub):
        def __init__(self) -> None:
            super().__init__()
            self.traces: list[PerfTrace] = []

        def update(self, **fields):
            trace = fields.get("perf_trace")
            if trace is not None:
                self.traces.append(trace)
            return super().update(**fields)

    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 3:
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(
        phase_for=phase_for,
        champion_id=131,
    )
    live_client = FakeLiveClient(
        available=True, champion_alias="Diana", champion_name="皎月女神", level=3)

    def on_read(n: int) -> None:
        if n == 1:
            live_client.level = 7
        elif recorder.snapshot().stage == 2:
            state["phase"] = "Lobby"

    recorder = RecorderHub()

    def script(n: int):
        if recorder.snapshot().stage == 2:
            return DIANA_OFFERS
        return (1, 2, 3)

    reader = FakeCardReader(
        script=script,
        on_read=on_read,
    )
    # 让第一次和第二次都得到可推荐数据。
    provider = seed_provider(temp_database)
    # 第一次 ID 只需存在，测试关注 trigger，不关注该非真实样本的名称。
    from src.core.models import AugmentStats, DataScope

    provider.stats_repository.upsert_many(131, [
        AugmentStats(augment_id=aid, performance=50.0, popular=float(aid),
                     scope=DataScope.aggregate_scope())
        for aid in (1, 2, 3)
    ], patch=provider.data_patch or "16.18")
    asyncio.run(provider.preload_champion(131, force=True))

    install_fakes(monkeypatch, client=client, live=live_client, reader=reader)
    monkeypatch.setattr(live_mod, "HUB", recorder)
    args = make_args(scan_interval=0.01, level_poll_interval=0.01,
                     watchdog_interval=0.01)

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(), args, client, provider, live_mod.HubOverlay()))

    triggers = [trace.trigger for trace in recorder.traces]
    assert "INITIAL" in triggers
    assert "NEXT_STAGE" in triggers
    for trace in recorder.traces:
        assert trace.total_ms > 0
        assert trace.session_id > 0
        assert "PERF" in trace.describe()


def test_play_one_game_passes_supported_card_reader_keyword(
        monkeypatch, temp_database) -> None:
    """回归：错误的 engine= 曾触发异常并清空已确认英雄。"""
    from src import live as live_mod
    from src.ocr import capture as capture_mod
    from src.ocr import card_reader as card_reader_mod
    from tests.session_harness import (
        FakeCardReader,
        FakeLiveClient,
        ScriptedLcuClient,
        make_args,
        seed_provider,
    )

    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 2:
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=131)
    live_client = FakeLiveClient(
        available=True, champion_alias="Diana", champion_name="皎月女神", level=3)
    reader = FakeCardReader(
        on_read=lambda n: state.update(phase="Lobby"))
    captured: dict = {}

    def reader_factory(capture, *args, **kwargs):
        captured.update(kwargs)
        return reader

    monkeypatch.setattr(live_mod, "LiveClientDataSource", lambda *a, **k: live_client)
    monkeypatch.setattr(capture_mod, "ScreenCapture", lambda *a, **k: reader.capture
                        if hasattr(reader, "capture") else type("Capture", (), {
                            "close": lambda self: None})())
    monkeypatch.setattr(card_reader_mod, "CardReader", reader_factory)
    provider = seed_provider(temp_database)

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(), make_args(), client, provider, live_mod.HubOverlay()))

    assert "ocr_engine" in captured
    assert "engine" not in captured
    assert reader.read_calls >= 1


def test_two_augment_rounds_at_same_level_get_round_1_and_2(
        monkeypatch, temp_database) -> None:
    """死亡后同等级连续两轮必须通过界面消失产生新的 Round。"""
    from src import live as live_mod
    from tests.session_harness import (
        DIANA_OFFERS,
        FakeCardReader,
        FakeLiveClient,
        ScriptedLcuClient,
        install_fakes,
        make_args,
        seed_provider,
    )

    class RecorderHub(StateHub):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[dict] = []

        def update(self, **fields):
            self.events.append(dict(fields))
            return super().update(**fields)

    recorder = RecorderHub()
    monkeypatch.setattr(live_mod, "HUB", recorder)
    monkeypatch.setattr(live_mod, "MISS_SECONDS", 0.05)
    monkeypatch.setattr(live_mod, "MISS_LIMIT", 2)

    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 2:
            state["phase"] = "InProgress"
        return state["phase"]

    first_round = (1325, 1115, 1079)

    def script(n: int):
        snap = recorder.snapshot()
        if snap.offers:
            return None                    # 上一轮界面消失
        if snap.augment_round == 0:
            return first_round
        if snap.augment_round == 1:
            return DIANA_OFFERS
        return None

    def on_read(n: int) -> None:
        snap = recorder.snapshot()
        if (snap.augment_round >= 2 and snap.offers) or n >= 80:
            state["phase"] = "Lobby"

    reader = FakeCardReader(script=script, on_read=on_read)
    client = ScriptedLcuClient(phase_for=phase_for, champion_id=131)
    live_client = FakeLiveClient(
        available=True, champion_alias="Diana", champion_name="皎月女神", level=3)
    install_fakes(monkeypatch, client=client, live=live_client, reader=reader)
    provider = seed_provider(temp_database)

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(),
        make_args(scan_interval=0.005, level_poll_interval=0.005,
                  watchdog_interval=0.005),
        client, provider, live_mod.HubOverlay()))

    rounds_with_offers = {
        event["augment_round"]: tuple(o["augment_id"] for o in event["offers"])
        for event in recorder.events
        if event.get("offers") and "augment_round" in event
    }
    assert rounds_with_offers[1] == first_round
    assert rounds_with_offers[2] == DIANA_OFFERS


@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def test_hub_change_updates_overlay_without_waiting_for_legacy_200ms_poll(
        qapp, isolated_home) -> None:
    from PySide6.QtTest import QTest

    from src.core.state_hub import HUB

    from tests.test_overlay_refresh import _controller, _payload

    controller = _controller(qapp, isolated_home)
    try:
        HUB.update(offers=_payload(), overlay_visible=True)
        QTest.qWait(30)
        assert controller._last_overlay_payload, "HUB 更新后应通过 Qt 信号即时同步"
        assert [card.name.text() for card in controller.overlay_window._cards] == \
            ["玻璃大炮", "最万用的瞄准镜", "战争交响乐"]
    finally:
        HUB.update(offers=[], overlay_visible=False)
        controller.shutdown()


__all__: list[str] = []
