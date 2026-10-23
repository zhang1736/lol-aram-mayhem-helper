"""CPU 来源定位探针（**只测量，不改任何业务逻辑**）。

    & ".venv\\Scripts\\python.exe" research\\cpu_profile_probe.py

回答 7 个问题：
    1) OCR 实际调用频率与延迟分布（普通对局 / 海克斯界面 / 海克斯稳定后）
    2) 普通对局阶段是否仍在跑 RapidOCR
    3) 截图成本（全屏 vs 名称带 vs 单卡 ROI）
    4) 稳定牌面下三个 slot 是否重复 OCR
    5) ONNX Runtime 实际线程配置 + 进程真实线程数
    6) CPU 时间占比（OCR / 截图 / hash / OP.GG / Overlay / 其他）
    7) 根因与热点函数

所有数字都是本机实测；场景用真实截图 + 真实 RapidOCR，不伪造数据。
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from src.ocr.card_reader import CardReader  # noqa: E402
from src.ocr.engine import OcrEngine  # noqa: E402
from src.ocr.roi import build_roi_set  # noqa: E402

OUT_FILE = PROJECT_ROOT / "research" / "cpu_profile.txt"
SAMPLE_SCREENSHOT = PROJECT_ROOT / "research" / "captures" / "augment_cards_fixture.png"

_LINES: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    _LINES.append(str(msg))


def head(title: str) -> None:
    log("")
    log("=" * 74)
    log(title)
    log("=" * 74)


# ---------------------------------------------------------------------------
# CPU 采样
# ---------------------------------------------------------------------------

class CpuMeter:
    """用 cpu_times 差值算"占用了几个核"，同时给出占整机百分比。"""

    def __init__(self) -> None:
        self.proc = psutil.Process()
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self._t0 = None
        self._w0 = None
        self.cores = 0.0
        self.percent = 0.0

    def start(self) -> None:
        self._t0 = self.proc.cpu_times()
        self._w0 = time.perf_counter()

    def stop(self) -> tuple[float, float]:
        wall = time.perf_counter() - self._w0
        t1 = self.proc.cpu_times()
        cpu = ((t1.user + t1.system) - (self._t0.user + self._t0.system))
        self.cores = cpu / wall if wall > 0 else 0.0
        self.percent = self.cores / self.ncpu * 100.0
        return self.cores, self.percent


def fmt_latencies(name: str, values: list[float]) -> str:
    if not values:
        return "%s : 无样本" % name
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return ("%s : n=%d  平均 %.1f ms  P50 %.1f  P95 %.1f  最小 %.1f  最大 %.1f"
            % (name, len(values), statistics.mean(ordered) * 1000, 1000 * statistics.median(ordered),
               1000 * p95, 1000 * min(ordered), 1000 * max(ordered)))


# ---------------------------------------------------------------------------
# 假屏幕：从真实截图里取像素（不依赖是否在对局中）
# ---------------------------------------------------------------------------

class FrameCapture:
    """把一张真实截图当成"屏幕"，实现 ScreenCapture 的接口。"""

    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.height, self.width = frame.shape[:2]
        self.grab_calls = 0
        self.grab_seconds: list[float] = []

    def build_roi_set(self):
        return build_roi_set(self.width, self.height)

    def grab_region(self, box):
        self.grab_calls += 1
        t0 = time.perf_counter()
        out = self.frame[box.top:box.bottom, box.left:box.right].copy()
        self.grab_seconds.append(time.perf_counter() - t0)
        return out

    def close(self) -> None:
        pass


class CountingOcr:
    """包装真实 OcrEngine：统计每次识别属于哪个 slot + 记录延迟。"""

    def __init__(self, engine: OcrEngine) -> None:
        self.engine = engine
        self.calls: list[int] = []          # slot 下标（靠 marker 像素识别）
        self.latencies: list[float] = []

    def recognize_texts(self, image, use_det=None):
        # 标记像素写在 TITLE_ROI 的 (top+1, left+1)，切出来的图里就是 [1,1]
        slot = -1
        try:
            slot = int(image[1, 1, 0]) // 10 - 1
        except Exception:  # noqa: BLE001
            slot = -1
        t0 = time.perf_counter()
        texts = self.engine.recognize_texts(image, use_det=use_det)
        self.latencies.append(time.perf_counter() - t0)
        self.calls.append(slot)
        return texts

    def reset(self) -> None:
        self.calls = []
        self.latencies = []

    def per_slot(self) -> dict[int, int]:
        out = {0: 0, 1: 0, 2: 0}
        for slot in self.calls:
            if slot in out:
                out[slot] += 1
        return out


def load_frame() -> np.ndarray:
    import cv2

    # 注意：cv2.imread 在 Windows 下无法处理中文路径，必须自己读字节再解码
    data = np.frombuffer(SAMPLE_SCREENSHOT.read_bytes(), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("无法读取样本截图: %s" % SAMPLE_SCREENSHOT)
    return frame


def stamp_slot_markers(frame: np.ndarray, roi) -> None:
    """在每个 TITLE_ROI 的左上角写一个标记像素（10/20/30）用来区分 slot。"""
    for index in range(3):
        box = roi.title_box(index)
        frame[box.top + 1, box.left + 1] = (10 * (index + 1), 0, 0)


def disturb(frame: np.ndarray, roi, n: int) -> None:
    """模拟"游戏画面在动"：让三个 ROI 的像素都发生变化（触发 hash 变化）。"""
    band = roi.capture_bounds()
    region = frame[band.top:band.bottom, band.left:band.right]
    region[:, 25::50, :] = (n * 13) % 256


# ---------------------------------------------------------------------------
# 1) 环境 / ONNX 会话配置
# ---------------------------------------------------------------------------

def section_env(engine: OcrEngine, ocr: CountingOcr) -> None:
    head("1) 环境与 ONNX Runtime 真实配置")
    import onnxruntime

    log("CPU 逻辑/物理核 : %s / %s" % (psutil.cpu_count(), psutil.cpu_count(logical=False)))
    log("onnxruntime     : %s" % onnxruntime.__version__)
    log("rapidocr backend: %s" % engine.backend)
    log("进程线程数(初始化后): %s" % psutil.Process().num_threads())

    rapid = engine._engine  # noqa: SLF001 - 探针需要读真实对象
    for label, module in (("det(检测)", getattr(rapid, "text_det", None)),
                          ("cls(方向)", getattr(rapid, "text_cls", None)),
                          ("rec(识别)", getattr(rapid, "text_rec", None))):
        if module is None:
            log("  %-9s : 未加载" % label)
            continue
        raw = None
        for attr in ("infer", "session"):
            candidate = getattr(module, attr, None)
            raw = getattr(candidate, "session", candidate)
            if raw is not None and hasattr(raw, "get_providers"):
                break
        if raw is None:
            log("  %-9s : 无法读取会话" % label)
            continue
        try:
            so = raw.get_session_options()
        except Exception as exc:  # noqa: BLE001
            log("  %-9s : get_session_options 失败 %s" % (label, exc))
            continue
        log("  %-9s : providers=%s" % (label, raw.get_providers()))
        log("              intra_op_num_threads = %s" % getattr(so, "intra_op_num_threads", "?"))
        log("              inter_op_num_threads = %s" % getattr(so, "inter_op_num_threads", "?"))
        log("              execution_mode       = %s" % getattr(so, "execution_mode", "?"))
        log("              graph_opt_level      = %s" % getattr(so, "graph_optimization_level", "?"))
        log("              enable_cpu_mem_arena = %s" % getattr(so, "enable_cpu_mem_arena", "?"))
    log("")
    log("说明：intra_op_num_threads = 0 表示交给 ONNX Runtime 自己决定")
    log("      （实测会按物理核数开线程池，本机 24 核 -> 单个会话可吃满 24 线程）")


# ---------------------------------------------------------------------------
# 2) 单次 OCR 延迟 + 截图成本
# ---------------------------------------------------------------------------

def section_latency(engine: OcrEngine, roi, frame: np.ndarray) -> None:
    head("2) 单轮 OCR 延迟（真实 ROI 内容）")
    latencies: list[float] = []
    for index in range(3):
        box = roi.title_box(index)
        crop = frame[box.top:box.bottom, box.left:box.right]
        t0 = time.perf_counter()
        engine.recognize_texts(crop, use_det=False)
        latencies.append(time.perf_counter() - t0)
    log(fmt_latencies("首次 3 个 slot", latencies))

    repeats: list[float] = []
    box = roi.title_box(1)
    crop = frame[box.top:box.bottom, box.left:box.right]
    for _ in range(20):
        t0 = time.perf_counter()
        engine.recognize_texts(crop, use_det=False)
        repeats.append(time.perf_counter() - t0)
    log(fmt_latencies("同一 slot 重复 20 次", repeats))
    log("进程线程数(识别后): %s" % psutil.Process().num_threads())
    log("ROI 尺寸: %s（单卡名称行）" % (crop.shape,))

    head("3) 截图成本（mss 真实抓屏）")
    import mss

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        full = {"left": monitor["left"], "top": monitor["top"],
                "width": monitor["width"], "height": monitor["height"]}
        band = roi.capture_bounds()
        single = roi.title_box(0)
        regions = {
            "全屏 %dx%d" % (full["width"], full["height"]): dict(full),
            "名称带 %dx%d（当前实现）" % (band.width, band.height):
                {"left": monitor["left"] + band.left, "top": monitor["top"] + band.top,
                 "width": band.width, "height": band.height},
            "单卡 ROI %dx%d" % (single.width, single.height):
                {"left": monitor["left"] + single.left, "top": monitor["top"] + single.top,
                 "width": single.width, "height": single.height},
        }
        for name, region in regions.items():
            samples = []
            for _ in range(30):
                t0 = time.perf_counter()
                sct.grab(region)
                samples.append(time.perf_counter() - t0)
            log(fmt_latencies(name, samples))


def section_hash(roi, frame: np.ndarray) -> None:
    head("4) 像素哈希 + numpy 切片成本")
    import hashlib

    band = roi.capture_bounds()
    t0 = time.perf_counter()
    for _ in range(200):
        view = frame[band.top:band.bottom, band.left:band.right]
    log("band 切片 x200        : %.3f ms/次" % ((time.perf_counter() - t0) / 200 * 1000))

    slices = []
    for index in range(3):
        box = roi.title_box(index)
        slices.append(frame[box.top:box.bottom, box.left:box.right].copy())
    t0 = time.perf_counter()
    for _ in range(200):
        for image in slices:
            hashlib.blake2b(image.tobytes(), digest_size=8).hexdigest()
    log("三槽 tobytes+blake2b x200: %.3f ms/轮" % ((time.perf_counter() - t0) / 200 * 1000))


# ---------------------------------------------------------------------------
# 5) CardReader.read_once 三种场景
# ---------------------------------------------------------------------------

def run_cadence(reader: CardReader, ocr: CountingOcr, capture: FrameCapture,
                seconds: float, interval: float, disturb_each: bool, roi,
                frame: np.ndarray) -> dict:
    """按主循环节奏（0.25s）跑一段时间，返回统计。"""
    ocr.reset()
    capture.grab_calls = 0
    meter = CpuMeter()
    iterations = 0
    overdue = 0
    started = time.perf_counter()
    meter.start()
    while time.perf_counter() - started < seconds:
        if disturb_each:
            disturb(frame, roi, iterations + 1)
        t0 = time.perf_counter()
        reader.read_once()
        elapsed = time.perf_counter() - t0
        iterations += 1
        if elapsed > interval:
            overdue += 1
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
    wall = time.perf_counter() - started
    cores, percent = meter.stop()
    return {
        "wall": wall,
        "iterations": iterations,
        "ocr_calls": len(ocr.calls),
        "ocr_per_slot": ocr.per_slot(),
        "latencies": list(ocr.latencies),
        "cores": cores,
        "percent": percent,
        "grabs": capture.grab_calls,
        "overdue": overdue,
    }


def section_read_once(engine: OcrEngine, roi, frame: np.ndarray) -> None:
    head("5) CardReader.read_once()：三种状态实测（scan_interval=0.25s）")
    ocr = CountingOcr(engine)
    capture = FrameCapture(frame)
    reader = CardReader(capture, ocr_engine=ocr, interval=0.25)

    # ---- 场景 A：海克斯界面，牌面稳定（像素完全不变） ----
    result = run_cadence(reader, ocr, capture, 10.0, 0.25, False, roi, frame)
    log("[A] 海克斯界面 · 牌面稳定 10 秒")
    log("    迭代次数=%d  OCR 次数=%d（slot0/1/2=%s）  OCR/秒=%.2f"
        % (result["iterations"], result["ocr_calls"], result["ocr_per_slot"],
           result["ocr_calls"] / result["wall"]))
    log("    截图次数=%d（%.1f 次/秒）  CPU=%.2f 核 / %.1f%%"
        % (result["grabs"], result["grabs"] / result["wall"],
           result["cores"], result["percent"]))

    # ---- 场景 B：对局中（game world 在动 -> 像素每帧都变） ----
    reader2 = CardReader(capture, ocr_engine=ocr, interval=0.25)
    result = run_cadence(reader2, ocr, capture, 6.0, 0.25, True, roi, frame)
    log("[B] 普通对局/刷牌（像素每帧变化）6 秒")
    log("    迭代次数=%d  OCR 次数=%d（slot0/1/2=%s）  OCR/秒=%.2f"
        % (result["iterations"], result["ocr_calls"], result["ocr_per_slot"],
           result["ocr_calls"] / result["wall"]))
    log("    截图次数=%d（%.1f 次/秒）  CPU=%.2f 核 / %.1f%%  超过 0.25s 的轮次=%d"
        % (result["grabs"], result["grabs"] / result["wall"], result["cores"],
           result["percent"], result["overdue"]))
    log("    %s" % fmt_latencies("    单次识别延迟", result["latencies"]))

    # ---- 场景 C：牌面稳定但每 5 秒才变化一次（真实"稳定后"） ----
    reader3 = CardReader(capture, ocr_engine=ocr, interval=0.25)
    result = run_cadence(reader3, ocr, capture, 6.0, 0.25, False, roi, frame)
    log("[C] 海克斯界面已稳定 5 秒以后（继续 6 秒无变化）")
    log("    迭代次数=%d  OCR 次数=%d  OCR/秒=%.2f  CPU=%.2f 核 / %.1f%%"
        % (result["iterations"], result["ocr_calls"],
           result["ocr_calls"] / result["wall"], result["cores"], result["percent"]))


# ---------------------------------------------------------------------------
# 6) cProfile 热点
# ---------------------------------------------------------------------------

def section_profile(engine: OcrEngine, roi, frame: np.ndarray) -> None:
    head("6) cProfile 热点（像素每帧变化的场景）")
    ocr = CountingOcr(engine)
    capture = FrameCapture(frame)
    reader = CardReader(capture, ocr_engine=ocr, interval=0.0)

    profiler = cProfile.Profile()
    profiler.enable()
    for index in range(8):
        disturb(frame, roi, index + 1)
        reader.read_once()
    profiler.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    stats.print_stats(14)
    for line in stream.getvalue().splitlines()[:28]:
        log("  " + line)

    # 归类占比（按 cumulative 的顶层分类，粗略但真实）
    total = stats.total_tt
    buckets = {"OCR(识别/推理)": 0.0, "截图/采集": 0.0, "哈希/切片": 0.0,
               "文本匹配": 0.0, "其他/框架": 0.0}
    for (filename, lineno, func), (cc, nc, tt, ct, callers) in stats.stats.items():
        tag = "%s:%s" % (filename.replace("\\", "/").split("/")[-1], func)
        if any(k in tag for k in ("TextRecognize", "rec_", "onnxruntime", "infer",
                                  "engine.py", "card_reader.py", "RapidOCR")):
            buckets["OCR(识别/推理)"] += tt
        elif any(k in tag for k in ("mss", "capture.py", "get_pixel")):
            buckets["截图/采集"] += tt
        elif any(k in tag for k in ("blake2b", "_hash_image", "tobytes", "slicing",
                                    "getitem", "array")):
            buckets["哈希/切片"] += tt
        elif any(k in tag for k in ("matcher.py", "SequenceMatcher", "normalize")):
            buckets["文本匹配"] += tt
        else:
            buckets["其他/框架"] += tt
    log("")
    log("按自身时间(tt)归类（总 %.3fs，8 轮识别）：" % total)
    for name, value in buckets.items():
        log("  %-14s %.3fs  %.1f%%" % (name, value, value / total * 100 if total else 0))


# ---------------------------------------------------------------------------
# 7) Overlay / HTTP / 常驻进程 CPU
# ---------------------------------------------------------------------------

def section_overlay_and_http(roi) -> None:
    head("7) Overlay 更新成本 与 每轮 HTTP 成本")
    try:
        from PySide6 import QtWidgets

        from src.overlay.window import OverlayWindow

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = OverlayWindow(screen_index=0, opacity=100, offset_x=0, offset_y=0)
        window.show_overlay()
        payload = [
            {"augment_id": 1325, "name_cn": "玻璃大炮", "performance": 70.88,
             "popular": 0.49, "recommended": False},
            {"augment_id": 1115, "name_cn": "最万用的瞄准镜", "performance": 81.77,
             "popular": 1.93, "recommended": True, "tie": False},
            {"augment_id": 1079, "name_cn": "战争交响乐", "performance": 80.07,
             "popular": 0.84, "recommended": False},
        ]
        t0 = time.perf_counter()
        for _ in range(100):
            window.set_offers(payload)
            QtWidgets.QApplication.processEvents()
        log("Overlay.set_offers + processEvents x100 : %.3f ms/次"
            % ((time.perf_counter() - t0) / 100 * 1000))
        t0 = time.perf_counter()
        for _ in range(100):
            window.set_offers([])
        log("Overlay.set_offers(空) x100             : %.3f ms/次"
            % ((time.perf_counter() - t0) / 100 * 1000))
        window.hide()
        window.close()
        _ = app
    except Exception as exc:  # noqa: BLE001
        log("Overlay 测量失败: %s" % exc)

    from src.core.state_hub import HUB

    t0 = time.perf_counter()
    for _ in range(1000):
        HUB.snapshot()
    log("StateHub.snapshot() x1000               : %.3f ms/次"
        % ((time.perf_counter() - t0) / 1000 * 1000))

    # 每轮 Live Client API 调用：当前实现每次 new 一个 httpx.AsyncClient
    try:
        import asyncio

        import httpx

        from src.lcu.credential import LcuCredentialProvider

        credentials = LcuCredentialProvider().find_credentials()
        if credentials is None:
            log("LCU 未运行：跳过 HTTP 成本测量")
            return

        async def one_call() -> float:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(base_url=credentials.base_url, verify=False,
                                         timeout=4.0,
                                         auth=("riot", credentials.token)) as client:
                await client.get("/lol-gameflow/v1/gameflow-phase")
            return time.perf_counter() - t0

        samples = [asyncio.run(one_call()) for _ in range(10)]
        log(fmt_latencies("新建 httpx client + GET LCU（每轮一次）", samples))
    except Exception as exc:  # noqa: BLE001
        log("HTTP 测量失败: %s" % exc)


def section_running_app() -> None:
    head("8) 当前正在运行的正式版 CPU（常驻、等待游戏中）")
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] and "海克斯助手" in proc.info["name"]:
            try:
                proc.cpu_percent(None)
                time.sleep(3.0)
                log("PID %s  CPU=%.2f%%（3 秒平均，等待游戏阶段） 线程=%s"
                    % (proc.pid, proc.cpu_percent(None), proc.num_threads()))
            except Exception as exc:  # noqa: BLE001
                log("读取进程失败: %s" % exc)


def main() -> int:
    log("LOL 海克斯助手 · CPU 来源定位报告")
    log("生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    frame = load_frame()
    roi = build_roi_set(frame.shape[1], frame.shape[0])
    log("样本截图: %s  (%dx%d)" % (SAMPLE_SCREENSHOT.name, frame.shape[1], frame.shape[0]))
    stamp_slot_markers(frame, roi)

    engine = OcrEngine()

    # 先初始化（把模型加载成本排除在后面的测量之外）
    warm = frame[roi.title_box(0).top:roi.title_box(0).bottom,
                 roi.title_box(0).left:roi.title_box(0).right]
    engine.recognize_texts(warm, use_det=False)

    ocr = CountingOcr(engine)
    section_env(engine, ocr)
    section_latency(engine, roi, frame)
    section_hash(roi, frame)
    section_read_once(engine, roi, frame)
    section_profile(engine, roi, frame)
    section_overlay_and_http(roi)
    section_running_app()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log("")
    log("完整报告已写入: %s" % OUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
