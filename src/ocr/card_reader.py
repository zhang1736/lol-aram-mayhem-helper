"""三卡识别：ROI -> OCR -> normalize -> match -> augmentId，并做稳定性确认。"""

from __future__ import annotations

import time
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from src.data.augment_repository import AugmentRepository
from src.ocr.capture import ScreenCapture
from src.ocr.engine import OcrEngine
from src.ocr.matcher import AugmentMatcher, normalize_text
from src.ocr.roi import RoiSet
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL = 0.35
DEFAULT_REQUIRED_STABLE = 3
DEFAULT_TIMEOUT = 12.0

#: 同一个 slot 两次 OCR 之间的最小间隔（§10）。
#: 即使像素一直在变（动画/游戏画面），也不会 0.25s 一次地重复 OCR，
#: 只更新哈希；等过了这个间隔再 OCR 一次，拿到"最后稳定"的名字。
MIN_SLOT_OCR_INTERVAL = 0.75

#: 判断"画面只是动画/光照"还是"真的换牌了"的像素差异阈值（2026-09-22 实测）：
#:   真换牌（不同海克斯）：平均差 16.0~22.1，强变化像素 8.1~13.6%
#:   噪点 ±6 / 亮度 +8%  ：平均差 3.2~3.9，强变化 0%
#:   中部高光动画         ：平均差 5.9，强变化 0%
#: 只要差异低于 IGNORE 阈值，就认为"卡面没换"，**保持已识别状态**（不 OCR、不显示识别中…）。
CARD_DIFF_STRONG_LEVEL = 40          # 单像素差多少算"强变化"
CARD_CHANGE_IGNORE_MEAN = 8.0        # 平均差 <= 8 且强变化 < 3% → 纯动画/光照
CARD_CHANGE_IGNORE_STRONG = 0.03
#: OCR 失败时，变化达到这个量级才认为是"换牌进行中"（显示识别中…）
CARD_REROLL_MEAN = 15.0
CARD_REROLL_STRONG = 0.06


class SlotOcrState(str, Enum):
    """单个 slot 的 OCR 状态（§11）。"""

    UNCHANGED = "UNCHANGED"              # 还没建立基线
    CHANGED_PENDING = "CHANGED_PENDING"  # 像素变了，但还在等最小间隔 → 先不 OCR
    OCR_IN_PROGRESS = "OCR_IN_PROGRESS"  # 本次正在 OCR
    STABLE = "STABLE"                    # 当前像素与该槽上次 OCR 时一致（读数可信）


@dataclass(frozen=True)
class BandFeature:
    """窗口外的廉价特征（只抓名称带算哈希/亮度，不做 OCR）。"""

    band_hash: str = ""
    mean_brightness: float = 0.0
    changed: bool = False
    stable: bool = False


@dataclass(frozen=True)
class PixelDiff:
    """两张卡面 ROI 的差异程度（用来区分"动画"和"真换牌"）。"""

    mean: float = 0.0          # 平均灰度差
    strong: float = 0.0        # 强变化像素比例（差 > CARD_DIFF_STRONG_LEVEL）

    @property
    def cosmetic(self) -> bool:
        """只是动画 / 光照 / 噪点 → 不算换牌。"""
        return (self.mean <= CARD_CHANGE_IGNORE_MEAN
                and self.strong <= CARD_CHANGE_IGNORE_STRONG)

    @property
    def reroll_like(self) -> bool:
        """像真的换了牌（用于 OCR 失败时判断是否显示"识别中…"）。"""
        return self.mean >= CARD_REROLL_MEAN or self.strong >= CARD_REROLL_STRONG


@dataclass
class CardReading:
    index: int                 # 1 / 2 / 3
    raw_text: str
    normalized: str
    augment_id: Optional[int] = None
    name_cn: Optional[str] = None
    confidence: float = 0.0
    method: str = "none"
    #: 本次是复用上一次结果（像素未变）还是重新 OCR
    reused: bool = False

    @property
    def matched(self) -> bool:
        return self.augment_id is not None


