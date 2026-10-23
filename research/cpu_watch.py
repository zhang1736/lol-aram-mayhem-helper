"""真实对局中的 OCR / CPU 观测（只读屏幕，不干预游戏）。

用法（**在英雄联盟对局中**运行）：

    & ".venv\\Scripts\\python.exe" research\\cpu_watch.py --seconds 60

它会：
  * 用真实 mss 抓当前屏幕的「三卡名称带」（和正式版完全相同的 ROI）
  * 统计 10 秒 / N 秒内：每个 slot 的像素变化次数、哈希变化率
  * 对变化过的 slot 跑真实 RapidOCR，统计 OCR 次数/秒与延迟
  * 同时采样正在运行的正式版 LOL海克斯助手.exe 的 CPU

输出 research/cpu_watch.txt —— 用来回答：
    "普通对局里 ROI 像素到底会不会一直变化？"
    "海克斯界面稳定后像素是否真的不动？"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from src.ocr.engine import OcrEngine  # noqa: E402
from src.ocr.roi import build_roi_set  # noqa: E402

OUT = PROJECT_ROOT / "research" / "cpu_watch.txt"
LINES: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    LINES.append(str(msg))


def digest(image) -> str:
    return hashlib.blake2b(image.tobytes(), digest_size=8).hexdigest()


def find_app_cpu() -> str:
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if "海克斯助手" in name:
            try:
                return "PID %s %s CPU=%.1f%%" % (proc.pid, name, proc.cpu_percent(None))
            except Exception:  # noqa: BLE001
                continue
    return "未检测到正在运行的 LOL海克斯助手.exe"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--ocr", action="store_true",
                        help="对变化的 slot 真的跑 RapidOCR（默认只统计像素变化）")
    args = parser.parse_args()

    import mss

    proc = psutil.Process()
    ncpu = psutil.cpu_count(logical=True) or 1
    engine = OcrEngine() if args.ocr else None
    ocr_calls = 0
    ocr_latencies: list[float] = []

    log("OCR / 像素变化观测   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("时长 %.0fs  轮询 %.2fs  执行OCR=%s" % (args.seconds, args.interval, args.ocr))
    log("")

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        roi = build_roi_set(monitor["width"], monitor["height"])
        band = roi.capture_bounds()
        region = {"left": monitor["left"] + band.left, "top": monitor["top"] + band.top,
                  "width": band.width, "height": band.height}
        log("屏幕 %dx%d  名称带 ROI %dx%d @(%d,%d)"
            % (monitor["width"], monitor["height"], band.width, band.height,
               band.left, band.top))

        last: list[str | None] = [None, None, None]
        changes = [0, 0, 0]
        grabs = 0
        rounds = 0
        started = time.perf_counter()
        cpu_t0 = proc.cpu_times()
        app_line = find_app_cpu()
        next_tick = started

        while time.perf_counter() - started < args.seconds:
            next_tick += args.interval
            shot = np.array(sct.grab(region))[:, :, :3]
            grabs += 1
            rounds += 1
            for index in range(3):
                box = roi.title_box(index)
                top = box.top - band.top
                left = box.left - band.left
                crop = shot[top:top + box.height, left:left + box.width]
                current = digest(crop)
                if last[index] is not None and current != last[index]:
                    changes[index] += 1
                    if engine is not None:
                        t0 = time.perf_counter()
                        engine.recognize_texts(crop, use_det=False)
                        ocr_latencies.append(time.perf_counter() - t0)
                        ocr_calls += 1
                last[index] = current
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

        wall = time.perf_counter() - started
        cpu_t1 = proc.cpu_times()
        cores = ((cpu_t1.user + cpu_t1.system) - (cpu_t0.user + cpu_t0.system)) / wall

    log("")
    log("轮数=%d（%.1f 次/秒）  截图=%d" % (rounds, rounds / wall, grabs))
    log("slot 像素变化次数: slot0=%d  slot1=%d  slot2=%d（%.2f 次/秒/槽）"
        % (changes[0], changes[1], changes[2],
           sum(changes) / wall / 3))
    if engine is not None:
        avg = (sum(ocr_latencies) / len(ocr_latencies) * 1000) if ocr_latencies else 0.0
        log("OCR 调用=%d（%.2f 次/秒）  平均延迟 %.0f ms"
            % (ocr_calls, ocr_calls / wall, avg))
    log("本探针进程: %.2f 核 / %.1f%%" % (cores, cores / ncpu * 100))
    log("正式版进程: %s" % app_line)
    log("")
    log("判读方法：")
    log("  * 普通对局：slot 变化次数应该接近‘轮数’（≈每帧都变）")
    log("  * 海克斯界面稳定后：三个 slot 的变化次数应该都为 0")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    log("")
    log("写入: %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
