"""远程版本清单的解析与严格校验。

清单由真实发布流程生成（tools/make_release.py），地址放在
`config/config.yaml` 的 `update.manifest_url` 中，禁止在多处散落硬编码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from src.update.version import InvalidVersionError, Version, parse_version

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UPDATE_TYPES = ("full", "incremental")


class ManifestError(ValueError):
    """清单缺失、格式错误或不符合安全要求。"""


def _notes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [line.strip(" -•\t") for line in raw.splitlines() if line.strip()]
    if isinstance(raw, Iterable) and not isinstance(raw, (bytes, dict)):
        out = []
        for item in raw:
            if not isinstance(item, str):
                raise ManifestError("release_notes 的每一项都必须是字符串")
            text = item.strip()
            if text:
                out.append(text)
        return out
    raise ManifestError("release_notes 必须是字符串数组")


def _validate_url(url: Any, allow_http: bool) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ManifestError("缺少 package_url")
    url = url.strip()
    parsed = urlparse(url)
    if allow_http:
        if parsed.scheme not in ("https", "http"):
            raise ManifestError("package_url 必须是 http(s) 地址")
    elif parsed.scheme != "https":
        raise ManifestError("package_url 必须使用 HTTPS（当前为 %r）" % parsed.scheme)
    if not parsed.netloc:
        raise ManifestError("package_url 缺少主机名")
    if parsed.username or parsed.password:
        raise ManifestError("package_url 不允许包含账号密码")
    return url


@dataclass(frozen=True)
class UpdateManifest:
    version: Version
    package_url: str
    sha256: str
    release_notes: list[str] = field(default_factory=list)
    update_type: str = "full"
    minimum_supported_version: Optional[Version] = None
    published_at: Optional[str] = None
    package_size: Optional[int] = None
    #: 增量包适用的旧版本列表（全量包为空）
    from_versions: list[str] = field(default_factory=list)
    #: 增量包不可用时的完整包（自动回退）
    full_package_url: Optional[str] = None
    full_sha256: Optional[str] = None
    full_package_size: Optional[int] = None
    raw: dict = field(default_factory=dict, repr=False)

    # -- 兼容性 -------------------------------------------------------------
    def requires_full_package(self, current: Version) -> bool:
        """当前版本低于最低支持版本时，必须整包升级。"""
        minimum = self.minimum_supported_version
        return bool(minimum and current < minimum)

    def can_apply_incrementally(self, current: Version) -> bool:
        if self.update_type != "incremental":
            return False
        if self.requires_full_package(current):
            return False
        if not self.from_versions:
            return False
        return str(current) in self.from_versions

    def as_full_package(self) -> Optional["UpdateManifest"]:
        """返回对应的完整包清单（没有配置则 None）。"""
        if not self.full_package_url or not self.full_sha256:
            return None
        return replace(self, package_url=self.full_package_url,
                       sha256=self.full_sha256,
                       package_size=self.full_package_size,
                       update_type="full", from_versions=[])


def parse_manifest(payload: Any, *, allow_http: bool = False) -> UpdateManifest:
    """把远程 JSON 解析成 UpdateManifest；任何不安全/缺失字段都抛 ManifestError。"""
    if not isinstance(payload, dict):
        raise ManifestError("版本清单根节点必须是对象")

    if "version" not in payload:
        raise ManifestError("缺少 version")
    try:
        version = parse_version(str(payload["version"]))
    except InvalidVersionError as exc:
        raise ManifestError(str(exc)) from exc

    package_url = _validate_url(payload.get("package_url"), allow_http=allow_http)

    sha = payload.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.match(sha.strip()):
        raise ManifestError("sha256 必须是 64 位十六进制字符串")

    update_type = str(payload.get("update_type") or "full").strip().lower()
    if update_type not in UPDATE_TYPES:
        raise ManifestError("update_type 只能是 %s" % (UPDATE_TYPES,))

    minimum = payload.get("minimum_supported_version")
    minimum_version = None
    if minimum not in (None, ""):
        try:
            minimum_version = parse_version(str(minimum))
        except InvalidVersionError as exc:
            raise ManifestError("minimum_supported_version: %s" % exc) from exc

    size = payload.get("package_size")
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise ManifestError("package_size 必须是整数") from exc
        if size < 0:
            raise ManifestError("package_size 不能为负数")

    from_versions: list[str] = []
    raw_from = payload.get("from_versions")
    if raw_from is not None:
        if not isinstance(raw_from, list):
            raise ManifestError("from_versions 必须是字符串数组")
        for item in raw_from:
            try:
                from_versions.append(str(parse_version(str(item))))
            except InvalidVersionError as exc:
                raise ManifestError("from_versions: %s" % exc) from exc

    full_url = payload.get("full_package_url")
    full_sha = payload.get("full_sha256")
    full_size = payload.get("full_package_size")
    if full_url is not None:
        full_url = _validate_url(full_url, allow_http=allow_http)
        if not isinstance(full_sha, str) or not _SHA256_RE.match(full_sha.strip()):
            raise ManifestError("full_sha256 必须是 64 位十六进制字符串")
        full_sha = full_sha.strip().lower()
        if full_size is not None:
            try:
                full_size = int(full_size)
            except (TypeError, ValueError) as exc:
                raise ManifestError("full_package_size 必须是整数") from exc
            if full_size < 0:
                raise ManifestError("full_package_size 不能为负数")

    return UpdateManifest(
        version=version,
        package_url=package_url,
        sha256=sha.strip().lower(),
        release_notes=_notes(payload.get("release_notes")),
        update_type=update_type,
        minimum_supported_version=minimum_version,
        published_at=(str(payload["published_at"])
                      if payload.get("published_at") else None),
        package_size=size,
        from_versions=from_versions,
        full_package_url=full_url,
        full_sha256=full_sha,
        full_package_size=full_size,
        raw=dict(payload),
    )


def format_size(num_bytes: Optional[int]) -> str:
    """人可读的大小（用于弹窗）。"""
    if num_bytes is None:
        return "未知"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%.0f %s" % (value, unit) if unit == "B"
                    else "%.1f %s" % (value, unit))
        value /= 1024
    return "%.1f GB" % value


__all__ = ["UpdateManifest", "ManifestError", "parse_manifest", "format_size",
           "UPDATE_TYPES"]
