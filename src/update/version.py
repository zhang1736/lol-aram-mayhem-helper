"""语义化版本比较（严格实现，禁止比较字符串或浮点数）。

必须正确区分：
    1.0.10 > 1.0.9      （字符串比较会得出相反结论）
    1.0.1  > 1.0.1-rc1  （正式版高于预发布）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Optional

_VERSION_RE = re.compile(
    r"^[vV]?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)


class InvalidVersionError(ValueError):
    pass


def _pre_key(prerelease: str) -> tuple:
    """semver 预发布优先级：数字标识按数值比较，且低于字母标识。"""
    key: list = []
    for part in prerelease.split("."):
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: Optional[str] = None

    @classmethod
    def parse(cls, text: str) -> "Version":
        raw = (text or "").strip()
        match = _VERSION_RE.match(raw)
        if not match:
            raise InvalidVersionError("版本号格式不正确: %r（应形如 1.0.1）" % text)
        return cls(int(match.group("major")), int(match.group("minor")),
                   int(match.group("patch")), match.group("pre"))

    def __str__(self) -> str:
        base = "%d.%d.%d" % (self.major, self.minor, self.patch)
        return "%s-%s" % (base, self.prerelease) if self.prerelease else base

    def _cmp_key(self):
        # 正式版 > 任何预发布：没有 pre 时用 (1,)，有 pre 时用 (0, ...)
        pre = (1,) if not self.prerelease else (0, _pre_key(self.prerelease))
        return (self.major, self.minor, self.patch, pre)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp_key() == other._cmp_key()

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp_key() < other._cmp_key()


def parse_version(text: str) -> Version:
    return Version.parse(text)


def is_newer(remote: str | Version, local: str | Version) -> bool:
    """远程版本是否比本地新。"""
    remote_v = remote if isinstance(remote, Version) else parse_version(remote)
    local_v = local if isinstance(local, Version) else parse_version(local)
    return remote_v > local_v


__all__ = ["Version", "InvalidVersionError", "parse_version", "is_newer"]
