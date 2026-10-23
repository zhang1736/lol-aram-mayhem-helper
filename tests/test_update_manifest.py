"""版本清单严格校验（安全要求：HTTPS、sha256、字段完整）。"""

from __future__ import annotations

import pytest

from src.update.manifest import ManifestError, format_size, parse_manifest

VALID = {
    "version": "1.0.2",
    "minimum_supported_version": "1.0.0",
    "release_notes": ["修复多局失效", "优化浮层"],
    "published_at": "2026-10-23",
    "update_type": "incremental",
    "package_url": "https://github.com/example/repo/releases/download/v1.0.2/inc.zip",
    "sha256": "a" * 64,
    "package_size": 1024,
    "from_versions": ["1.0.1"],
}


def test_valid_manifest() -> None:
    manifest = parse_manifest(VALID)
    assert str(manifest.version) == "1.0.2"
    assert manifest.release_notes == ["修复多局失效", "优化浮层"]
    assert manifest.package_size == 1024
    assert manifest.can_apply_incrementally(_v("1.0.1")) is True


def _v(text):
    from src.update.version import parse_version

    return parse_version(text)


def test_incremental_rejected_for_unsupported_old_version() -> None:
    manifest = parse_manifest(VALID)
    assert manifest.can_apply_incrementally(_v("0.9.9")) is False


def test_minimum_supported_forces_full() -> None:
    payload = dict(VALID, minimum_supported_version="1.0.1")
    manifest = parse_manifest(payload)
    assert manifest.requires_full_package(_v("1.0.0")) is True
    assert manifest.can_apply_incrementally(_v("1.0.0")) is False


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("version"),
    lambda p: p.pop("package_url"),
    lambda p: p.update(package_url="http://example.com/x.zip"),
    lambda p: p.update(package_url="https://user:pass@example.com/x.zip"),
    lambda p: p.update(sha256="xyz"),
    lambda p: p.update(release_notes="not-a-list-but-ok-string"),
    lambda p: p.update(release_notes=[1, 2]),
    lambda p: p.update(update_type="binary-diff"),
    lambda p: p.update(package_size=-5),
    lambda p: p.update(version="1.0"),
    lambda p: p.update(minimum_supported_version="bad"),
])
def test_invalid_manifests(mutate) -> None:
    payload = dict(VALID)
    mutate(payload)
    if payload.get("release_notes") == "not-a-list-but-ok-string":
        # 字符串形式的日志是允许的（按行切分）
        manifest = parse_manifest(payload)
        assert manifest.release_notes == ["not-a-list-but-ok-string"]
        return
    with pytest.raises(ManifestError):
        parse_manifest(payload)


def test_http_allowed_only_for_tests() -> None:
    payload = dict(VALID, package_url="http://127.0.0.1:1/x.zip")
    with pytest.raises(ManifestError):
        parse_manifest(payload)
    manifest = parse_manifest(payload, allow_http=True)
    assert manifest.package_url.startswith("http://127.0.0.1")


def test_format_size() -> None:
    assert format_size(None) == "未知"
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
