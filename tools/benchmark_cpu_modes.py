"""测量稳定牌面与窗口外 watchdog 的 CPU（单核等效百分比）。"""

from __future__ import annotations

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
from tests.replay_harness import ReplayScreen

SCREENSHOT = ROOT / "research" / "captures" / "augment_cards_fixture.png"


def _cpu_percent(work, seconds: float) -> tuple[float, int]:
    started = time.perf_counter()
    cpu_started = time.process_time()
    count = 0
    while time.perf_counter() - started < seconds:
        work()
        count += 1
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu_started
    return cpu / elapsed * 100.0, count


def main() -> None:
    engine = OcrEngine()
    engine.ensure_ready()
    screen = ReplayScreen(SCREENSHOT)
    reader = CardReader(
        screen, engine, AugmentMatcher(AugmentRepository().load()),
        interval=0.25, min_slot_ocr_interval=0.20)
    first = reader.read_once()
    if not first.complete:
        raise SystemExit("真实截图首次识别失败")
    # ONNX Runtime 模型初始化后线程池会短暂收尾；正式程序在开局前预热，
    # 这里等收尾结束，避免把一次性的模型初始化算进稳定扫描 CPU。
    time.sleep(2.2)

    def stable_scan() -> None:
        reader.read_once()
        time.sleep(0.25)

    def watchdog_probe() -> None:
        reader.probe_feature()
        time.sleep(0.35)

    stable_cpu, stable_rounds = _cpu_percent(stable_scan, 5.0)
    watchdog_cpu, watchdog_rounds = _cpu_percent(watchdog_probe, 5.0)
    print("稳定牌面扫描: %.2f%% 单核等效（%d 轮，OCR calls=%d）"
          % (stable_cpu, stable_rounds, reader.ocr_calls - 3))
    print("窗口外 watchdog: %.2f%% 单核等效（%d 轮，无 OCR）"
          % (watchdog_cpu, watchdog_rounds))


if __name__ == "__main__":
    main()
