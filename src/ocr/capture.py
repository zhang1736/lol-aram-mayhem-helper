"""屏幕采集（mss）。

只截取需要的区域，不每次 OCR 都抓整屏。
"""

from __future__ import annotations

from typing import Any, Optional

from src.ocr.roi import Box, RoiSet, build_roi_set
from src.utils.logger import get_logger

logger = get_logger(__name__)


class CaptureUnavailableError(RuntimeError):
    """mss 未安装或无法初始化。"""


class ScreenCapture:
    """基于 mss 的屏幕采集。"""

    def __init__(self, monitor_index: int = 1) -> None:
        self.monitor_index = monitor_index
        self._sct: Any = None
        self._size: Optional[tuple[int, int]] = None

    # -- 生命周期 -----------------------------------------------------------
    def _ensure(self) -> Any:
        if self._sct is None:
            try:
                import mss
            except ImportError as exc:
                raise CaptureUnavailableError("mss 未安装，无法截屏") from exc
            self._sct = mss.mss()
        return self._sct

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None

    def __enter__(self) -> "ScreenCapture":
        self._ensure()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 分辨率 -------------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        if self._size is None:
            sct = self._ensure()
            monitor = sct.monitors[self.monitor_index]
            self._size = (int(monitor["width"]), int(monitor["height"]))
        return self._size

    def build_roi_set(self) -> RoiSet:
        width, height = self.size
        return build_roi_set(width, height)

    # -- 截图 ---------------------------------------------------------------
    def grab(self, box: Box | None = None) -> Any:
        """截取指定区域（默认整屏），返回 numpy 数组（BGR 顺序，供 OCR 使用）。"""
        sct = self._ensure()
        monitor = sct.monitors[self.monitor_index]
        if box is None:
            region = {"left": monitor["left"], "top": monitor["top"],
                      "width": monitor["width"], "height": monitor["height"]}
        else:
            region = {"left": monitor["left"] + box.left,
                      "top": monitor["top"] + box.top,
                      "width": box.width, "height": box.height}
        shot = sct.grab(region)
        return _to_array(shot)

    def grab_region(self, box: Box) -> Any:
        return self.grab(box)

    def grab_full(self) -> Any:
        return self.grab(None)


def _to_array(shot: Any) -> Any:
    """mss 截图 -> numpy 数组。优先用 numpy（RapidOCR 需要），否则退回 PIL。"""
    try:
        import numpy as np

        return np.array(shot)[:, :, :3]
    except ImportError:
        from PIL import Image

        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


__all__ = ["ScreenCapture", "CaptureUnavailableError"]
