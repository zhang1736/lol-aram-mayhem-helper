"""验证「ONNX Runtime 线程池空转烧 CPU」+ 各限流方案的代价。

    & ".venv\\Scripts\\python.exe" research\\cpu_spin_test.py

只测量，不改项目代码。
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

PROC = psutil.Process()
NCPU = psutil.cpu_count(logical=True) or 1
OUT = PROJECT_ROOT / "research" / "cpu_spin_test.txt"
LINES: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    LINES.append(str(msg))


def idle_cpu(seconds: float) -> tuple[float, float]:
    """空转 CPU：返回（占用核数, 占整机百分比）。"""
    t0 = PROC.cpu_times()
    w0 = time.perf_counter()
    time.sleep(seconds)
    t1 = PROC.cpu_times()
    wall = time.perf_counter() - w0
    cores = ((t1.user + t1.system) - (t0.user + t0.system)) / wall
    return cores, cores / NCPU * 100


def fmt_cpu(cores: float, percent: float) -> str:
    return "%.2f 核 / %.1f%%（整机 %d 核）" % (cores, percent, NCPU)


def bench_recognize(engine, image, rounds: int = 5) -> list[float]:
    out = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        engine(image, use_det=False, use_cls=False)
        out.append(time.perf_counter() - t0)
    return out


def main() -> int:
    log("ONNX Runtime 线程 / 空转实验")
    log("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("CPU: %s 逻辑核 / %s 物理核" % (psutil.cpu_count(), psutil.cpu_count(logical=False)))
    log("")

    image = np.zeros((58, 430, 3), np.uint8)

    log("[0] 未加载模型时的空转 CPU")
    log("    " + fmt_cpu(*idle_cpu(3.0)))
    log("    线程数 = %d" % PROC.num_threads())

    from rapidocr_onnxruntime import RapidOCR

    log("")
    log("[1] 默认参数 RapidOCR()：intra/inter_op_num_threads = -1（= 交给 ORT，全核）")
    engine = RapidOCR()
    log("    模型加载后（还没推理）空转 CPU")
    log("    " + fmt_cpu(*idle_cpu(3.0)))
    log("    线程数 = %d" % PROC.num_threads())
    lat = bench_recognize(engine, image, 5)
    log("    单次识别: %s" % ", ".join("%.0f ms" % (v * 1000) for v in lat))
    log("    推理之后空转 5 秒 CPU")
    log("    " + fmt_cpu(*idle_cpu(5.0)))
    log("    线程数 = %d" % PROC.num_threads())

    log("")
    log("[2] 限制线程 RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)")
    engine2 = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    lat2 = bench_recognize(engine2, image, 5)
    log("    单次识别: %s" % ", ".join("%.0f ms" % (v * 1000) for v in lat2))
    log("    推理之后空转 5 秒 CPU")
    log("    " + fmt_cpu(*idle_cpu(5.0)))
    for label, module in (("rec", engine2.text_rec),):
        raw = getattr(getattr(module, "session", None), "session", None)
        if raw is not None:
            so = raw.get_session_options()
            log("    实际生效: rec.intra_op_num_threads=%s inter=%s"
                % (so.intra_op_num_threads, so.inter_op_num_threads))

    log("")
    log("[3] 直接建会话：allow_spinning=0（关闭线程池自旋）")
    import onnxruntime as ort

    model = Path(ort.__file__).parent.parent / "rapidocr_onnxruntime" / "models" / \
        "ch_PP-OCRv4_rec_infer.onnx"
    if not model.exists():
        import rapidocr_onnxruntime

        model = Path(rapidocr_onnxruntime.__file__).parent / "models" / "ch_PP-OCRv4_rec_infer.onnx"
    log("    模型: %s" % model.name)
    feed = np.zeros((1, 3, 48, 320), np.float32)

    for label, intra, spin in (("默认(24 线程, 允许自旋)", 0, "1"),
                               ("2 线程 + 自旋开", 2, "1"),
                               ("2 线程 + 自旋关", 2, "0"),
                               ("1 线程 + 自旋关", 1, "0")):
        so = ort.SessionOptions()
        so.log_severity_level = 4
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if intra:
            so.intra_op_num_threads = intra
            so.inter_op_num_threads = 1
        so.add_session_config_entry("session.intra_op.allow_spinning", spin)
        session = ort.InferenceSession(str(model), sess_options=so,
                                       providers=["CPUExecutionProvider"])
        name = session.get_inputs()[0].name
        latencies = []
        for _ in range(5):
            t0 = time.perf_counter()
            session.run(None, {name: feed})
            latencies.append(time.perf_counter() - t0)
        cores, percent = idle_cpu(4.0)
        log("    %-22s 单次 %.0f ms  空转 %s"
            % (label, min(latencies) * 1000, fmt_cpu(cores, percent)))
        del session

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    log("")
    log("写入: %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