@dataclass
class OfferSet:
    readings: list[CardReading] = field(default_factory=list)
    stable_count: int = 0
    #: 正在等待 OCR（动画中 / 未过最小间隔 / 上一帧识别失败）的卡槽下标（0 基）
    pending_slots: list[int] = field(default_factory=list)
    #: 当前像素已经确认过的卡槽下标（读数可信，不需要重试）
    confirmed_slots: list[int] = field(default_factory=list)

    @property
    def signature(self) -> tuple[Optional[int], Optional[int], Optional[int]]:
        ids = [r.augment_id for r in self.readings]
        while len(ids) < 3:
            ids.append(None)
        return (ids[0], ids[1], ids[2])

    @property
    def complete(self) -> bool:
        return len(self.readings) == 3 and all(r.matched for r in self.readings)

    @property
    def matched_slots(self) -> list[int]:
        """识别成功的卡槽下标（0 基）。"""
        return [i for i, r in enumerate(self.readings) if r.matched]

    @property
    def has_any_offer(self) -> bool:
        """至少有一张卡识别成功 → 说明海克斯界面还在屏幕上。

        单槽刷新失败时**不允许**把整块 Overlay 判成"界面消失"（见 §6/§10）。
        """
        return bool(self.matched_slots)

    @property
    def confirmed_count(self) -> int:
        return len(self.confirmed_slots)

    @property
    def augment_ids(self) -> list[int]:
        return [r.augment_id for r in self.readings if r.augment_id is not None]

    def describe(self) -> str:
        return " / ".join(
            (f"{r.augment_id}" if r.matched else f"?({r.raw_text or '空'})")
            for r in self.readings
        )


