"""海克斯扫描门控：决定"现在该不该抓屏 / 该不该 OCR"。

实测背景（2026-09-22，research/CPU_PROFILE_REPORT.md）：
    旧逻辑在对局中每 0.25s 无条件抓屏，只要某个 slot 的像素变了就 OCR。
    而这条 1410x58 名称带正好压实时游戏画面上，像素每帧都在动，
    于是 3 槽 x 3.3 轮/秒 ≈ 10 次 OCR/秒 → 20.7 核 / 86.4% CPU。

现在分三层：
    FAST      高频扫描（0.20~0.25s）：只有当"该看牌面"时才进入
              —— stage 窗口内，或牌面已经在屏幕上（此时 OCR 仍只在像素变化时发生）
    WATCHDOG  低频检测（1 次/秒）：只抓名称带算廉价特征（哈希 + 亮度），**不 OCR**
    STOPPED   对局结束：不抓屏、不 OCR

进入 FAST 的三种方式（都不会错过海克斯界面）：
    1. 等级到达某个 stage（由 stage_from_level 得到，不是写死 level）
    2. WATCHDOG 发现"画面突然静止"或"亮度明显跳变"（牌面 UI 出现的典型特征）
    3. SAFETY_REARM_SECONDS 保底重开（即使 1、2 都没触发，也不会永久错过）
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from src.utils.logger import get_tagged

logger = get_tagged("ocr", "ocr.gate")

#: 进入 stage 后的高频扫描窗口长度（§6）
AUGMENT_SCAN_TIMEOUT = 10.0
#: 窗口外的低频检测间隔（§12）
WATCHDOG_INTERVAL = 1.0
#: 画面静止多久 → 认为"可能是牌面 UI"（牌面是静态覆盖层）
STABLE_ARM_SECONDS = 0.9
#: 相对近期平均亮度跳变多少 → 认为"牌面 UI 出现"（0~255）
BRIGHTNESS_JUMP = 25.0
#: 两次重新武装之间的最小间隔，防止抖动
REARM_COOLDOWN = 2.0
#: 保底重新武装周期：stage 已到但一直没看到牌面时，最多这么久一定再看一次
SAFETY_REARM_SECONDS = 20.0


class ScanMode(str, Enum):
    FAST = "FAST"           # 高频扫描（OCR 只在像素变化时发生）
    WATCHDOG = "WATCHDOG"   # 低频特征检测，不 OCR
    STOPPED = "STOPPED"     # 对局结束，什么都不做


@dataclass(frozen=True)
class BandFeature:
    """窗口外的廉价特征（由 CardReader.probe_feature 产出）。"""

    band_hash: str = ""
    mean_brightness: float = 0.0
    changed: bool = False       # 与上一次探测比，像素变了
    stable: bool = False        # 与上一次探测比，像素完全没变


class AugmentScanGate:
    """stage 窗口 + 牌面跟踪 + 静默检测的调度器（纯逻辑，可注入时钟）。"""

    def __init__(self, scan_interval: float = 0.25,
                 window_seconds: float = AUGMENT_SCAN_TIMEOUT,
                 watchdog_interval: float = WATCHDOG_INTERVAL,
                 stable_arm_seconds: float = STABLE_ARM_SECONDS,
                 brightness_jump: float = BRIGHTNESS_JUMP,
                 rearm_cooldown: float = REARM_COOLDOWN,
                 safety_rearm_seconds: float = SAFETY_REARM_SECONDS,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.scan_interval = float(scan_interval)
        self.window_seconds = float(window_seconds)
        self.watchdog_interval = float(watchdog_interval)
        self.stable_arm_seconds = float(stable_arm_seconds)
        self.brightness_jump = float(brightness_jump)
        self.rearm_cooldown = float(rearm_cooldown)
        self.safety_rearm_seconds = float(safety_rearm_seconds)
        self.clock = clock or time.monotonic

        #: 当前 stage（来自 Live Client 等级，不是写死的 level）
        self.stage: Optional[int] = None
        #: 已经武装过高频窗口的 stage
        self.armed_stage: Optional[int] = None
        #: 高频窗口是否开启 / 截止时间
        self.scan_window_active: bool = False
        self.window_deadline: Optional[float] = None
        #: 该 stage 是否已经确认过稳定三卡
        self.stage_completed: bool = False
        #: 牌面是否正显示在屏幕上
        self.offers_visible: bool = False
        #: 对局是否结束
        self.stopped: bool = False

        self.rearms = 0
        self.history: list[tuple[float, str]] = []

        self._stable_since: Optional[float] = None
        self._brightness: deque[float] = deque(maxlen=5)
        self._last_arm_at: float = float("-inf")

    # -- 状态查询 -----------------------------------------------------------
    @property
    def scanning(self) -> bool:
        return self.mode() is ScanMode.FAST

    def mode(self, now: Optional[float] = None) -> ScanMode:
        """当前应该用哪种扫描模式。"""
        if self.stopped:
            return ScanMode.STOPPED
        if self.offers_visible:
            return ScanMode.FAST          # 牌面在屏幕上：只跟踪像素变化
        moment = self.clock() if now is None else now
        if self.scan_window_active:
            if self.window_deadline is None or moment <= self.window_deadline:
                return ScanMode.FAST
            self.close_window(moment, reason="TIMEOUT")
        # stage 已到但一直没看到牌面 → 保底重开
        if (self.safety_rearm_seconds > 0 and self.stage is not None
                and not self.stage_completed
                and moment - self._last_arm_at >= self.safety_rearm_seconds):
            self.arm(moment, reason="SAFETY_REARM")
            return ScanMode.FAST
        return ScanMode.WATCHDOG

    def interval(self, mode: Optional[ScanMode] = None) -> float:
        """该模式下的轮询间隔。"""
        current = mode or self.mode()
        if current is ScanMode.FAST:
            return self.scan_interval
        return self.watchdog_interval

    # -- stage -------------------------------------------------------------
    def update_stage(self, stage: Optional[int], now: Optional[float] = None) -> bool:
        """stage 变化 → 武装新的高频窗口（每个 stage 只初次武装一次）。"""
        if stage is None or stage == self.stage:
            return False
        self.stage = stage
        self.stage_completed = False
        self.arm(now, reason="STAGE_ARMED")
        return True

    def arm(self, now: Optional[float] = None, reason: str = "ARMED") -> None:
        moment = self.clock() if now is None else now
        self.scan_window_active = True
        self.window_deadline = moment + self.window_seconds
        self.armed_stage = self.stage
        self._stable_since = None
        self._last_arm_at = moment
        self.rearms += 1
        self.history.append((moment, reason))
        logger.info("开启高频扫描窗口 stage=%s (%.0fs, reason=%s)",
                    self.stage, self.window_seconds, reason)

    def close_window(self, now: Optional[float] = None, reason: str = "CLOSE") -> None:
        if self.scan_window_active:
            logger.info("关闭高频扫描窗口 stage=%s（reason=%s, 已确认牌面=%s）",
                        self.stage, reason, self.stage_completed)
        self.scan_window_active = False
        self.window_deadline = None
        self._stable_since = None

    # -- 牌面 ---------------------------------------------------------------
    def note_offers(self, visible: bool, now: Optional[float] = None) -> None:
        """牌面出现 / 消失。"""
        moment = self.clock() if now is None else now
        if visible:
            self.offers_visible = True
            self.stage_completed = True
            return
        self.offers_visible = False
        # 牌面收走（选完 / 界面消失）→ 本 stage 结束，回到低频 watchdog
        if self.stage_completed:
            self.close_window(moment, reason="OFFERS_GONE")

    def note_game_end(self) -> None:
        """对局结束：彻底停止抓屏与 OCR。"""
        self.stopped = True
        self.offers_visible = False
        self.stage_completed = True
        self.close_window(reason="GAME_END")

    def reset(self) -> None:
        """新一局开始。"""
        self.stage = None
        self.armed_stage = None
        self.scan_window_active = False
        self.window_deadline = None
        self.stage_completed = False
        self.offers_visible = False
        self.stopped = False
        self._stable_since = None
        self._brightness.clear()
        self._last_arm_at = float("-inf")

    # -- 低频特征检测（窗口外） ----------------------------------------------
    def observe(self, feature: Optional[BandFeature],
                now: Optional[float] = None) -> bool:
        """窗口外的廉价检测：返回 True 表示重新进入高频扫描。"""
        if feature is None or self.stopped or self.offers_visible:
            return False
        moment = self.clock() if now is None else now
        reason: Optional[str] = None

        if feature.stable:
            if self._stable_since is None:
                self._stable_since = moment
            elif moment - self._stable_since >= self.stable_arm_seconds:
                reason = "SCREEN_STABLE"
        else:
            self._stable_since = None

        if reason is None and len(self._brightness) >= 3:
            recent = sum(self._brightness) / len(self._brightness)
            if feature.mean_brightness - recent >= self.brightness_jump:
                reason = "BRIGHTNESS_JUMP"
        self._brightness.append(feature.mean_brightness)

        if reason is None:
            return False
        if moment - self._last_arm_at < self.rearm_cooldown:
            logger.debug("静默/亮度特征触发但处于冷却期（%.1fs），忽略", self.rearm_cooldown)
            return False
        self.arm(moment, reason=reason)
        return True


__all__ = [
    "ScanMode", "BandFeature", "AugmentScanGate",
    "AUGMENT_SCAN_TIMEOUT", "WATCHDOG_INTERVAL", "STABLE_ARM_SECONDS",
    "BRIGHTNESS_JUMP", "REARM_COOLDOWN", "SAFETY_REARM_SECONDS",
]
