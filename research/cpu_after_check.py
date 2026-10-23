"""P0-1 + P0-2 修改后的性能实测（§20 A~E）。

    & ".venv\\Scripts\\python.exe" research\\cpu_after_check.py

用真实 RapidOCR（已限到 2/1 线程）+ 真实门控 + 真实 CardReader 跑完整流水线：
    A 普通游戏 30 秒        ：期望 OCR = 0
    B 海克斯首次出现 10 秒   ：允许短时上升，OCR 只在变化槽
    C 海克斯稳定 30 秒       ：期望 OCR = 0
    D 刷新一次               ：只 OCR 变化 slot
    E 游戏结束               ：OCR = 0，也不再抓屏
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from research.cpu_profile_probe import (  # noqa: E402
    CountingOcr,
    CpuMeter,
    FrameCapture,
    load_frame,
    stamp_slot_markers,
)
from src.ocr.card_reader import CardReader  # noqa: E402
from src.ocr.engine import OcrEngine  # noqa: E402
from src.ocr.gate import AugmentScanGate, ScanMode  # noqa: E402
from src.ocr.roi import build_roi_set  # noqa: E402

OUT = PROJECT_ROOT / "research" / "cpu_after_check.txt"
SAMPLE = PROJECT_ROOT / "research" / "captures" / "augment_cards_fixture.png"
LINES: list[str] = []
NCPU = psutil.cpu_count(logical=True) or 1


def log(msg: str = "") -> None:
    print(msg)
    LINES.append(str(msg))


def fmt(meter: CpuMeter, wall: float) -> str:
    return "%.2f 核 / %.2f%%" % (meter.cores, meter.percent)


class Pipeline:
    """复刻 live.py 的扫描分支：WATCHDOG 只探测，FAST 才 read_once。"""

    def __init__(self, capture, engine, gate, scan_interval=0.25,
                 watchdog_interval=1.0) -> None:
        self.capture = capture
        self.ocr = CountingOcr(engine)
        self.reader = CardReader(capture, ocr_engine=self.ocr, interval=scan_interval)
        self.gate = gate
        self.scan_interval = scan_interval
        self.watchdog_interval = watchdog_interval
        self.steps: list[str] = []
        self.read_calls = 0

    def tick(self) -> str:
        mode = self.gate.mode()
        self.steps.append(mode.value)
        if mode is ScanMode.WATCHDOG:
            self.gate.observe(self.reader.probe_feature(), self.gate.clock())
        elif mode is ScanMode.FAST:
            self.read_calls += 1
            self.reader.read_once()
        return mode.value

    def run(self, seconds: float) -> None:
        started = time.perf_counter()
        next_tick = time.perf_counter()
        while time.perf_counter() - started < seconds:
            mode = self.gate.mode()
            interval = (self.scan_interval if mode is ScanMode.FAST
                        else self.watchdog_interval)
            next_tick += interval
            self.tick()
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()


def jitter(frame, band, rng, pixels: int = 60) -> None:
    """模拟游戏画面在动：随机少量像素变化（亮度基本不变）。"""
    region = frame[band.top:band.bottom, band.left:band.right]
    rows = rng.integers(0, region.shape[0], pixels)
    cols = rng.integers(0, region.shape[1], pixels)
    region[rows, cols] = rng.integers(0, 255, (pixels, 3))


def main() -> int:
    log("CPU 优化实测（P0-1 线程 + P0-2 门控）  %s"
        % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("CPU: %d 逻辑核；onnxruntime 线程 = OcrEngine 默认（2/1）"
        % NCPU)

    frame = load_frame()
    roi = build_roi_set(frame.shape[1], frame.shape[0])
    stamp_slot_markers(frame, roi)
    band = roi.capture_bounds()
    card_band = frame[band.top:band.bottom, band.left:band.right].copy()
    rng = np.random.default_rng(20260922)

    engine = OcrEngine()
    warm = frame[roi.title_box(0).top:roi.title_box(0).bottom,
                 roi.title_box(0).left:roi.title_box(0).right]
    engine.recognize_texts(warm, use_det=False)      # 预热
    log("ONNX 真实线程 = %s" % (engine.onnx_threads(),))
    time.sleep(5)                                     # 让首次自旋衰减

    # ---------------- A 普通游戏 30 秒（没有 stage，像素一直在动） ----------------
    clock = time.monotonic
    gate = AugmentScanGate(scan_interval=0.25, watchdog_interval=1.0, clock=clock)
    gate.reset()
    capture = FrameCapture(frame)
    pipeline = Pipeline(capture, engine, gate)
    meter = CpuMeter(); meter.start()
    started = time.perf_counter()
    next_tick = time.perf_counter()
    while time.perf_counter() - started < 30.0:
        next_tick += 1.0                       # WATCHDOG：1 次/秒
        jitter(frame, band, rng)               # 游戏画面在动
        pipeline.tick()
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.perf_counter()
    wall = time.perf_counter() - started
    meter.stop()
    log("")
    log("[A] 普通游戏 30 秒")
    log("    CPU = %s" % fmt(meter, wall))
    log("    OCR calls = %d（%.2f 次/秒）  低频探测次数 = %d"
        % (len(pipeline.ocr.calls), len(pipeline.ocr.calls) / wall,
           pipeline.reader.probe_calls))
    log("    牌面抓取次数 = %d  scan_mode = %s"
        % (pipeline.read_calls, sorted(set(pipeline.steps))))

    # ---------------- B 海克斯首次出现 10 秒（stage 1 武装） ----------------
    gate.reset()
    gate.update_stage(1)                        # level 3 -> stage 1
    pipeline.ocr.reset()
    pipeline.steps.clear()
    reader = pipeline.reader
    reader.ocr_calls = 0
    reader.deferred = 0
    meter = CpuMeter(); meter.start()
    started = time.perf_counter()
    next_tick = time.perf_counter()
    appeared = False
    while time.perf_counter() - started < 10.0:
        next_tick += 0.25                       # FAST：4 次/秒
        if time.perf_counter() - started < 1.5:
            jitter(frame, band, rng)            # 先还是游戏画面
        else:
            # 牌面 UI 出现（真实三卡截图，之后保持静态）
            frame[band.top:band.bottom, band.left:band.right] = card_band
            appeared = True
        pipeline.tick()
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.perf_counter()
    wall = time.perf_counter() - started
    meter.stop()
    log("")
    log("[B] 海克斯首次出现 10 秒（前 1.5s 仍是游戏画面）")
    log("    CPU = %s" % fmt(meter, wall))
    log("    OCR calls = %d（%.2f 次/秒）  单槽次数 = %s"
        % (len(pipeline.ocr.calls), len(pipeline.ocr.calls) / wall,
           pipeline.ocr.per_slot()))
    if pipeline.ocr.latencies:
        lat = sorted(pipeline.ocr.latencies)
        log("    单次识别 平均 %.0f ms / P95 %.0f ms"
            % (1000 * sum(lat) / len(lat), 1000 * lat[int(0.95 * (len(lat) - 1))]))

    # ---------------- C 海克斯稳定 30 秒 ----------------
    pipeline.ocr.reset()
    reader.ocr_calls = 0
    reader.deferred = 0
    before_grabs = capture.grab_calls
    meter = CpuMeter(); meter.start()
    pipeline.run(30.0)
    meter.stop()
    log("")
    log("[C] 海克斯稳定 30 秒")
    log("    CPU = %.2f 核 / %.2f%%" % (meter.cores, meter.percent))
    log("    OCR calls = %d（必须为 0）  抓屏次数 = %d  延迟次数 = %d"
        % (len(pipeline.ocr.calls), capture.grab_calls - before_grabs,
           reader.deferred))

    # ---------------- D 刷新一次：只 OCR 变化 slot ----------------
    slot0 = roi.title_box(0)
    slot1 = roi.title_box(1)
    # 把 slot0 的真实卡面搬到 slot1（等价于玩家刷新了 slot1）
    # 注意：标记像素是"第 1 行第 1 列"，搬运时保留 slot1 自己的标记，
    # 否则统计会把这次 OCR 记到 slot0 名下。
    frame[slot1.top + 2:slot1.bottom, slot1.left + 2:slot1.right] = \
        frame[slot0.top + 2:slot0.bottom, slot0.left + 2:slot0.right]
    frame[slot1.top + 1, slot1.left + 1] = (20, 0, 0)      # slot1 标记（保留）
    pipeline.ocr.reset()
    reader.ocr_calls = 0
    changed_slots: list[int] = []
    before = reader._slot_hashes[:]              # noqa: SLF001 - 验收用
    ticks = 0
    while ticks < 8:                             # 2 秒（0.25s 一轮）
        ticks += 1
        pipeline.tick()
        time.sleep(0.05)
    after = reader._slot_hashes[:]               # noqa: SLF001
    changed_slots = [i for i in range(3) if before[i] != after[i]]
    log("")
    log("[D] 刷新一次（slot1 换成另一个真实卡面）")
    log("    changed_slots = %s" % changed_slots)
    log("    OCR slots = %s   OCR calls = %d"
        % (sorted({s for s in pipeline.ocr.calls if s >= 0}), len(pipeline.ocr.calls)))

    # ---------------- E 游戏结束 ----------------
    gate.note_game_end()
    pipeline.ocr.reset()
    grabs_before = capture.grab_calls
    meter = CpuMeter(); meter.start()
    pipeline.run(5.0)
    meter.stop()
    log("")
    log("[E] 游戏结束 5 秒")
    log("    CPU = %.2f 核 / %.2f%%   OCR calls = %d   抓屏次数 = %d"
        % (meter.cores, meter.percent, len(pipeline.ocr.calls),
           capture.grab_calls - grabs_before))

    log("")
    log("=" * 70)
    log("对照（修改前）：普通游戏 20.73 核 / 86.4%；海克斯稳定 首轮 4.54 核后 0.00 核；"
        "OCR 9.9~10.5 次/秒；ONNX 线程 24x3、进程 99 线程")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    log("")
    log("写入: %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