class CardReader:
    """三卡识别器。capture 可注入（便于测试）。"""

    def __init__(self, capture, ocr_engine: OcrEngine | None = None,
                 matcher: AugmentMatcher | None = None,
                 interval: float = DEFAULT_INTERVAL,
                 required_stable: int = DEFAULT_REQUIRED_STABLE,
                 min_slot_ocr_interval: float = MIN_SLOT_OCR_INTERVAL,
                 clock=None) -> None:
        self.capture = capture
        self.engine = ocr_engine or OcrEngine()
        self.matcher = matcher
        self.interval = interval
        self.required_stable = required_stable
        #: 同一 slot 的最小 OCR 间隔（秒）
        self.min_slot_ocr_interval = float(min_slot_ocr_interval)
        self.clock = clock or time.monotonic
        #: 每个 slot 上次 OCR 的时间
        self._slot_ocr_at: list[float] = [float("-inf")] * 3
        self.slot_states: list[SlotOcrState] = [SlotOcrState.UNCHANGED] * 3
        #: 性能统计（验收用）
        self.ocr_calls = 0
        self.deferred = 0
        self.probe_calls = 0
        self._probe_hash: Optional[str] = None
        self._slot_hashes: list[str] | None = None
        self._slot_readings: list[CardReading] | None = None
        #: 每个 slot 上一次"识别失败"的那一帧哈希：同一帧不重复重试
        self._slot_failed_hash: list[Optional[str]] = [None, None, None]
        #: 每个 slot 上次"确认过"的卡面图（用来判断是动画还是真换牌）
        self._slot_ref_image: list[Any] = [None, None, None]
        #: 每个 slot 进入"等确认"状态的起始时间（用于长时间确认不出来的兜底）
        self._slot_pending_since: list[Optional[float]] = [None, None, None]
        #: 每个 slot 最近一次采样的哈希（兜底降级时用来避免重复重试同一帧）
        self._slot_sample_hash: list[Optional[str]] = [None, None, None]
        #: 统计：被判定为"只是动画"而忽略的帧数（性能/行为观测）
        self.cosmetic_ignored = 0

    # -- 内部 ---------------------------------------------------------------
    def _matcher(self) -> AugmentMatcher:
        if self.matcher is None:
            self.matcher = AugmentMatcher(AugmentRepository().load())
        return self.matcher

    def _roi(self) -> RoiSet:
        return self.capture.build_roi_set()

    # -- 单次识别 -----------------------------------------------------------
    def read_once(self) -> OfferSet:
        """一次扫描：屏幕只抓一次，三张卡在内存里裁。

        **识别状态机**（与"OP.GG 数据状态"完全分开）：

            RECOGNIZED    该槽已经有确认的 augmentID
            CHANGED_PENDING 真的换牌了、新内容还没识别出来（UI 才显示"识别中…"）

        逐槽决策（顺序很重要）：
            1. 像素与上次确认时一致          → RECOGNIZED，0 次 OCR
            2. 像素有变化但属于动画/光照/噪点 → **保持 RECOGNIZED**，不 OCR、不 pending
               （2026-09-22 实测：动画噪点平均差 3~6，强变化 0%；
                 真换牌平均差 16~22，强变化 8~14%）
            3. 变化明显但还没过最小 OCR 间隔  → 只有"像真换牌"时才是 CHANGED_PENDING，
               否则保持 RECOGNIZED
            4. 过了最小间隔 → 只对该槽 OCR：
               * 认出同一个 augmentID → 保持 RECOGNIZED（不产生任何 UI 抖动）
               * 认出不同 augmentID   → RECOGNIZED(新 id)（真换牌完成）
               * 没认出来             → 变化大才 CHANGED_PENDING，否则保持 RECOGNIZED
        """
        roi = self._roi()
        matcher = self._matcher()
        readings: list[CardReading] = []
        hashes: list[Optional[str]] = []
        pending: list[int] = []
        confirmed: list[int] = []
        now = self.clock()

        band = None
        frame = None
        try:
            band = roi.capture_bounds()
            frame = self.capture.grab_region(band)
        except Exception as exc:  # noqa: BLE001
            logger.warning("整条名称带截图失败: %s", exc)

        for index in range(3):
            box = roi.title_box(index)
            image = (_slice(frame, band, box)
                     if (frame is not None and band is not None) else None)
            previous = self._slot_readings[index] if self._slot_readings else None
            previous_hash = self._slot_hashes[index] if self._slot_hashes else None

            if image is None:
                self.slot_states[index] = SlotOcrState.UNCHANGED
                readings.append(CardReading(index + 1, "", "", None, None, 0.0, "none"))
                hashes.append(previous_hash)
                continue

            digest = _hash_image(image)
            self._slot_sample_hash[index] = digest
            if previous is not None and previous_hash == digest:
                # 1) 与该槽上次确认时的像素一模一样 → 读数依然可信，不 OCR
                self.slot_states[index] = SlotOcrState.STABLE
                self._slot_failed_hash[index] = None
                self._slot_pending_since[index] = None
                if previous.matched:
                    confirmed.append(index)
                readings.append(_reuse_reading(previous))
                hashes.append(previous_hash)
                continue

            diff = _pixel_diff(image, self._slot_ref_image[index])
            if diff is not None and diff.cosmetic:
                # 2) 只是动画 / 光照 / 噪点 → 保持已识别状态，不 OCR、不显示"识别中…"
                self.cosmetic_ignored += 1
                self.slot_states[index] = SlotOcrState.STABLE
                self._slot_pending_since[index] = None
                if previous is not None and previous.matched:
                    confirmed.append(index)
                readings.append(_reuse_reading(previous) if previous is not None
                                else CardReading(index + 1, "", "", None, None, 0.0, "none"))
                hashes.append(previous_hash)
                continue

            reroll_like = diff is not None and diff.reroll_like
            #: 这个槽从来没被确认过 → 任何"还没识别出来"的状态都该显示"识别中…"
            never_recognized = previous is None or not previous.matched
            pending_now = reroll_like or never_recognized
            if now - self._slot_ocr_at[index] < self.min_slot_ocr_interval:
                # 3) 最小间隔保护：先不 OCR；只有"像真换牌"才标记识别中…
                self.deferred += 1
                if pending_now:
                    self.slot_states[index] = SlotOcrState.CHANGED_PENDING
                    pending.append(index)
                    if self._slot_pending_since[index] is None:
                        self._slot_pending_since[index] = now
                else:
                    self.slot_states[index] = SlotOcrState.STABLE
                    self._slot_pending_since[index] = None
                    if previous is not None and previous.matched:
                        confirmed.append(index)
                readings.append(_reuse_reading(previous) if previous is not None
                                else CardReading(index + 1, "", "", None, None, 0.0, "none"))
                hashes.append(previous_hash)
                continue

            if digest == self._slot_failed_hash[index]:
                # 这一帧已经试过且没识别出来：不再重试（等像素变化）
                if pending_now:
                    self.slot_states[index] = SlotOcrState.CHANGED_PENDING
                    pending.append(index)
                    if self._slot_pending_since[index] is None:
                        self._slot_pending_since[index] = now
                else:
                    self.slot_states[index] = SlotOcrState.STABLE
                    self._slot_pending_since[index] = None
                    if previous is not None and previous.matched:
                        confirmed.append(index)
                readings.append(_reuse_reading(previous) if previous is not None
                                else CardReading(index + 1, "", "", None, None, 0.0, "none"))
                hashes.append(previous_hash)
                continue

            # ROI 已经是裁好的单行文字：跳过检测模型（实测快 20~30 倍）
            self.slot_states[index] = SlotOcrState.OCR_IN_PROGRESS
            texts = self.engine.recognize_texts(image, use_det=False)
            raw = "".join(texts).strip()
            match = matcher.match(raw)
            self.ocr_calls += 1
            self._slot_ocr_at[index] = now

            if match.augment_id is None:
                # 4c) 没认出来：保留旧读数；变化大才算"换牌进行中"
                self._slot_failed_hash[index] = digest
                if pending_now:
                    self.slot_states[index] = SlotOcrState.CHANGED_PENDING
                    pending.append(index)
                    if self._slot_pending_since[index] is None:
                        self._slot_pending_since[index] = now
                    logger.debug("slot%d 换牌中未识别(%r)，保留旧读数并等待重试", index, raw)
                else:
                    # 已经识别过的槽：一帧 OCR 失败**不允许**把它降级成"识别中…"
                    self.slot_states[index] = SlotOcrState.STABLE
                    self._slot_pending_since[index] = None
                    if previous.matched:
                        confirmed.append(index)
                    logger.debug("slot%d OCR 未匹配(%r)，保持已识别结果", index, raw)
                readings.append(_reuse_reading(previous) if previous is not None
                                else CardReading(index + 1, raw, normalize_text(raw),
                                                 None, None, match.confidence, match.method))
                hashes.append(previous_hash)
                continue

            same_as_before = (previous is not None
                              and previous.augment_id == match.augment_id)
            self.slot_states[index] = SlotOcrState.STABLE
            self._slot_failed_hash[index] = None
            self._slot_pending_since[index] = None
            confirmed.append(index)
            self._slot_ref_image[index] = image.copy()
            readings.append(CardReading(
                index=index + 1,
                raw_text=raw,
                normalized=normalize_text(raw),
                augment_id=match.augment_id,
                name_cn=match.name_cn,
                confidence=match.confidence,
                method=match.method,
            ))
            hashes.append(digest)
            if same_as_before:
                logger.debug("slot%d 仍是 augmentId=%s（变化只是动画），保持已识别",
                             index, match.augment_id)

        self._slot_hashes = hashes
        self._slot_readings = readings
        offer = OfferSet(readings=readings, pending_slots=pending,
                         confirmed_slots=confirmed)
        logger.debug("三卡识别: %s", offer.describe())
        return offer

    # -- 窗口外的低成本检测（不做 OCR） --------------------------------------
    def reset_slots(self, reason: str = "") -> None:
        """清空逐槽基线 → 下一次 read_once 对三个槽做**全量识别**。

        用于：新一局 / 新的海克斯阶段（要求 C：首次进入必须全量识别）。
        """
        self._slot_hashes = None
        self._slot_readings = None
        self._slot_ref_image = [None, None, None]
        self._slot_failed_hash = [None, None, None]
        self._slot_pending_since = [None, None, None]
        self._slot_sample_hash = [None, None, None]
        self.slot_states = [SlotOcrState.UNCHANGED] * 3
        if reason:
            logger.info("逐槽基线已清空（%s）→ 下一次全量识别", reason)

    def pending_slots(self) -> list[int]:
        """当前处于 CHANGED_PENDING（识别中…）的卡槽下标。"""
        return [index for index, state in enumerate(self.slot_states)
                if state is SlotOcrState.CHANGED_PENDING]

    def pending_age(self, index: int) -> float:
        """该槽进入"等确认"状态多久了（秒）；没在等确认返回 0。"""
        try:
            since = self._slot_pending_since[int(index)]
        except (IndexError, TypeError, ValueError):
            return 0.0
        if since is None:
            return 0.0
        return max(0.0, self.clock() - since)

    def drop_unresolved(self, slots) -> list[int]:
        """把长时间确认不出来的卡槽降级成"未识别"（**终结态**，不是过程态）。

        只在"这个 stage 还没有任何可用快照"时使用；
        否则宁可继续显示上一份稳定快照（用户要求：界面只显示稳定结果）。
        """
        dropped: list[int] = []
        for slot in slots or ():
            try:
                index = int(slot)
            except (TypeError, ValueError):
                continue
            if not 0 <= index < 3 or not self._slot_readings:
                continue
            self._slot_readings[index] = CardReading(
                index=index + 1, raw_text="", normalized="", augment_id=None,
                name_cn=None, confidence=0.0, method="none")
            if self._slot_hashes:
                # 同一帧不再重试；等像素真正变化后再重新识别
                self._slot_hashes[index] = self._slot_sample_hash[index]
            self._slot_failed_hash[index] = self._slot_sample_hash[index]
            self.slot_states[index] = SlotOcrState.UNCHANGED
            self._slot_pending_since[index] = None
            dropped.append(index)
        if dropped:
            logger.info("卡槽长时间无法识别，降级为未识别（内部终结态）: %s", dropped)
        return dropped

    def probe_feature(self) -> BandFeature:
        """只抓名称带算哈希/亮度：不 OCR、不改 slot 状态。

        供 AugmentScanGate 在窗口外（约 1 次/秒）判断"牌面是不是出现了"。
        """
        self.probe_calls += 1
        try:
            roi = self._roi()
            band = roi.capture_bounds()
            frame = self.capture.grab_region(band)
        except Exception as exc:  # noqa: BLE001 - 截图失败不能影响主循环
            logger.debug("低功耗检测截图失败: %s", exc)
            return BandFeature()
        if frame is None:
            return BandFeature()
        digest = _hash_image(frame)
        try:
            mean_brightness = float(frame.mean())
        except Exception:  # noqa: BLE001
            mean_brightness = 0.0
        changed = self._probe_hash is not None and digest != self._probe_hash
        stable = self._probe_hash is not None and digest == self._probe_hash
        self._probe_hash = digest
        return BandFeature(band_hash=digest, mean_brightness=mean_brightness,
                           changed=changed, stable=stable)


    # -- 稳定确认 -----------------------------------------------------------
    def wait_for_stable(self, timeout: float = DEFAULT_TIMEOUT,
                        should_continue=None) -> Optional[OfferSet]:
        """连续 required_stable 次结果一致且三张都匹配，才判定为 CONFIRMED。

        should_continue: 可选回调，返回 False 时提前退出（例如玩家已选完）。
        """
        deadline = time.monotonic() + timeout
        last_signature: Optional[tuple] = None
        repeat = 0
        best: Optional[OfferSet] = None

        while time.monotonic() < deadline:
            if should_continue is not None and not should_continue():
                logger.debug("wait_for_stable 提前退出")
                return None

            offer = self.read_once()
            if offer.complete:
                if offer.signature == last_signature:
                    repeat += 1
                else:
                    last_signature = offer.signature
                    repeat = 1
                offer.stable_count = repeat
                best = offer
                if repeat >= self.required_stable:
                    logger.info("三卡识别 CONFIRMED (连续 %d 次): %s",
                                repeat, offer.describe())
                    return offer
            else:
                last_signature = None
                repeat = 0
                best = offer
            time.sleep(self.interval)

        if best is not None:
            logger.info("三卡识别超时未稳定 (连续 %d 次): %s",
                        repeat, best.describe())
        return None


