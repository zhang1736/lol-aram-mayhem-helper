"""RapidOCR 引擎封装。

Day 3 范围：输入一张截图（路径 / ndarray / PIL）-> 输出文字。
不做全屏 OCR，也不做三卡定位。

依赖：rapidocr_onnxruntime（Python 3.9~3.12）。
若该包在当前 Python 3.12 上不可用，会抛出 OcrUnavailableError 并保留原因，
绝不静默降级或让调用方崩溃。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from src.utils.logger import get_logger

logger = get_logger(__name__)

#: ONNX Runtime 线程数（2026-09-22 实测结论）：
#:   rapidocr_onnxruntime 的 config.yaml 写 intra/inter = -1，
#:   而 utils/infer_engine.py::_init_sess_opts() 对 -1 **不设置** →
#:   ONNX 取默认 0 = 自动（按物理核，本机 24 线程），且推理后线程池要空转 2~4 秒：
#:       未加载模型        0.00 核 /  0.0%   线程 30
#:       加载后（未推理）  3.60 核 / 15.0%   线程 99
#:       1 次推理后       2.17 核 /  9.0%
#:       限制到 2/1 后    0.09 核 /  0.4%（单次识别 21~26ms，与 24 线程的 16~23ms 基本一致）
#: 因此这里显式限制线程，避免"OCR 一次，空转三秒"。
OCR_INTRA_OP_THREADS = 2
OCR_INTER_OP_THREADS = 1


class OcrUnavailableError(RuntimeError):
    """RapidOCR 未安装或无法初始化。"""


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]   # x1, y1, x2, y2


def _load_rapidocr():
    """依次尝试 rapidocr_onnxruntime 与新包名 rapidocr。"""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        return RapidOCR, "rapidocr_onnxruntime"
    except ImportError:
        pass
    try:
        from rapidocr import RapidOCR  # type: ignore

        return RapidOCR, "rapidocr"
    except ImportError as exc:
        raise OcrUnavailableError(
            "RapidOCR 未安装。请先安装 rapidocr_onnxruntime（或 rapidocr）。"
        ) from exc


def _to_bbox(box: Sequence[Sequence[float]] | None) -> tuple[int, int, int, int]:
    if not box:
        return (0, 0, 0, 0)
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


class OcrEngine:
    """RapidOCR 包装器。

    保留懒加载语义供测试/独立调用使用；正式常驻流程会在启动后由后台线程
    调用 ``start_ocr_prewarm()``，让约 0.9 秒的模型初始化不再落进第一次海克斯。
    """

    def __init__(self, engine: Any | None = None,
                 intra_op_num_threads: int | None = OCR_INTRA_OP_THREADS,
                 inter_op_num_threads: int | None = OCR_INTER_OP_THREADS) -> None:
        self._engine = engine
        self._backend: Optional[str] = None
        #: None = 用 RapidOCR 默认（不限制线程，只用于对照实验）
        self.intra_op_num_threads = intra_op_num_threads
        self.inter_op_num_threads = inter_op_num_threads
        self.thread_limit_applied = False
        self.init_ms = 0.0
        self._init_lock = threading.RLock()

    @property
    def backend(self) -> Optional[str]:
        return self._backend

    @property
    def available(self) -> bool:
        try:
            self._ensure_engine()
            return True
        except OcrUnavailableError:
            return False

    def _ensure_engine(self) -> Any:
        # 预热线程与识别线程可能同时到达；锁保证整个进程只创建一个模型实例。
        with self._init_lock:
            if self._engine is None:
                started = time.perf_counter()
                rapidocr_cls, backend = _load_rapidocr()
                kwargs = {}
                if self.intra_op_num_threads:
                    kwargs["intra_op_num_threads"] = int(self.intra_op_num_threads)
                if self.inter_op_num_threads:
                    kwargs["inter_op_num_threads"] = int(self.inter_op_num_threads)
                try:
                    self._engine = rapidocr_cls(**kwargs) if kwargs else rapidocr_cls()
                    self.thread_limit_applied = bool(kwargs)
                except TypeError:
                    # 老版本 / 新包名(rapidocr) 不支持这两个 kwarg：
                    # 退回默认构造（**不重写 RapidOCR**），只记日志。
                    if not kwargs:
                        raise
                    logger.warning("RapidOCR 不支持线程参数 %s，退回默认线程配置", kwargs)
                    try:
                        self._engine = rapidocr_cls()
                    except Exception as exc:  # noqa: BLE001
                        raise OcrUnavailableError(
                            f"RapidOCR 初始化失败: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - 失败时保留可重试状态
                    raise OcrUnavailableError(f"RapidOCR 初始化失败: {exc}") from exc
                self.init_ms = (time.perf_counter() - started) * 1000.0
                self._backend = backend
                threads = self.onnx_threads()
                logger.info(
                    "RapidOCR 初始化完成 (backend=%s, 耗时=%.1f ms, "
                    "线程限制=%s, ONNX线程=%s)",
                    backend, self.init_ms, kwargs or "默认", threads or "未知")
            return self._engine

    # -- 诊断 ---------------------------------------------------------------
    def ensure_ready(self) -> Any:
        """显式初始化（供诊断 / 测试使用）。"""
        return self._ensure_engine()

    def onnx_threads(self) -> dict[str, tuple[int, int]]:
        """读取**真实生效**的 ONNX 线程配置 {模型: (intra_op, inter_op)}。"""
        out: dict[str, tuple[int, int]] = {}
        if self._engine is None:
            return out
        for label, attr in (("det", "text_det"), ("cls", "text_cls"), ("rec", "text_rec")):
            module = getattr(self._engine, attr, None)
            if module is None:
                continue
            raw = None
            for name in ("infer", "session"):
                candidate = getattr(module, name, None)
                raw = getattr(candidate, "session", candidate)
                if raw is not None and hasattr(raw, "get_session_options"):
                    break
            if raw is None or not hasattr(raw, "get_session_options"):
                continue
            try:
                options = raw.get_session_options()
                out[label] = (int(options.intra_op_num_threads),
                              int(options.inter_op_num_threads))
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取 %s ONNX 线程配置失败: %s", label, exc)
        return out

    def recognize(self, image: str | Path | Any,
                  use_det: bool | None = None,
                  use_cls: bool | None = None) -> list[OcrResult]:
        """识别一张图片，按从上到下、从左到右返回文本块。

        use_det=False 时跳过文本检测模型（识别专用）。
        实测（Day 4）：对已经裁好的单行 ROI，
        默认调用约 1.8~2.8s，det=False 仅约 0.09s（快 20~30 倍），结果一致。
        """
        engine = self._ensure_engine()
        source = str(image) if isinstance(image, Path) else image
        kwargs = {}
        if use_det is not None:
            kwargs["use_det"] = use_det
        if use_cls is not None:
            kwargs["use_cls"] = use_cls
        try:
            raw = engine(source, **kwargs) if kwargs else engine(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 识别失败: %s", exc)
            return []

        # rapidocr 常见返回：(list[[box, text, score]], elapse)
        # det=False 时返回：(list[[text, score]], elapse)
        lines = raw[0] if isinstance(raw, tuple) and raw else raw
        if not lines:
            return []

        results: list[OcrResult] = []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            if len(line) >= 3:
                box, text, score = line[0], line[1], line[2]
            else:
                box, text, score = None, line[0], line[1]
            if not isinstance(text, str):
                continue
            try:
                confidence = float(score)
            except (TypeError, ValueError):
                confidence = 0.0
            results.append(OcrResult(text=text, confidence=confidence, bbox=_to_bbox(box)))

        results.sort(key=lambda r: (r.bbox[1], r.bbox[0]))
        return results

    def recognize_line(self, image: str | Path | Any) -> list[OcrResult]:
        """单行 ROI 专用：跳过检测与方向分类，只跑识别模型。"""
        return self.recognize(image, use_det=False, use_cls=False)

    def recognize_texts(self, image: str | Path | Any,
                        use_det: bool | None = None) -> list[str]:
        return [r.text for r in self.recognize(image, use_det=use_det)]

    def recognize_joined(self, image: str | Path | Any, sep: str = "",
                         use_det: bool | None = None) -> str:
        return sep.join(self.recognize_texts(image, use_det=use_det))


_SHARED_ENGINE: Optional[OcrEngine] = None
_SHARED_LOCK = threading.Lock()
_PREWARM_THREAD: Optional[threading.Thread] = None


def shared_ocr_engine() -> OcrEngine:
    """正式流程共享的唯一 OCR 实例。"""
    global _SHARED_ENGINE
    with _SHARED_LOCK:
        if _SHARED_ENGINE is None:
            _SHARED_ENGINE = OcrEngine()
        return _SHARED_ENGINE


def start_ocr_prewarm() -> threading.Thread:
    """在后台预热共享 OCR；重复调用不会创建第二个模型实例。"""
    global _PREWARM_THREAD
    engine = shared_ocr_engine()
    with _SHARED_LOCK:
        if _PREWARM_THREAD is not None and _PREWARM_THREAD.is_alive():
            return _PREWARM_THREAD

        def worker() -> None:
            try:
                engine.ensure_ready()
                logger.info("OCR 后台预热完成（%.1f ms，正式流程可直接复用）",
                            engine.init_ms)
            except Exception as exc:  # noqa: BLE001 - 预热失败不能影响主程序
                logger.warning("OCR 后台预热失败（识别时会自动重试）: %s", exc)

        thread = threading.Thread(target=worker, name="ocr-prewarm", daemon=True)
        _PREWARM_THREAD = thread
        thread.start()
        return thread


__all__ = ["OcrEngine", "OcrResult", "OcrUnavailableError",
           "OCR_INTRA_OP_THREADS", "OCR_INTER_OP_THREADS",
           "shared_ocr_engine", "start_ocr_prewarm"]
