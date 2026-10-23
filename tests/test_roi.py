"""三卡 ROI 与分辨率缩放测试。"""

from __future__ import annotations

import pytest

from src.ocr.roi import (
    BASE_HEIGHT,
    BASE_WIDTH,
    CARD_CENTERS_X,
    SUPPORTED_RESOLUTIONS,
    build_roi_set,
    compute_scale,
)


def test_base_resolution_roi_matches_measured_values() -> None:
    """基准 ROI 必须与 Day 3 真实截图实测一致：
    名称中心 x = 798/1288/1778，名称行 y = 566..616。"""
    roi = build_roi_set(BASE_WIDTH, BASE_HEIGHT)
    assert roi.scale_x == 1.0 and roi.scale_y == 1.0
    for index, center in enumerate(CARD_CENTERS_X):
        box = roi.title_box(index)
        assert box.left <= center <= box.right
        assert abs((box.left + box.right) / 2 - center) <= 1
        assert box.top <= 566 and box.bottom >= 616


def test_title_rois_do_not_overlap() -> None:
    roi = build_roi_set(BASE_WIDTH, BASE_HEIGHT)
    boxes = [roi.title_box(i) for i in range(3)]
    assert boxes[0].right < boxes[1].left
    assert boxes[1].right < boxes[2].left


def test_title_roi_excludes_label_row() -> None:
    """名称行 ROI 必须排除下方标签（实测标签在 y>=614）。"""
    roi = build_roi_set(BASE_WIDTH, BASE_HEIGHT)
    for index in range(3):
        assert roi.title_box(index).bottom <= 620


@pytest.mark.parametrize("width,height", SUPPORTED_RESOLUTIONS)
def test_scaling_for_supported_resolutions(width: int, height: int) -> None:
    roi = build_roi_set(width, height)
    expected_x, expected_y = compute_scale(width, height)
    assert roi.scale_x == pytest.approx(expected_x)
    assert roi.scale_y == pytest.approx(expected_y)
    for index, center in enumerate(CARD_CENTERS_X):
        box = roi.title_box(index)
        assert 0 <= box.left < box.right <= width
        assert 0 <= box.top < box.bottom <= height
        assert abs((box.left + box.right) / 2 - center * expected_x) <= 2


def test_1080p_scale_is_three_quarters() -> None:
    roi = build_roi_set(1920, 1080)
    assert roi.scale_x == pytest.approx(0.75)
    assert roi.scale_y == pytest.approx(0.75)
    assert roi.title_box(0).width == pytest.approx(
        build_roi_set(BASE_WIDTH, BASE_HEIGHT).title_box(0).width * 0.75, abs=2)


def test_capture_bounds_cover_all_title_rois() -> None:
    roi = build_roi_set(BASE_WIDTH, BASE_HEIGHT)
    bounds = roi.capture_bounds()
    for index in range(3):
        box = roi.title_box(index)
        assert bounds.left <= box.left and bounds.right >= box.right
        assert bounds.top <= box.top and bounds.bottom >= box.bottom
