"""三卡 ROI 与分辨率缩放。

基准分辨率 2560x1440。实测依据（Day 3 截图；仓库内保留的是脱敏卡片样本）：
    三张卡的名称中心 x = 798 / 1288 / 1778，间距精确 490
    名称文字像素落在 y 576..610
    只裁名称行（y 566..616）时三张卡全部 exact 1.000；
    若带上下方「综合 / 伤害 / 伤害速度」标签行（y 555..630）则 1079 匹配失败。

因此 TITLE_ROI 只覆盖名称行，绝不 OCR 整张卡。
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_WIDTH = 2560
BASE_HEIGHT = 1440

#: 卡片中心（基准分辨率）
CARD_CENTERS_X = (798, 1288, 1778)

#: 名称行 ROI（基准分辨率）：x 中心 ±215，y 562..620
TITLE_HALF_WIDTH = 215
TITLE_TOP = 562
TITLE_BOTTOM = 620

#: 整张卡的范围（用于 Overlay 定位，不做 OCR）
CARD_HALF_WIDTH = 245
CARD_TOP = 470
CARD_BOTTOM = 700

#: 支持的分辨率（第一版）
SUPPORTED_RESOLUTIONS = ((1920, 1080), (2560, 1440), (3840, 2160))


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


@dataclass(frozen=True)
class RoiSet:
    """一组随分辨率缩放后的 ROI。"""

    screen_width: int
    screen_height: int
    scale_x: float
    scale_y: float
    title_boxes: tuple[Box, Box, Box]
    card_boxes: tuple[Box, Box, Box]

    def title_box(self, index: int) -> Box:
        """index: 0/1/2 -> CARD_1/CARD_2/CARD_3。"""
        return self.title_boxes[index]

    def card_box(self, index: int) -> Box:
        return self.card_boxes[index]

    def capture_bounds(self) -> Box:
        """三张名称行 ROI 的最小外接范围（用于 mss 一次截取）。"""
        left = min(b.left for b in self.title_boxes)
        top = min(b.top for b in self.title_boxes)
        right = max(b.right for b in self.title_boxes)
        bottom = max(b.bottom for b in self.title_boxes)
        return Box(left, top, right - left, bottom - top)


def compute_scale(width: int, height: int) -> tuple[float, float]:
    return width / BASE_WIDTH, height / BASE_HEIGHT


def _scale_box(left: float, top: float, right: float, bottom: float,
               scale_x: float, scale_y: float,
               screen_width: int, screen_height: int) -> Box:
    box_left = max(0, int(round(left * scale_x)))
    box_top = max(0, int(round(top * scale_y)))
    box_right = min(screen_width, int(round(right * scale_x)))
    box_bottom = min(screen_height, int(round(bottom * scale_y)))
    return Box(box_left, box_top,
               max(1, box_right - box_left), max(1, box_bottom - box_top))


def build_roi_set(width: int, height: int) -> RoiSet:
    """按当前分辨率生成三张卡的 ROI。"""
    scale_x, scale_y = compute_scale(width, height)

    title_boxes = []
    card_boxes = []
    for center in CARD_CENTERS_X:
        title_boxes.append(_scale_box(
            center - TITLE_HALF_WIDTH, TITLE_TOP,
            center + TITLE_HALF_WIDTH, TITLE_BOTTOM,
            scale_x, scale_y, width, height))
        card_boxes.append(_scale_box(
            center - CARD_HALF_WIDTH, CARD_TOP,
            center + CARD_HALF_WIDTH, CARD_BOTTOM,
            scale_x, scale_y, width, height))

    return RoiSet(
        screen_width=width,
        screen_height=height,
        scale_x=scale_x,
        scale_y=scale_y,
        title_boxes=(title_boxes[0], title_boxes[1], title_boxes[2]),
        card_boxes=(card_boxes[0], card_boxes[1], card_boxes[2]),
    )


__all__ = [
    "Box", "RoiSet", "build_roi_set", "compute_scale",
    "BASE_WIDTH", "BASE_HEIGHT", "CARD_CENTERS_X", "SUPPORTED_RESOLUTIONS",
    "TITLE_TOP", "TITLE_BOTTOM", "TITLE_HALF_WIDTH",
    "CARD_TOP", "CARD_BOTTOM", "CARD_HALF_WIDTH",
]
