"""三卡识别端到端测试（使用 Day 3 的真实截图，不依赖游戏）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.augment_repository import AugmentRepository
from src.ocr.card_reader import CardReader, OfferSet
from src.ocr.matcher import AugmentMatcher
from src.ocr.roi import build_roi_set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT = PROJECT_ROOT / "research" / "captures" / "augment_cards_fixture.png"

EXPECTED_IDS = (1325, 1115, 1079)     # 玻璃大炮 / 最万用的瞄准镜 / 战争交响乐


class ReplayCapture:
    """用真实截图回放 ROI 裁剪，替代 mss。"""

    def __init__(self, image_path: Path) -> None:
        from PIL import Image

        self.image = Image.open(image_path).convert("RGB")
        self.size = self.image.size

    def build_roi_set(self):
        return build_roi_set(*self.size)

    def grab_region(self, box):
        import numpy as np

        return np.array(self.image.crop((box.left, box.top, box.right, box.bottom)))[:, :, :3]


def _reader(clock=None) -> CardReader:
    if not SCREENSHOT.exists():
        pytest.skip("缺少脱敏 OCR 样本 research/captures/augment_cards_fixture.png")
    from src.ocr.engine import OcrEngine

    engine = OcrEngine()
    if not engine.available:
        pytest.skip("RapidOCR 不可用")
    return CardReader(ReplayCapture(SCREENSHOT), engine,
                      AugmentMatcher(AugmentRepository().load()),
                      interval=0.0, required_stable=2, clock=clock)


def test_read_once_matches_all_three_cards() -> None:
    offer = _reader().read_once()
    assert len(offer.readings) == 3
    assert offer.complete
    assert offer.signature == EXPECTED_IDS
    for reading in offer.readings:
        assert reading.method in ("exact", "normalized"), reading.raw_text
        assert reading.confidence >= 0.99
        assert reading.reused is False      # 第一次扫描必须全部 OCR


def test_second_scan_reuses_unchanged_slots() -> None:
    """逐槽优化：像素没变的卡槽不再重复 OCR。"""
    reader = _reader()
    first = reader.read_once()
    assert all(not r.reused for r in first.readings)

    second = reader.read_once()
    assert second.signature == first.signature
    assert all(r.reused for r in second.readings), "静态画面下第二次应全部复用"


def test_changed_slot_is_reocrd() -> None:
    """把某个卡槽的像素改掉：只有该槽重新 OCR（其余复用）。

    P0-2 起同一 slot 有 MIN_SLOT_OCR_INTERVAL（0.75s）保护：
    间隔内的变化只标记 CHANGED_PENDING，等过了间隔才真正 OCR。
    """
    from src.ocr.card_reader import MIN_SLOT_OCR_INTERVAL, SlotOcrState

    class _Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    reader = _reader(clock=clock)
    reader.read_once()

    import numpy as np

    original = reader.capture.grab_region
    roi = reader.capture.build_roi_set()
    band = roi.capture_bounds()
    slot0 = roi.title_box(0)
    slot1 = roi.title_box(1)
    # slot 在 band 内的横向偏移与宽度
    slot0_left = slot0.left - band.left
    slot1_left = slot1.left - band.left
    slot0_width = slot0.width

    def patched(box):
        frame = original(box).copy()
        # 用 slot1 的真实卡面替换 slot0（模拟玩家刷新了 slot0）
        frame[:, slot0_left:slot0_left + slot0_width, :] = \
            frame[:, slot1_left:slot1_left + slot0_width, :]
        return frame

    reader.capture.grab_region = patched

    # 1) 间隔内：只登记状态，不重复 OCR
    after = reader.read_once()
    assert after.readings[0].reused is True, "0.75s 内不允许重复 OCR"
    assert reader.slot_states[0] is SlotOcrState.CHANGED_PENDING

    # 2) 过了最小间隔：被改动的槽才重新 OCR
    clock.now += MIN_SLOT_OCR_INTERVAL + 0.01
    after = reader.read_once()
    assert after.readings[0].reused is False, "被改动的卡槽必须重新 OCR"
    assert after.readings[0].augment_id == 1115, "新卡面必须被识别出来"
    assert after.readings[1].reused is True
    assert after.readings[2].reused is True

def test_read_once_extracts_correct_names() -> None:
    offer = _reader().read_once()
    names = [r.name_cn for r in offer.readings]
    assert names == ["玻璃大炮", "最万用的瞄准镜", "战争交响乐"]


def test_wait_for_stable_confirms_repeated_signature() -> None:
    offer = _reader().wait_for_stable(timeout=20.0)
    assert offer is not None
    assert offer.complete
    assert offer.signature == EXPECTED_IDS
    assert offer.stable_count >= 2


def test_offer_set_helpers() -> None:
    offer = OfferSet()
    assert not offer.complete
    assert offer.signature == (None, None, None)
    assert offer.augment_ids == []


def test_missing_image_capture_does_not_crash(tmp_path: Path) -> None:
    """截图失败时返回未匹配的 reading，而不是抛异常。"""
    if not SCREENSHOT.exists():
        pytest.skip("缺少真实截图样本")
    from src.ocr.engine import OcrEngine

    class BrokenCapture(ReplayCapture):
        def grab_region(self, box):
            raise RuntimeError("capture failed")

    reader = CardReader(BrokenCapture(SCREENSHOT), OcrEngine(),
                        AugmentMatcher(AugmentRepository().load()))
    offer = reader.read_once()
    assert len(offer.readings) == 3
    assert not offer.complete
    assert offer.signature == (None, None, None)


def test_same_frame_ocr_failure_is_retried_after_cooldown() -> None:
    """同一新卡帧偶发 OCR 空结果时，冷却后必须重试，不能永久卡死。"""
    from src.ocr.card_reader import FAILED_FRAME_RETRY_SECONDS
    from tests.test_ocr_gate import SlotFakeEngine, _reader

    class FailOnceEngine(SlotFakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def recognize_texts(self, image, use_det=None):
            slot = int(image[1, 1, 0]) // 10 - 1
            if slot == 1 and not self.failed:
                self.failed = True
                self.calls.append(slot)
                return [""]
            return super().recognize_texts(image, use_det=use_det)

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    capture, _, reader = _reader(clock=clock)
    reader.read_once()
    reader.engine = FailOnceEngine()

    capture.set_slot(1, 3)
    clock.now += 0.25
    reader.read_once()                           # 第一帧只建立候选
    clock.now += 0.25
    failed = reader.read_once()                  # 第二次 OCR 故意返回空
    assert failed.pending_slots == [1]

    clock.now += FAILED_FRAME_RETRY_SECONDS + 0.01
    retried = reader.read_once()
    assert retried.readings[1].augment_id == 1079
    assert retried.readings[1].reused is False


def test_changing_reroll_animation_still_ocr_after_settle_timeout() -> None:
    """刷新动画逐帧变化、没有两帧相同时，也必须在短等待后 OCR。"""
    from src.ocr.card_reader import MIN_REROLL_SETTLE_SECONDS
    from tests.test_ocr_gate import _reader

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    capture, engine, reader = _reader(clock=clock)
    reader.read_once()
    engine.calls.clear()

    capture.set_slot(1, 3)
    clock.now += MIN_REROLL_SETTLE_SECONDS / 2
    first = reader.read_once()
    assert first.pending_slots == [1]
    assert engine.calls == []

    # 动画继续变化，最终帧与上一采样仍不一致。
    capture.set_slot(1, 1)
    clock.now += MIN_REROLL_SETTLE_SECONDS + 0.01
    updated = reader.read_once()
    assert engine.calls == [1]
    assert updated.readings[1].augment_id == 1325
    assert updated.readings[1].reused is False
