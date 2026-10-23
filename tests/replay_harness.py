"""真实牌面回放工具：用真实截图 + 真实 RapidOCR 驱动真实 play_one_game。

被 tests/test_overlay_refresh.py 与 tests/test_opgg_display.py 共用。
原则：不依赖真实 LCU / 真实网络；牌面像素来自 Day 3 的真实截图。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 脱敏测试样本：只保留三张海克斯卡片区域（无 HUD/昵称/小地图/水印）
SCREENSHOT = PROJECT_ROOT / "research" / "captures" / "augment_cards_fixture.png"

#: 截图里真实的三张卡：玻璃大炮 / 最万用的瞄准镜 / 战争交响乐
CARDS = (1325, 1115, 1079)
NAMES = {1325: "玻璃大炮", 1115: "最万用的瞄准镜", 1079: "战争交响乐"}
#: 真实数值（9-18 抓取）——只用于测试断言，不代表当前 OP.GG 数据
REAL_STATS = {1325: (70.88, 0.49), 1115: (81.77, 1.93), 1079: (80.07, 0.84)}


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlotEngine:
    """包住真实 RapidOCR：按标记像素区分 slot，可选让某些槽识别失败。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls: list[int] = []
        self.fail_slots: set[int] = set()

    def recognize_texts(self, image, use_det=None):
        slot = int(image[1, 1, 0]) // 10 - 1
        self.calls.append(slot)
        if slot in self.fail_slots:
            return ["这一定不是海克斯"]
        return self.engine.recognize_texts(image, use_det=use_det)

    def per_slot(self) -> dict[int, int]:
        out = {0: 0, 1: 0, 2: 0}
        for slot in self.calls:
            out[slot] = out.get(slot, 0) + 1
        return out

    def reset(self) -> None:
        self.calls = []


class ReplayScreen:
    """真实截图的假屏幕；支持"把 src 槽的卡面搬到 dst 槽"来模拟刷新。"""

    def __init__(self, path: Path = SCREENSHOT) -> None:
        if not Path(path).exists():
            pytest.skip("缺少脱敏 OCR 测试样本: %s" % path)
        import numpy as np
        from PIL import Image

        from src.ocr.roi import build_roi_set

        self.image = Image.open(path).convert("RGB")
        self.size = self.image.size
        self.frame = np.array(self.image)[:, :, :3]
        self.roi = build_roi_set(*self.size)
        self._anim_phase: dict[int, int] = {}
        for index in range(3):
            box = self.roi.title_box(index)
            self.frame[box.top + 1, box.left + 1] = (10 * (index + 1), 0, 0)

    def build_roi_set(self):
        return self.roi

    def grab_region(self, box):
        return self.frame[box.top:box.bottom, box.left:box.right].copy()

    def close(self) -> None:
        pass

    def reroll(self, dst: int, src: int) -> None:
        """模拟玩家刷新 dst 槽：把 src 槽的卡面搬过去（保留 dst 的标记）。"""
        target = self.roi.title_box(dst)
        source = self.roi.title_box(src)
        self.frame[target.top + 2:target.bottom, target.left + 2:target.right] = \
            self.frame[source.top + 2:source.bottom, source.left + 2:source.right]
        self.frame[target.top + 1, target.left + 1] = (10 * (dst + 1), 0, 0)

    def animate(self, index: int, delta: int = 3) -> None:
        """模拟卡面动画/光照变化（不应该被当成换牌、也不该触发 OCR）。

        亮度在 ±delta 之间来回摆动（不累积），差分量级远小于真换牌。
        """
        import numpy as np

        box = self.roi.title_box(index)
        phase = self._anim_phase.get(index, 0)
        self._anim_phase[index] = 1 - phase
        shift = delta if phase == 0 else -delta
        region = self.frame[box.top + 2:box.bottom, box.left + 2:box.right]
        self.frame[box.top + 2:box.bottom, box.left + 2:box.right] = \
            np.clip(region.astype("int16") + shift, 0, 255).astype("uint8")


