"""稳态 CPU 分桶测量：确认「稳定牌面」时 CPU 到底是持续高还是只有首轮尖峰。

    & ".venv\\Scripts\\python.exe" research\\cpu_steady_test.py
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
    FrameCapture,
    load_frame,
    stamp_slot_markers,
)
from src.ocr.card_reader import CardReader  # noqa: E402
from src.ocr.engine import OcrEngine  # noqa: E402
from src.ocr.roi import build_roi_set  # noqa: E402

OUT = PROJECT_ROOT / "research" / "cpu_steady_test.txt"
PROC = psutil.Process()
NCPU = psutil.cpu_count(logical=True) or 1
LINES: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    LINES.append(str(msg))


def bucket_cpu(seconds: float):
    t0 = PROC.cpu_times()
    w0 = time.perf_counter()
    yield None  # 占位，调用方在 with 外自己 sleep
    t1 = PROC.cpu_times()
    return ((t1.user + t1.system) - (t0.user + t0.system)) / (time.perf_counter() - w0)


def run(label: str, reader: CardReader, ocr: CountingOcr, seconds: float,
        interval: float = 0.25, bucket: float = 5.0) -> None:
    ocr.reset()
    started = time.perf_counter()
    log("[%s] 按 %.2fs 节奏跑 %.0fs（成绩按 %.0fs 分桶）"
        % (label, interval, seconds, bucket))
    while time.perf_counter() - started < seconds:
        b_start = time.perf_counter()
        t0 = PROC.cpu_times()
        ocr_before = len(ocr.calls)
        next_tick = b_start
        while time.perf_counter() - b_start < bucket and \
                time.perf_counter() - started < seconds:
            next_tick += interval
            reader.read_once()
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()
        t1 = PROC.cpu_times()
        wall = time.perf_counter() - b_start
        cores = ((t1.user + t1.system) - (t0.user + t0.system)) / wall
        log("    第 %4.0f~%4.0f 秒: %.2f 核 / %5.1f%%  本桶 OCR=%d"
            % (b_start - started, time.perf_counter() - started,
               cores, cores / NCPU * 100, len(ocr.calls) - ocr_before))


def main() -> int:
    log("稳态 CPU 分桶测量   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("CPU %d 核（逻辑=%s 物理=%s）"
        % (NCPU, psutil.cpu_count(), psutil.cpu_count(logical=False)))
    log("")

    frame = load_frame()
    roi = build_roi_set(frame.shape[1], frame.shape[0])
    stamp_slot_markers(frame, roi)
    engine = OcrEngine()
    warm = frame[roi.title_box(0).top:roi.title_box(0).bottom,
                 roi.title_box(0).left:roi.title_box(0).right]
    engine.recognize_texts(warm, use_det=False)      # 预热（排除首次加载成本）
    time.sleep(6)                                     # 让线程池自旋衰减

    ocr = CountingOcr(engine)
    capture = FrameCapture(frame)
    log("A) 牌面稳定（像素完全不变，numpy 假屏幕，不含真实抓屏）")
    run("稳定", CardReader(capture, ocr_engine=ocr, interval=0.25), ocr, 30.0)
    log("    合计 OCR 次数 = %d" % len(ocr.calls))

    log("")
    log("B) 牌面稳定 + 真实 mss 抓屏（每轮真实截取名称带）")
    import mss

    class RealBandCapture:
        def __init__(self, roi, sct, monitor):
            self.roi = roi
            self.sct = sct
            self.monitor = monitor
            self.grabs = 0

        def build_roi_set(self):
            return self.roi

        def grab_region(self, box):
            self.grabs += 1
            return np.array(self.sct.grab({
                "left": self.monitor["left"] + box.left,
                "top": self.monitor["top"] + box.top,
                "width": box.width, "height": box.height}))[:, :, :3]

        def close(self):
            pass

    with mss.mss() as sct:
        real = RealBandCapture(roi, sct, sct.monitors[1])
        ocr.reset()
        run("稳定+真实抓屏", CardReader(real, ocr_engine=ocr, interval=0.25), ocr, 15.0)
        log("    合计 OCR 次数 = %d（当前屏幕若有动画元素会触发）" % len(ocr.calls))

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    log("")
    log("写入: %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
