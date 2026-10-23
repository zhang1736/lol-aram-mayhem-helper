"""海克斯识别链路基准（真实 RapidOCR + 脱敏真实牌面）。

这个脚本只做性能测量，不进入正式运行路径。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.augment_repository import AugmentRepository
from src.ocr.card_reader import CardReader
from src.ocr.engine import OcrEngine
from src.ocr.matcher import AugmentMatcher
from tests.replay_harness import FakeClock, ReplayScreen


SCREENSHOT = ROOT / "research" / "captures" / "augment_cards_fixture.png"


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio)))
    return ordered[index]


class RealReplayScreen(ReplayScreen):
    """真实截图回放，同时记录截图与 ROI 裁剪的墙钟耗时。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.grab_ms: list[float] = []

    def grab_region(self, box):
        started = time.perf_counter()
        frame = super().grab_region(box)
        self.grab_ms.append((time.perf_counter() - started) * 1000.0)
        return frame


def run(iterations: int = 5) -> None:
    from PIL import Image  # noqa: F401 - 明确检查测试依赖

    if not SCREENSHOT.exists():
        raise SystemExit("缺少真实牌面样本: %s" % SCREENSHOT)

    init_started = time.perf_counter()
    engine = OcrEngine()
    engine.ensure_ready()
    init_ms = (time.perf_counter() - init_started) * 1000.0

    first_times: list[float] = []
    static_times: list[float] = []
    reroll_times: list[float] = []
    reroll_ocr: list[float] = []
    grab_times: list[float] = []

    for iteration in range(iterations):
        screen = RealReplayScreen(SCREENSHOT)
        clock = FakeClock(100.0)
        reader = CardReader(
            screen,
            engine,
            AugmentMatcher(AugmentRepository().load()),
            interval=0.0,
            required_stable=2,
            clock=clock,
        )

        started = time.perf_counter()
        first = reader.read_once()
        first_times.append((time.perf_counter() - started) * 1000.0)
        if not first.complete:
            raise SystemExit("第 %d 轮首次识别不完整: %s"
                             % (iteration + 1, first.describe()))

        started = time.perf_counter()
        static = reader.read_once()
        static_times.append((time.perf_counter() - started) * 1000.0)
        if not all(reading.reused for reading in static.readings):
            raise SystemExit("静态牌面仍触发 OCR")

        screen.reroll(dst=1, src=0)
        clock.advance(0.25)
        pending = reader.read_once()
        if 1 not in pending.pending_slots:
            raise SystemExit("换牌第一帧未进入待确认状态")

        # 第二个 0.25s 采样仍得到同一张新卡 → 真实换牌已稳定，允许 OCR。
        clock.advance(0.25)
        started = time.perf_counter()
        changed = reader.read_once()
        reroll_times.append((time.perf_counter() - started) * 1000.0)
        if changed.readings[1].reused:
            raise SystemExit("0.75 秒节流后仍未重新识别")
        reroll_ocr.append((time.perf_counter() - started) * 1000.0)
        grab_times.extend(screen.grab_ms)

    print("样本: %s" % SCREENSHOT)
    print("OCR 初始化: %.1f ms" % init_ms)
    print("首次三卡 total: P50=%.1f ms P95=%.1f ms max=%.1f ms"
          % (statistics.median(first_times), _percentile(first_times, 0.95),
             max(first_times)))
    print("静态三卡复用 total: P50=%.1f ms P95=%.1f ms max=%.1f ms"
          % (statistics.median(static_times), _percentile(static_times, 0.95),
             max(static_times)))
    print("单槽重随（新帧确认后的识别轮）: P50=%.1f ms P95=%.1f ms max=%.1f ms"
          % (statistics.median(reroll_times), _percentile(reroll_times, 0.95),
             max(reroll_times)))
    print("名称带截图: P50=%.2f ms P95=%.2f ms max=%.2f ms"
          % (statistics.median(grab_times), _percentile(grab_times, 0.95),
             max(grab_times)))
    print("换牌调度等待（两帧 x 0.25s 扫描间隔）: 250 ms")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    run(max(1, args.iterations))


if __name__ == "__main__":
    main()
