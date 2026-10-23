"""打包后自检：把关键依赖的导入结果写到文件（exe 无控制台时排查用）。"""

from __future__ import annotations

import io
import os
import sys
import traceback

from src.utils import paths


def _try_import(name: str) -> str:
    try:
        module = __import__(name)
        version = getattr(module, "__version__", "")
        return "OK %s %s" % (name, version)
    except Exception:  # noqa: BLE001 - 自检要报告任何异常
        return "FAIL %s\n%s" % (name, traceback.format_exc())


def _probe_dlls() -> list[str]:
    """逐个尝试加载关键 DLL，定位到底哪一个加载失败。"""
    import ctypes

    base = paths.resource_dir()
    targets = [
        ("PySide6/Qt6Core.dll", base / "PySide6" / "Qt6Core.dll"),
        ("PySide6/Qt6Gui.dll", base / "PySide6" / "Qt6Gui.dll"),
        ("PySide6/Qt6Widgets.dll", base / "PySide6" / "Qt6Widgets.dll"),
        ("PySide6/pyside6.abi3.dll", base / "PySide6" / "pyside6.abi3.dll"),
        ("shiboken6/shiboken6.abi3.dll", base / "shiboken6" / "shiboken6.abi3.dll"),
    ]
    out: list[str] = []
    for label, path in targets:
        if not path.exists():
            out.append("MISSING %s (%s)" % (label, path))
            continue
        try:
            ctypes.WinDLL(str(path))
            out.append("LOAD OK %s" % label)
        except OSError as exc:
            out.append("LOAD FAIL %s -> %s" % (label, exc))
    return out


def run_selftest() -> int:
    lines = [
        "frozen=%s" % getattr(sys, "frozen", False),
        "executable=%s" % sys.executable,
        "MEIPASS=%s" % getattr(sys, "_MEIPASS", "-"),
        "resource_dir=%s" % paths.resource_dir(),
        "user_data_dir=%s" % paths.user_data_dir(),
        "python=%s" % sys.version.replace("\n", " "),
        "",
    ]
    for name in ("PySide6", "PySide6.QtCore", "shiboken6", "rapidocr_onnxruntime",
                 "onnxruntime", "psutil", "yaml", "pydantic", "httpx"):
        lines.append(_try_import(name))
        lines.append("")

    lines.append("--- dll probe ---")
    lines.extend(_probe_dlls())
    lines.append("")
    lines.append("--- resource check ---")
    for relative in ("data/kiwi_augments.json", "config/config.yaml",
                     "certs/windows_ca_bundle.pem"):
        path = paths.resource_dir() / relative
        lines.append("%s exists=%s" % (path, path.exists()))

    # 精简发布包后必须验证：cv2 图像函数 + RapidOCR 真实推理
    lines.append("")
    lines.append("--- ocr smoke ---")
    try:
        import numpy as np

        import cv2
        from src.ocr.engine import OcrEngine

        engine = OcrEngine()
        engine.ensure_ready()
        blank = np.zeros((58, 430, 3), np.uint8)
        texts = engine.recognize_texts(blank, use_det=False)
        lines.append("cv2=%s" % cv2.__version__)
        lines.append("onnx_threads=%s" % (engine.onnx_threads(),))
        lines.append("ocr_smoke=OK texts=%r" % (texts,))
    except Exception:  # noqa: BLE001 - 自检要报告任何异常
        lines.append("ocr_smoke=FAIL\n%s" % traceback.format_exc())

    target = paths.user_data_dir() / "selftest.txt"
    with io.open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest())
