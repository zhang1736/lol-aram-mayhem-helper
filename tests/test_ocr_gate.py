"""P0-2 扫描门控回归测试（CPU 优化）。

背景（2026-09-22 实测）：
    旧逻辑在对局中每 0.25s 无条件抓屏、像素一变就 OCR →
    3 槽 x 3.3 轮/秒 ≈ 10 次/秒 → 20.7 核 / 86.4% CPU。

现在要求：
    * 没到海克斯窗口：不 OCR（只做 1 次/秒的廉价检测）
    * 到了 stage：开一个最多 10 秒的高频窗口
    * 牌面稳定：OCR = 0
    * 只有变化的 slot 才 OCR，且同一 slot 至少间隔 0.75s
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.core.state_hub import HUB
from src.data.augment_repository import AugmentRepository
from src.ocr.card_reader import MIN_SLOT_OCR_INTERVAL, CardReader, SlotOcrState
from src.ocr.gate import (
    AUGMENT_SCAN_TIMEOUT,
    BandFeature,
    AugmentScanGate,
    ScanMode,
)
from src.ocr.matcher import AugmentMatcher
from src.ocr.roi import build_roi_set
from tests.session_harness import (
    VARUS_OFFERS,
    FakeCardReader,
    FakeLiveClient,
    ScriptedLcuClient,
    install_fakes,
    make_args,
    seed_provider,
)

#: 三个真实海克斯名称（SlotFakeEngine 用 G 通道的"内容码"选择）
NAMES = {1: "玻璃大炮", 2: "最万用的瞄准镜", 3: "战争交响乐"}
IDS = {1: 1325, 2: 1115, 3: 1079}


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlotFrameCapture:
    """固定尺寸的假屏幕：每个 slot 有 (标记像素, 内容块) 用来模拟卡面内容。

    标记像素在 [1,1]（SlotFakeEngine 靠它区分 slot）；
    set_slot() 会改写一整块卡面内容（模拟真换牌，而不是 1 像素噪点）。
    """

    def __init__(self, width: int = 2560, height: int = 1440) -> None:
        import numpy as np

        self.frame = np.zeros((height, width, 3), dtype="uint8")
        self.roi = build_roi_set(width, height)
        self.grabs = 0

    def set_slot(self, index: int, code: int) -> None:
        box = self.roi.title_box(index)
        self.frame[box.top + 1, box.left + 1] = (10 * (index + 1), code, 0)
        # 真换牌 = 卡面大面积变化（不是单个像素）
        self.frame[box.top + 4:box.bottom, box.left + 4:box.right] = \
            (code * 37) % 256

    def add_noise(self, index: int, delta: int = 3) -> None:
        """模拟动画/光照：整块卡面轻微变化（不应触发"识别中…"）。"""
        import numpy as np

        box = self.roi.title_box(index)
        region = self.frame[box.top + 4:box.bottom, box.left + 4:box.right]
        self.frame[box.top + 4:box.bottom, box.left + 4:box.right] = \
            np.clip(region.astype("int16") + delta, 0, 255).astype("uint8")

    def build_roi_set(self):
        return self.roi

    def grab_region(self, box):
        self.grabs += 1
        return self.frame[box.top:box.bottom, box.left:box.right].copy()

    def close(self) -> None:
        pass


class SlotFakeEngine:
    """按标记像素判断是哪个 slot，按内容码返回名称（不加载 ONNX）。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def recognize_texts(self, image, use_det=None):  # noqa: ARG002
        slot = int(image[1, 1, 0]) // 10 - 1
        code = int(image[1, 1, 1])
        self.calls.append(slot)
        return [NAMES.get(code, "")]

    def per_slot(self) -> dict[int, int]:
        out = {0: 0, 1: 0, 2: 0}
        for slot in self.calls:
            if slot in out:
                out[slot] += 1
        return out


def _reader(clock=None, min_interval: float = MIN_SLOT_OCR_INTERVAL):
    capture = SlotFrameCapture()
    engine = SlotFakeEngine()
    matcher = AugmentMatcher(AugmentRepository().load())
    reader = CardReader(capture, engine, matcher, interval=0.0,
                        min_slot_ocr_interval=min_interval, clock=clock)
    for index in range(3):
        capture.set_slot(index, index + 1)
    return capture, engine, reader


def _gate(clock: FakeClock, **overrides) -> AugmentScanGate:
    kwargs = dict(scan_interval=0.25, watchdog_interval=1.0, clock=clock)
    kwargs.update(overrides)
    return AugmentScanGate(**kwargs)


# ------------------------------------------------------------------ 门控逻辑
def test_no_ocr_before_augment_window() -> None:
    """没到海克斯阶段（stage=None）：只低频检测，绝不 OCR。"""
    clock = FakeClock()
    gate = _gate(clock)
    assert gate.mode() is ScanMode.WATCHDOG
    assert gate.interval() == 1.0
    assert gate.armed_stage is None

    gate.update_stage(None)
    assert gate.mode() is ScanMode.WATCHDOG, "stage 未知时不能进入高频扫描"


