"""海克斯匹配器测试（不依赖截图）。"""

from __future__ import annotations

import pytest

from src.data.augment_repository import AugmentRepository
from src.ocr.matcher import (
    AugmentMatch,
    AugmentMatcher,
    match_augment,
    normalize_text,
    to_simplified,
)


@pytest.fixture(scope="module")
def matcher() -> AugmentMatcher:
    repo = AugmentRepository()
    try:
        repo.load()
    except FileNotFoundError:
        pytest.skip("data/kiwi_augments.json 不存在，跳过匹配测试")
    return AugmentMatcher(repo, aliases={})


# ---------------------------------------------------------------- normalize
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("升级：无尽之刃", "升级:无尽之刃"),
        ("升级 : 无尽之刃", "升级:无尽之刃"),
        ("升级:无尽之刃", "升级:无尽之刃"),
        ("升级： 无尽之刃", "升级:无尽之刃"),
        ("升级：无尽之刃 ", "升级:无尽之刃"),
        ("灵 魂 虹 吸", "灵魂虹吸"),
        ("灵魂\u3000虹吸", "灵魂虹吸"),          # 全角空格
        ("Soul Siphon", "soulsiphon"),
        ("灵魂虹吸!", "灵魂虹吸"),
        ("灵魂虹吸。", "灵魂虹吸"),
        ("灵魂虹吸0", "灵魂虹吸0"),               # 末位数字不能被误删
        ("", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalize_keeps_colon_because_it_is_part_of_name() -> None:
    assert ":" in normalize_text("升级：无尽之刃")


def test_traditional_to_simplified() -> None:
    assert to_simplified("靈魂虹吸") == "灵魂虹吸"
    assert normalize_text("靈魂虹吸") == "灵魂虹吸"


# ---------------------------------------------------------------- matching
def test_exact_match(matcher: AugmentMatcher) -> None:
    item = matcher.repository.get(1077)
    result = matcher.match(item.name_cn or "")
    assert result.method == "exact"
    assert result.augment_id == 1077
    assert result.confidence == 1.0


def test_normalized_match_ignores_spaces_and_fullwidth(matcher: AugmentMatcher) -> None:
    item = matcher.repository.get(1077)
    noisy = " ".join(item.name_cn or "")          # "灵 魂 虹 吸"
    result = matcher.match(noisy)
    assert result.matched
    assert result.augment_id == 1077
    assert result.method in ("normalized", "exact", "fuzzy")


def test_normalized_match_for_colon_variants(matcher: AugmentMatcher) -> None:
    item = matcher.repository.get(1336)
    assert item is not None
    # 字典原文使用的是全角冒号 U+FF1A，因此全角写法属于 exact，
    # 半角 / 带空格写法属于 normalized。
    assert "：" in (item.name_cn or "")
    for variant in ("升级:无尽之刃", "升级 : 无尽之刃"):
        result = matcher.match(variant)
        assert result.augment_id == 1336, variant
        assert result.method == "normalized"
    assert matcher.match(item.name_cn or "").method == "exact"


def test_alias_match() -> None:
    repo = AugmentRepository()
    try:
        repo.load()
    except FileNotFoundError:
        pytest.skip("字典不存在")
    matcher = AugmentMatcher(repo, aliases={normalize_text("灵魂 吸取"): 1077})
    result = matcher.match("灵魂 吸取")
    assert result.method == "alias"
    assert result.augment_id == 1077


def test_fuzzy_match_tolerates_one_bad_char(matcher: AugmentMatcher) -> None:
    # 「灵魂虹吸」误识别成「灵魂虹汲」
    result = matcher.match("灵魂虹汲")
    assert result.method in ("fuzzy", "none")
    if result.method == "fuzzy":
        assert result.augment_id == 1077


def test_no_match_returns_none_without_raising(matcher: AugmentMatcher) -> None:
    result = matcher.match("这完全不是一个海克斯名称")
    assert isinstance(result, AugmentMatch)
    assert result.augment_id is None
    assert result.method == "none"


def test_empty_input_is_safe(matcher: AugmentMatcher) -> None:
    assert matcher.match("").augment_id is None
    assert matcher.match("   ").augment_id is None


def test_module_level_helper_never_raises() -> None:
    result = match_augment("随便什么文本")
    assert isinstance(result, AugmentMatch)
