"""OCR 引擎测试。

默认只做引擎可用性与合成图自检；真实截图测试在
tests/fixtures/ocr/ 有图片时才运行（Day 3 尚无真实截图）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ocr.engine import OcrEngine, OcrResult, OcrUnavailableError

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ocr"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _engine_or_skip() -> OcrEngine:
    engine = OcrEngine()
    if not engine.available:
        pytest.skip("RapidOCR 不可用")
    return engine


def test_engine_is_lazy_until_first_use() -> None:
    engine = OcrEngine()
    assert engine.backend is None


def test_synthetic_image_smoke(tmp_path: Path) -> None:
    """引擎自检：合成图，不计入识别测试集。"""
    PIL = pytest.importorskip("PIL.Image")
    pytest.importorskip("PIL.ImageDraw")
    from PIL import ImageDraw, ImageFont

    engine = _engine_or_skip()
    image = PIL.new("RGB", (420, 120), (18, 18, 24))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 44)
    except OSError:
        font = ImageFont.load_default()
    draw.text((16, 34), "灵魂虹吸", font=font, fill=(240, 230, 210))
    path = tmp_path / "synthetic.png"
    image.save(path)

    results = engine.recognize(path)
    assert isinstance(results, list)
    if results:
        assert all(isinstance(r, OcrResult) for r in results)
        assert all(0.0 <= r.confidence <= 1.0 for r in results)
        assert all(len(r.bbox) == 4 for r in results)


def test_missing_file_returns_empty_list(tmp_path: Path) -> None:
    engine = _engine_or_skip()
    assert engine.recognize(tmp_path / "does_not_exist.png") == []


def _real_images() -> list[Path]:
    if not FIXTURE_DIR.exists():
        return []
    return sorted(p for p in FIXTURE_DIR.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def test_real_screenshots_recognize_and_match() -> None:
    """真实截图 -> OCR -> matcher -> augmentId。无截图时 skip。"""
    images = _real_images()
    if not images:
        pytest.skip("tests/fixtures/ocr/ 尚无真实截图，跳过识别测试")

    from src.data.augment_repository import AugmentRepository
    from src.ocr.matcher import AugmentMatcher

    engine = _engine_or_skip()
    matcher = AugmentMatcher(AugmentRepository().load())

    stats = {"total": 0, "matched": 0, "exact": 0, "normalized": 0,
             "alias": 0, "fuzzy": 0, "failed": 0}
    for path in images:
        stats["total"] += 1
        text = engine.recognize_joined(path)
        result = matcher.match(text)
        if result.augment_id is None:
            stats["failed"] += 1
        else:
            stats["matched"] += 1
            stats[result.method] = stats.get(result.method, 0) + 1

    print("\nTotal: %(total)d\nMatched: %(matched)d\nExact: %(exact)d\n"
          "Fuzzy: %(fuzzy)d\nFailed: %(failed)d" % stats)
    assert stats["total"] == len(images)
    assert stats["matched"] >= 1


def test_ocr_unavailable_error_is_runtime_error() -> None:
    assert issubclass(OcrUnavailableError, RuntimeError)