def test_stage_arms_scan_window() -> None:
    clock = FakeClock(100.0)
    gate = _gate(clock)
    assert gate.update_stage(1) is True          # level 3 -> stage 1
    assert gate.armed_stage == 1
    assert gate.scan_window_active is True
    assert gate.mode() is ScanMode.FAST
    assert gate.interval() == 0.25
    assert gate.window_deadline == 100.0 + AUGMENT_SCAN_TIMEOUT
    # 同一个 stage 不会重复武装
    assert gate.update_stage(1) is False
    assert gate.rearms == 1


def test_stage_window_timeout() -> None:
    clock = FakeClock(0.0)
    gate = _gate(clock, safety_rearm_seconds=0.0)
    gate.update_stage(1)
    clock.advance(AUGMENT_SCAN_TIMEOUT - 0.5)
    assert gate.mode() is ScanMode.FAST
    clock.advance(1.0)                            # 超过 10 秒
    assert gate.mode() is ScanMode.WATCHDOG
    assert gate.scan_window_active is False
    assert gate.stage_completed is False


def test_stage_change_rearms_scan() -> None:
    clock = FakeClock(0.0)
    gate = _gate(clock, safety_rearm_seconds=0.0)
    gate.update_stage(1)
    clock.advance(AUGMENT_SCAN_TIMEOUT + 1)
    assert gate.mode() is ScanMode.WATCHDOG

    assert gate.update_stage(2) is True           # level 7 -> stage 2
    assert gate.armed_stage == 2
    assert gate.mode() is ScanMode.FAST


def test_stable_offers_disable_ocr() -> None:
    """牌面稳定 → 0 次 OCR（但仍在快速跟踪像素变化）。"""
    clock = FakeClock()
    capture, engine, reader = _reader(clock)
    gate = _gate(clock)
    gate.update_stage(1)

    first = reader.read_once()
    assert first.complete
    assert first.signature == VARUS_OFFERS
    assert len(engine.calls) == 3                 # 首次三槽各 OCR 一次

    gate.note_offers(True)                        # 牌面已稳定显示
    assert gate.mode() is ScanMode.FAST

    for _ in range(20):                           # 稳定 20 轮（= 5 秒）
        clock.advance(0.25)
        offer = reader.read_once()
        assert offer.signature == VARUS_OFFERS
        assert all(r.reused for r in offer.readings)
    assert len(engine.calls) == 3, "稳定期间不允许再 OCR"
    assert reader.ocr_calls == 3


def test_changed_slot_only_triggers_ocr() -> None:
    """A B C -> A D C：只有 slot1 重新 OCR。"""
    clock = FakeClock()
    capture, engine, reader = _reader(clock)
    reader.read_once()
    engine.calls.clear()

    capture.set_slot(1, 3)                        # slot1 换成"战争交响乐"
    clock.advance(MIN_SLOT_OCR_INTERVAL + 0.01)   # 过了最小间隔
    offer = reader.read_once()

    assert engine.calls == [1], "只允许 OCR 变化的 slot1"
    assert offer.signature == (1325, 1079, 1079)
    assert offer.readings[0].reused is True
    assert offer.readings[1].reused is False
    assert offer.readings[2].reused is True


def test_slot_ocr_min_interval() -> None:
    """像素一直变（动画）时，同一 slot 至少间隔 0.75s 才 OCR。"""
    clock = FakeClock()
    capture, engine, reader = _reader(clock)
    reader.read_once()
    engine.calls.clear()

    capture.set_slot(1, 3)
    clock.advance(0.10)                           # 不足 0.75s
    deferred = reader.read_once()
    assert engine.calls == [], "动画中间帧不允许 OCR"
    assert deferred.readings[1].reused is True
    assert reader.slot_states[1] is SlotOcrState.CHANGED_PENDING
    assert reader.deferred >= 1

    capture.set_slot(1, 2)                        # 继续变
    clock.advance(0.10)
    reader.read_once()
    assert engine.calls == []

    capture.set_slot(1, 3)                        # 动画结束，最终停在"战争交响乐"
    clock.advance(MIN_SLOT_OCR_INTERVAL)          # 间隔到点 → 只 OCR 一次
    offer = reader.read_once()
    assert engine.calls == [1]
    assert offer.signature == (1325, 1079, 1079)


def test_game_end_disables_ocr() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    gate.update_stage(1)
    gate.note_offers(True)

    gate.note_game_end()
    assert gate.mode() is ScanMode.STOPPED
    assert gate.scan_window_active is False
    assert gate.observe(BandFeature(stable=True)) is False


