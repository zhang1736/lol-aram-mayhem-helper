"""语义化版本比较：必须正确区分 1.0.10 / 1.0.9，预发布低于正式版。"""

from __future__ import annotations

import pytest

from src.update.version import InvalidVersionError, is_newer, parse_version


@pytest.mark.parametrize(("a", "b", "expected"), [
    ("1.0.10", "1.0.9", True),
    ("1.0.9", "1.0.10", False),
    ("1.0.1", "1.0.1", False),
    ("1.1.0", "1.0.99", True),
    ("2.0.0", "1.9.9", True),
    ("1.0.1-beta.2", "1.0.1", False),     # 预发布 < 正式版
    ("1.0.1", "1.0.1-beta.2", True),
    ("1.0.1-beta.10", "1.0.1-beta.9", True),
    ("v1.0.2", "1.0.1", True),
])
def test_semver_compare(a, b, expected) -> None:
    assert is_newer(a, b) is expected


def test_parse_normalizes() -> None:
    assert str(parse_version("v1.02.003")) == "1.2.3"
    assert parse_version("1.0.1-rc.1").prerelease == "rc.1"


@pytest.mark.parametrize("bad", ["", "1", "1.0", "1.0.0.0", "abc", "1.0.x"])
def test_invalid_versions(bad: str) -> None:
    with pytest.raises(InvalidVersionError):
        parse_version(bad)