def _reuse_reading(previous: CardReading) -> CardReading:
    """复用上一次读数（像素未变 / 变化但还在最小间隔内）。"""
    return CardReading(
        index=previous.index,
        raw_text=previous.raw_text,
        normalized=previous.normalized,
        augment_id=previous.augment_id,
        name_cn=previous.name_cn,
        confidence=previous.confidence,
        method=previous.method,
        reused=True,
    )


def _pixel_diff(image, reference) -> Optional[PixelDiff]:
    """算两张卡面 ROI 的差异（平均灰度差 + 强变化像素比例）。

    reference 为空（还没确认过卡面）时返回 None —— 调用方按"必须 OCR"处理。
    """
    if image is None or reference is None:
        return None
    try:
        import numpy as np

        current = np.asarray(image).astype("int16")
        ref = np.asarray(reference).astype("int16")
        if current.shape != ref.shape:
            return None
        diff = np.abs(current - ref)
        if diff.ndim == 3:
            diff = diff.mean(axis=2)
        return PixelDiff(mean=float(diff.mean()),
                         strong=float((diff > CARD_DIFF_STRONG_LEVEL).mean()))
    except Exception as exc:  # noqa: BLE001 - 差异计算失败不能影响识别
        logger.debug("卡面差异计算失败: %s", exc)
        return None


def _slice(frame, band, box):
    """从整条 band 截图中裁出某张卡的名称 ROI（纯内存操作）。"""
    if frame is None:
        return None
    top = box.top - band.top
    left = box.left - band.left
    bottom = top + box.height
    right = left + box.width
    if top < 0 or left < 0:
        return None
    try:
        return frame[top:bottom, left:right]
    except Exception:  # noqa: BLE001
        return None


def _hash_image(image) -> str:
    """ROI 像素哈希：用于判断这个卡槽是否需要重新 OCR。"""
    try:
        return hashlib.blake2b(image.tobytes(), digest_size=8).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["CardReader", "CardReading", "OfferSet", "SlotOcrState", "BandFeature",
           "PixelDiff", "DEFAULT_INTERVAL", "DEFAULT_REQUIRED_STABLE",
           "DEFAULT_TIMEOUT", "MIN_SLOT_OCR_INTERVAL",
           "CARD_CHANGE_IGNORE_MEAN", "CARD_CHANGE_IGNORE_STRONG",
           "CARD_REROLL_MEAN", "CARD_REROLL_STRONG", "CARD_DIFF_STRONG_LEVEL"]
