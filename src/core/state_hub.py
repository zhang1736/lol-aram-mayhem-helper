"""线程安全的状态中枢 + 性能统计。

GUI 线程轮询 snapshot()，引擎线程写入 update()，互不阻塞。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.utils.logger import get_logger, get_tagged

logger = get_logger(__name__)
perf_log = get_tagged("flow", "perf")


@dataclass
class PerfStats:
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    ocr_ms: float = 0.0
    opgg_ms: float = 0.0
    overlay_ms: float = 0.0
    screenshot_ms: float = 0.0
    offer_detect_ms: float = 0.0
    stabilize_ms: float = 0.0
    stats_lookup_ms: float = 0.0
    recommend_ms: float = 0.0
    overlay_update_ms: float = 0.0
    total_ms: float = 0.0
    overlay_visible_ms: float = 0.0
    samples: int = 0

    def describe(self) -> str:
        return ("CPU: %.1f%% Memory: %.1f MB OCR: %.0f ms OP.GG: %.0f ms "
                "Overlay: %.1f ms Total: %.0f ms"
                % (self.cpu_percent, self.memory_mb, self.ocr_ms,
                   self.opgg_ms, self.overlay_ms, self.total_ms))


@dataclass
class PerfTrace:
    """一次"有效牌面 -> 浮层发布"的毫秒级分段记录。"""

    trace_id: int = 0
    session_id: int = 0
    stage: Optional[int] = None
    augment_round: int = 0
    offer_version: int = 0
    trigger: str = ""
    offer_detect_ms: float = 0.0
    screenshot_ms: float = 0.0
    ocr_ms: float = 0.0
    stabilize_ms: float = 0.0
    stats_lookup_ms: float = 0.0
    stats_source: str = "none"
    recommend_ms: float = 0.0
    overlay_update_ms: float = 0.0
    overlay_visible_ms: float = 0.0
    total_ms: float = 0.0
    ocr_calls: int = 0
    slot_ocr_calls: list[int] = field(default_factory=lambda: [0, 0, 0])
    started_at_perf: float = 0.0
    recorded_at: float = 0.0

    def describe(self) -> str:
        """固定日志格式；可由日志工具直接采集。"""
        return (
            "PERF trace=%s session=%s stage=%s round=%s offer_version=%s trigger=%s "
            "offer_detect_ms=%.1f screenshot_ms=%.1f ocr_ms=%.1f "
            "stabilize_ms=%.1f stats_lookup_ms=%.2f source=%s "
            "recommend_ms=%.2f overlay_update_ms=%.2f total_ms=%.1f "
            "ocr_calls=%s slots=%s"
            % (self.trace_id, self.session_id, self.stage, self.augment_round,
               self.offer_version, self.trigger, self.offer_detect_ms,
               self.screenshot_ms,
               self.ocr_ms, self.stabilize_ms, self.stats_lookup_ms,
               self.stats_source, self.recommend_ms, self.overlay_update_ms,
               self.total_ms, self.ocr_calls, self.slot_ocr_calls)
        )


@dataclass(frozen=True)
class LatencySummary:
    count: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0

    def describe(self) -> str:
        return ("n=%d P50=%.1f ms P95=%.1f ms max=%.1f ms"
                % (self.count, self.p50_ms, self.p95_ms, self.max_ms))


class PerfMonitor:
    """采样本进程的 CPU / 内存，并记录各环节耗时。"""

    def __init__(self, interval: float = 5.0) -> None:
        self.interval = interval
        self.stats = PerfStats()
        self._process = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._trace_seq = 0
        self._trace_history: deque[PerfTrace] = deque(maxlen=500)
        try:
            import psutil

            self._psutil = psutil
            self._process = psutil.Process()
            self._process.cpu_percent(None)      # 预热，第一次调用返回 0
        except Exception as exc:  # noqa: BLE001 - 没有 psutil 也不能影响主流程
            self._psutil = None
            logger.debug("psutil 不可用，性能统计关闭: %s", exc)

    # -- 采样 ---------------------------------------------------------------
    def start(self) -> None:
        if self._process is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="perf", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001
                logger.debug("性能采样失败: %s", exc)

    def sample(self) -> PerfStats:
        if self._process is not None:
            try:
                self.stats.cpu_percent = float(self._process.cpu_percent(None))
                self.stats.memory_mb = self._process.memory_info().rss / (1024 * 1024)
                self.stats.samples += 1
            except Exception:  # noqa: BLE001
                pass
        return self.stats

    # -- 各环节耗时 ---------------------------------------------------------
    def record(self, name: str, seconds: float) -> None:
        value = seconds * 1000.0
        if name == "ocr":
            self.stats.ocr_ms = value
        elif name == "opgg":
            self.stats.opgg_ms = value
        elif name == "overlay":
            self.stats.overlay_ms = value

    def new_trace(self, *, session_id: int = 0, stage: Optional[int] = None,
                  augment_round: int = 0, offer_version: int = 0,
                  trigger: str = "",
                  started_at_perf: Optional[float] = None) -> PerfTrace:
        """创建一条待填充的性能记录。"""
        self._trace_seq += 1
        return PerfTrace(
            trace_id=self._trace_seq,
            session_id=session_id,
            stage=stage,
            augment_round=augment_round,
            offer_version=offer_version,
            trigger=trigger,
            started_at_perf=(time.perf_counter()
                             if started_at_perf is None else started_at_perf),
        )

    def record_trace(self, trace: PerfTrace) -> PerfTrace:
        """保存并更新最新分段指标；同一 trace 可多次补写。"""
        trace.recorded_at = time.perf_counter()
        if not any(item is trace for item in self._trace_history):
            self._trace_history.append(trace)
        self.stats.screenshot_ms = trace.screenshot_ms
        self.stats.offer_detect_ms = trace.offer_detect_ms
        self.stats.ocr_ms = trace.ocr_ms
        self.stats.stabilize_ms = trace.stabilize_ms
        self.stats.stats_lookup_ms = trace.stats_lookup_ms
        self.stats.opgg_ms = trace.stats_lookup_ms
        self.stats.recommend_ms = trace.recommend_ms
        self.stats.overlay_update_ms = trace.overlay_update_ms
        self.stats.overlay_ms = trace.overlay_update_ms
        self.stats.total_ms = trace.total_ms
        if trace.overlay_visible_ms > 0:
            self.stats.overlay_visible_ms = trace.overlay_visible_ms
        return trace

    def mark_overlay_visible(self, trace_id: int,
                             visible_at_perf: Optional[float] = None) -> float:
        """记录主线程真正应用浮层内容时的端到端耗时。"""
        moment = time.perf_counter() if visible_at_perf is None else visible_at_perf
        for trace in reversed(self._trace_history):
            if trace.trace_id != trace_id:
                continue
            trace.overlay_visible_ms = max(
                0.0, (moment - trace.started_at_perf) * 1000.0)
            self.stats.overlay_visible_ms = trace.overlay_visible_ms
            return trace.overlay_visible_ms
        return 0.0

    def latency_summary(self, trigger: Optional[str] = None) -> LatencySummary:
        """按触发类型汇总 total_ms；用于开发基准与 Debug 页面。"""
        values = [trace.total_ms for trace in self._trace_history
                  if trace.total_ms > 0
                  and (trigger is None or trace.trigger == trigger)]
        if not values:
            return LatencySummary()
        ordered = sorted(values)
        p50 = ordered[int(round((len(ordered) - 1) * 0.50))]
        p95 = ordered[int(round((len(ordered) - 1) * 0.95))]
        return LatencySummary(len(ordered), p50, p95, ordered[-1])

    def log(self) -> None:
        perf_log.debug("%s", self.stats.describe())


@dataclass
class AppSnapshot:
    """界面/诊断需要的一切（只读快照）。"""

    client_connected: bool = False
    lcu_port: Optional[int] = None
    lcu_pid: Optional[int] = None
    region: str = ""
    locale: str = ""
    gameflow: str = "-"

    champion_id: Optional[int] = None
    champion_alias: Optional[str] = None
    champion_name: Optional[str] = None
    #: ChampSelect 阶段的动态候选英雄（玩家换英雄时会变）
    candidate_champion_id: Optional[int] = None
    candidate_champion_alias: Optional[str] = None
    candidate_champion_name: Optional[str] = None
    #: 进入对局后由 Live Client API 确定的最终英雄（权威）
    final_champion_id: Optional[int] = None
    final_champion_alias: Optional[str] = None
    final_champion_name: Optional[str] = None
    level: Optional[int] = None
    stage: Optional[int] = None
    augment_round: int = 0

    offers: list = field(default_factory=list)
    offer_version: int = 0
    changed_slots: list = field(default_factory=list)
    rerolled_slots: list = field(default_factory=list)
    recommended: list = field(default_factory=list)

    ocr_texts: list = field(default_factory=list)
    ocr_latency_ms: float = 0.0
    #: 当前扫描模式 FAST / WATCHDOG / STOPPED（P0-2 门控）
    scan_mode: str = "-"
    #: 本局累计真实 OCR 次数（性能验收用）
    ocr_calls: int = 0

    cache_status: str = "MISSING"
    cache_fetched_at: str = "-"
    data_patch: Optional[str] = None

    overlay_enabled: bool = True
    overlay_visible: bool = False

    hero_scope: str = "Korea / Iron–Gold"
    augment_scope: str = "OP.GG Aggregate"
    data_region: str = "kr"
    data_tier: str = "ibsg"

    #: 按钮/操作反馈（主窗口底部的绿色提示）
    status_message: str = ""

    perf: PerfStats = field(default_factory=PerfStats)
    perf_trace: Optional[PerfTrace] = None
    offers_updated_at: float = 0.0
    updated_at: float = 0.0


class StateHub:
    """引擎线程写、GUI 线程读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = AppSnapshot()
        self.perf = PerfMonitor()
        self._listeners: dict[int, Callable[[set[str]], None]] = {}
        self._listener_seq = 0

    def subscribe(self, listener: Callable[[set[str]], None]) -> int:
        """订阅字段变化；回调可能来自引擎线程，Qt 调用方必须转到主线程。"""
        with self._lock:
            self._listener_seq += 1
            token = self._listener_seq
            self._listeners[token] = listener
            return token

    def unsubscribe(self, token: Optional[int]) -> None:
        if token is None:
            return
        with self._lock:
            self._listeners.pop(token, None)

    def update(self, **fields: Any) -> AppSnapshot:
        with self._lock:
            changed: set[str] = set()
            for key, value in fields.items():
                if hasattr(self._snapshot, key):
                    setattr(self._snapshot, key, value)
                    changed.add(key)
            self._snapshot.perf = self.perf.stats
            self._snapshot.updated_at = time.time()
            snapshot = self._snapshot
            listeners = tuple(self._listeners.values())
        # 回调必须在锁外执行；回调内部可以安全地再次读取 snapshot()。
        for listener in listeners:
            try:
                listener(changed)
            except Exception as exc:  # noqa: BLE001 - 订阅者不能影响状态发布
                logger.debug("状态订阅回调失败: %s", exc)
        return snapshot

    def snapshot(self) -> AppSnapshot:
        with self._lock:
            self._snapshot.perf = self.perf.stats
            return AppSnapshot(**{
                field_name: getattr(self._snapshot, field_name)
                for field_name in self._snapshot.__dataclass_fields__
            })


#: 全局单例（引擎与 GUI 共用）
HUB = StateHub()


__all__ = ["AppSnapshot", "PerfStats", "PerfTrace", "LatencySummary",
           "PerfMonitor", "StateHub", "HUB"]
