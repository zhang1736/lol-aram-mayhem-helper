"""Overlay 贴牌面位置计算（纯几何，不依赖 PySide6，便于测试）。

实测（2026-09-21 正式版实战）：
    Overlay 贴在卡片正下方（2560x1440 下 y = 700 + 10 = 710）时，
    会盖住海克斯的**描述文字**。因此默认整体下移 +60px：
        2560x1440 ： y = 700 + 70 = 770
        1920x1080 ： 随 scale_y 缩放
        3840x2160 ： 随 scale_y 缩放

横向位置**不动**；``offset_x`` / ``offset_y`` 仍然是用户在设置里的
手动微调量（默认 0，叠加在默认位置之上）。
"""

from __future__ import annotations

from src.ocr.roi import build_roi_set

#: 卡片底部到 Overlay 顶部的间距（基准分辨率 2560x1440）
CARD_GAP_Y = 70


def card_gap(scale_y: float) -> int:
    """按分辨率缩放后的间距（保证 1080p / 1440p / 4K 视觉一致）。"""
    return int(round(CARD_GAP_Y * scale_y))


def overlay_card_positions(screen_width: int, screen_height: int,
                           offset_x: int = 0, offset_y: int = 0,
                           count: int = 3) -> list[tuple[int, int, int]]:
    """返回每张卡对应的 (left, top, width)。

    横向 = 卡片左边缘 + 用户 offset_x（横向位置不因本次修复而改变）
    纵向 = 卡片底部 + CARD_GAP_Y(按 scale_y 缩放) + 用户 offset_y
    """
    roi = build_roi_set(screen_width, screen_height)
    gap = card_gap(roi.scale_y)
    positions: list[tuple[int, int, int]] = []
    for index in range(count):
        box = roi.card_box(index)
        positions.append((box.left + int(offset_x),
                          box.bottom + gap + int(offset_y),
                          box.width))
    return positions


def default_overlay_y(screen_width: int, screen_height: int) -> int:
    """默认（无用户偏移）Overlay 顶部 y —— 用于验收 770 这个位置。"""
    return overlay_card_positions(screen_width, screen_height)[0][1]


__all__ = ["CARD_GAP_Y", "card_gap", "overlay_card_positions", "default_overlay_y"]