# ------------------------------------------------------- 不会错过海克斯界面
def test_watchdog_stable_screen_rearms_scan() -> None:
    """窗口超时后画面突然静止（牌面 UI 出现）→ 重新进入高频扫描。"""
    clock = FakeClock(0.0)
    gate = _gate(clock, safety_rearm_seconds=0.0)
    gate.update_stage(1)
    clock.advance(AUGMENT_SCAN_TIMEOUT + 2)
    assert gate.mode() is ScanMode.WATCHDOG

    assert gate.observe(BandFeature(band_hash="a", stable=True), clock.now) is False
    clock.advance(1.0)
    assert gate.observe(BandFeature(band_hash="a", stable=True), clock.now) is True
    assert gate.mode() is ScanMode.FAST


def test_watchdog_brightness_jump_rearms_scan() -> None:
    clock = FakeClock(0.0)
    gate = _gate(clock, safety_rearm_seconds=0.0)
    gate.update_stage(1)
    clock.advance(AUGMENT_SCAN_TIMEOUT + 2)
    assert gate.mode() is ScanMode.WATCHDOG

    for value in (40.0, 42.0, 41.0):
        clock.advance(1.0)
        assert gate.observe(BandFeature(band_hash="x", mean_brightness=value,
                                        changed=True), clock.now) is False
    clock.advance(1.0)
    assert gate.observe(BandFeature(band_hash="y", mean_brightness=120.0,
                                    changed=True), clock.now) is True
    assert gate.mode() is ScanMode.FAST


def test_safety_rearm_prevents_missing_augment() -> None:
    """特征检测都没命中时，保底重开窗口，避免永久错过海克斯。"""
    clock = FakeClock(0.0)
    gate = _gate(clock, safety_rearm_seconds=20.0)
    gate.update_stage(1)
    clock.advance(AUGMENT_SCAN_TIMEOUT + 0.1)
    assert gate.mode() is ScanMode.WATCHDOG
    clock.advance(20.0)
    assert gate.mode() is ScanMode.FAST
    assert gate.history[-1][1] == "SAFETY_REARM"


# ------------------------------------------------------------ 主循环集成
@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def test_loop_does_not_ocr_before_augment_stage(monkeypatch, caplog,
                                                temp_database, isolated_home) -> None:
    """普通对局（等级 1，没有 stage）：主循环只做低频检测，一次 OCR 都没有。"""
    from src import live

    caplog.set_level(logging.INFO)
    HUB.update(client_connected=True)
    provider = seed_provider(temp_database)
    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 3 and state["phase"] == "ChampSelect":
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=131)
    live_client = FakeLiveClient(available=True, champion_alias="Diana",
                                 champion_name="皎月女神", level=1)
    probe_count = {"n": 0}

    def feature_for(n: int):
        probe_count["n"] = n
        if n >= 3:
            state["phase"] = "Lobby"          # 探测几轮后结束本局
        return BandFeature(band_hash="probe-%d" % n, mean_brightness=100.0,
                           changed=True, stable=False)

    reader = FakeCardReader(feature_for=feature_for)
    install_fakes(monkeypatch, client=client, live=live_client, reader=reader)

    outcome = asyncio.run(live.play_one_game(
        live.Settings(), make_args(scan_interval=0.01, watchdog_interval=0.01),
        client, provider, live.HubOverlay()))

    assert outcome == "game_end"
    assert reader.read_calls == 0, "没有 stage 时不允许抓牌面/OCR"
    assert reader.probe_calls >= 3, "低频检测仍在运行"
    assert reader.ocr_calls == 0


def test_loop_enters_scan_when_stage_armed(monkeypatch, caplog,
                                           temp_database, isolated_home) -> None:
    """等级 3（stage 1）→ 立刻进入高频扫描并识别牌面。"""
    from src import live

    caplog.set_level(logging.INFO)
    HUB.update(client_connected=True)
    provider = seed_provider(temp_database)
    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 3 and state["phase"] == "ChampSelect":
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=131)
    live_client = FakeLiveClient(available=True, champion_alias="Diana",
                                 champion_name="皎月女神", level=3)
    stages: list = []

    def on_read(call_index: int) -> None:
        stages.append(HUB.snapshot().stage)
        state["phase"] = "Lobby"               # 识别到牌面后本局结束

    reader = FakeCardReader(script=lambda n: (2064, 1116, 1180), on_read=on_read)
    install_fakes(monkeypatch, client=client, live=live_client, reader=reader)

    asyncio.run(live.play_one_game(
        live.Settings(), make_args(scan_interval=0.01, watchdog_interval=0.01),
        client, provider, live.HubOverlay()))

    assert reader.read_calls >= 1, "stage 到达后必须开始扫描"
    assert stages and stages[0] == 1
    assert any("开启高频扫描窗口" in r.getMessage() for r in caplog.records)


__all__: list[str] = []
