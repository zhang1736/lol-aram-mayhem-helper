"""P0-1 回归测试：RapidOCR / ONNX Runtime 线程必须被限制。

实测（2026-09-22）：
    默认 intra/inter = 0（= 自动 → 物理核 24）+ 每次推理后线程池空转 2~4 秒：
        未加载 0.00 核 / 0.0%（线程 30）
        加载后 3.60 核 / 15.0%（线程 99）
        推理后 2.17 核 /  9.0%
    限制成 2/1 之后：线程 33、推理后空转 0.24 核 / 1.0%、单次识别 15~16ms。
"""

from __future__ import annotations

import pytest

from src.ocr.engine import (
    OCR_INTER_OP_THREADS,
    OCR_INTRA_OP_THREADS,
    OcrEngine,
)


def test_thread_limits_are_explicit_values() -> None:
    assert OCR_INTRA_OP_THREADS == 2
    assert OCR_INTER_OP_THREADS == 1
    assert OCR_INTER_OP_THREADS < OCR_INTRA_OP_THREADS


def test_ocr_engine_passes_thread_limits_to_rapidocr() -> None:
    """构造函数必须把 2/1 传给 RapidOCR（不猜参数名：rapidocr 1.4.4 的 kwarg）。"""
    captured: dict = {}

    class FakeRapidOCR:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    engine = OcrEngine()
    engine._engine = None

    import src.ocr.engine as engine_mod

    original = engine_mod._load_rapidocr
    engine_mod._load_rapidocr = lambda: (FakeRapidOCR, "fake")  # type: ignore[assignment]
    try:
        engine.ensure_ready()
    finally:
        engine_mod._load_rapidocr = original  # type: ignore[assignment]

    assert captured == {"intra_op_num_threads": 2, "inter_op_num_threads": 1}
    assert engine.thread_limit_applied is True


def test_ocr_engine_falls_back_when_kwargs_unsupported() -> None:
    """rapidocr 新包名不支持这两个 kwarg 时：退回默认，不抛异常、不重写库。"""
    calls: list = []

    class PickyRapidOCR:
        def __init__(self, **kwargs) -> None:
            if kwargs:
                raise TypeError("unexpected keyword argument")
            calls.append("default")

    import src.ocr.engine as engine_mod

    original = engine_mod._load_rapidocr
    engine_mod._load_rapidocr = lambda: (PickyRapidOCR, "picky")  # type: ignore[assignment]
    try:
        engine = OcrEngine()
        engine.ensure_ready()
    finally:
        engine_mod._load_rapidocr = original  # type: ignore[assignment]
    assert calls == ["default"]
    assert engine.thread_limit_applied is False


def test_real_rapidocr_sessions_use_two_threads() -> None:
    """真实加载模型后，det/cls/rec 三个会话都必须是 2/1。"""
    pytest.importorskip("rapidocr_onnxruntime")
    import numpy as np
    import psutil

    process = psutil.Process()
    before = process.num_threads()
    engine = OcrEngine()
    engine.ensure_ready()
    threads = engine.onnx_threads()
    if not threads:
        pytest.skip("无法读取 ONNX 会话配置")
    for name in ("det", "cls", "rec"):
        assert name in threads, "缺少 %s 会话" % name
        assert threads[name] == (OCR_INTRA_OP_THREADS, OCR_INTER_OP_THREADS), threads
    # 真实推理一次：只允许新增少量线程
    # （默认配置下 3 个会话会给进程 +69 个线程：30 -> 99）
    engine.recognize_texts(np.zeros((58, 430, 3), np.uint8), use_det=False)
    added = process.num_threads() - before
    assert added < 20, "限制线程后不应新增几十个线程，实际 +%d" % added


def test_engine_without_limit_can_still_be_constructed() -> None:
    """诊断用：显式传 None 时不加限制（仅供对照实验，不用于正式版）。"""
    engine = OcrEngine(intra_op_num_threads=None, inter_op_num_threads=None)
    assert engine.intra_op_num_threads is None
    assert engine.inter_op_num_threads is None