@pytest.fixture(scope="module")
def real_ocr_engine():
    pytest.importorskip("rapidocr_onnxruntime")
    from src.ocr.engine import OcrEngine

    engine = OcrEngine()
    if not engine.available:
        pytest.skip("RapidOCR 不可用")
    return engine


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def cut_at_game_end(events: list[dict]) -> list[dict]:
    """返回"本局进行中"的**合并后状态**序列（UI 真正看到的内容）。

    * 从第一张牌面发布开始；末尾连续的 offers=[] 是结算清理，掐掉。
      （中途若出现 offers=[] 会被保留 → 断言能抓到"刷新时被隐藏"这种回归）
    * HUB 是逐字段更新的（overlay.update_offers 先写 offers，
      随后 HUB.update 再补 recommended / offer_version）→ 必须合并
    """
    start = next((i for i, event in enumerate(events) if event.get("offers")), 0)
    end = len(events)
    while end > start and not events[end - 1].get("offers"):
        end -= 1
    state: dict = {}
    out: list[dict] = []
    for event in events[start:end]:
        state.update(event)
        out.append(dict(state))
    return out


def pending_events(events: list[dict]) -> list[dict]:
    return [e for e in events if any(o.get("pending") for o in (e.get("offers") or []))]


def run_replay(monkeypatch, temp_database, engine, screen, *, on_call=None,
               fail_slots=(), max_reads=6, clock=None, seed=None,
               provider=None, champion_id: int = 131,
               champion_alias: str = "Diana", champion_name: str = "皎月女神",
               level: int = 3) -> dict:
    """用真实 play_one_game + 真实 CardReader 回放一局。"""
    from src import live as live_mod
    from src.core.state_hub import StateHub
    from src.data.augment_repository import AugmentRepository
    from src.ocr.card_reader import CardReader
    from src.ocr.matcher import AugmentMatcher
    from tests.session_harness import (
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

    slot_engine = SlotEngine(engine)
    slot_engine.fail_slots = fail_slots if isinstance(fail_slots, set) else set(fail_slots)
    reader = CardReader(screen, slot_engine,
                        AugmentMatcher(AugmentRepository().load()),
                        interval=0.01, min_slot_ocr_interval=0.75,
                        clock=clock or FakeClock())
    recorder = RecorderHub()
    monkeypatch.setattr(live_mod, "HUB", recorder)

    state = {"phase": "ChampSelect"}

    def phase_for(n: int) -> str:
        if n == 3 and state["phase"] == "ChampSelect":
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=champion_id)
    live_client = FakeLiveClient(available=True, champion_alias=champion_alias,
                                 champion_name=champion_name, level=level)

    trace: list[dict] = []
    original = reader.read_once

    def patched_read_once():
        index = len(trace) + 1
        if on_call is not None:
            on_call(index, screen, clock)
        offer = original()
        trace.append({"n": index, "signature": offer.signature,
                      "pending": list(offer.pending_slots),
                      "confirmed": list(offer.confirmed_slots)})
        if index >= max_reads:
            state["phase"] = "Lobby"                 # 结束本局
        return offer

    monkeypatch.setattr(reader, "read_once", patched_read_once)
    install_fakes(monkeypatch, client=client, live=live_client, reader=reader)
    if provider is None:
        provider = seed_provider(temp_database, seed or {
            champion_id: [(aid, perf, pop) for aid, (perf, pop) in REAL_STATS.items()],
        })

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(),
        make_args(scan_interval=0.01, level_poll_interval=0.01,
                  watchdog_interval=0.01),
        client, provider, live_mod.HubOverlay()))
    return {"trace": trace, "events": recorder.events, "reader": reader,
            "engine": slot_engine, "screen": screen, "provider": provider}


__all__ = [
    "SCREENSHOT", "CARDS", "NAMES", "REAL_STATS", "FakeClock", "SlotEngine",
    "ReplayScreen", "real_ocr_engine", "isolated_home", "cut_at_game_end",
    "pending_events", "run_replay",
]
