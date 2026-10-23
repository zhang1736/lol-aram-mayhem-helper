"""线程安全的状态中枢 + 性能统计。

GUI 线程轮询 snapshot()，引擎线程写入 update()，互不阻塞。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

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
    samples: int = 0

    def describe(self) -> str:
        return ("CPU: %.1f%% Memory: %.1f MB OCR: %.0f ms OP.GG: %.0f ms Overlay: %.1f ms"
                % (self.cpu_percent, self.memory_mb, self.ocr_ms,
                   self.opgg_ms, self.overlay_ms))


class PerfMonitor:
    """采样本进程的 CPU / 内存，并记录各环节耗时。"""

    def __init__(self, interval: float = 5.0) -> None:
        self.interval = interval
        self.stats = PerfStats()
        self._process = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
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
    updated_at: float = 0.0


class StateHub:
    """引擎线程写、GUI 线程读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = AppSnapshot()
        self.perf = PerfMonitor()

    def update(self, **fields: Any) -> AppSnapshot:
        with self._lock:
            for key, value in fields.items():
                if hasattr(self._snapshot, key):
                    setattr(self._snapshot, key, value)
            self._snapshot.perf = self.perf.stats
            self._snapshot.updated_at = time.time()
            return self._snapshot

    def snapshot(self) -> AppSnapshot:
        with self._lock:
            self._snapshot.perf = self.perf.stats
            return AppSnapshot(**{
                field_name: getattr(self._snapshot, field_name)
                for field_name in self._snapshot.__dataclass_fields__
            })


#: 全局单例（引擎与 GUI 共用）
HUB = StateHub()


__all__ = ["AppSnapshot", "PerfStats", "PerfMonitor", "StateHub", "HUB"]
