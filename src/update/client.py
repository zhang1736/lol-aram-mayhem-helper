"""版本清单检查（HTTPS + 严格校验，任何失败都只返回状态，不抛到 UI 线程）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx

from src.opgg.client import verify_option
from src.update.manifest import ManifestError, UpdateManifest, parse_manifest
from src.update.version import InvalidVersionError, is_newer, parse_version
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

DEFAULT_TIMEOUT = 15.0
MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass
class UpdateCheckResult:
    status: str                      # update / latest / error / disabled
    manifest: Optional[UpdateManifest] = None
    error: Optional[str] = None
    current_version: str = ""
    raw_fields: dict = field(default_factory=dict)

    @property
    def has_update(self) -> bool:
        return self.status == "update" and self.manifest is not None


def _check_url(url: str, allow_http: bool) -> None:
    parsed = urlparse(url)
    if allow_http:
        if parsed.scheme not in ("https", "http"):
            raise ManifestError("更新源必须是 http(s) 地址")
    elif parsed.scheme != "https":
        raise ManifestError("更新源必须使用 HTTPS")
    if not parsed.netloc:
        raise ManifestError("更新源缺少主机名")


class UpdateClient:
    """读取远程版本清单。"""

    def __init__(self, manifest_url: str, timeout: float = DEFAULT_TIMEOUT,
                 allow_http: bool = False) -> None:
        self.manifest_url = (manifest_url or "").strip()
        self.timeout = float(timeout)
        self.allow_http = allow_http

    async def fetch_manifest(self) -> UpdateManifest:
        if not self.manifest_url:
            raise ManifestError("未配置更新源（config.yaml: update.manifest_url）")
        _check_url(self.manifest_url, self.allow_http)
        logger.info("检查更新: %s", self.manifest_url)
        try:
            async with httpx.AsyncClient(
                    verify=verify_option(), timeout=self.timeout,
                    headers={"User-Agent": "LOLHexHelper-Updater",
                             "Accept": "application/json"}) as client:
                response = await client.get(self.manifest_url)
        except httpx.HTTPError as exc:
            raise ManifestError("网络错误: %s: %s" % (type(exc).__name__, exc)) from exc

        logger.info("版本清单响应: HTTP %s", response.status_code)
        if response.status_code != 200:
            raise ManifestError("版本清单返回 HTTP %s" % response.status_code)
        if len(response.content) > MAX_MANIFEST_BYTES:
            raise ManifestError("版本清单过大（>1MB），拒绝解析")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ManifestError("版本清单不是合法 JSON") from exc
        return parse_manifest(payload, allow_http=self.allow_http)


async def check_for_update(current_version: str, manifest_url: str,
                           timeout: float = DEFAULT_TIMEOUT,
                           allow_http: bool = False) -> UpdateCheckResult:
    """检查是否有新版本；永不抛异常（UI 线程只读结果）。"""
    try:
        current = parse_version(current_version)
    except InvalidVersionError as exc:
        logger.error("本地版本号不合法: %s", exc)
        return UpdateCheckResult("error", error="本地版本号不合法", current_version=current_version)

    if not (manifest_url or "").strip():
        logger.info("未配置更新源，跳过检查")
        return UpdateCheckResult("disabled", current_version=current_version,
                                 error="未配置更新源")

    try:
        manifest = await UpdateClient(manifest_url, timeout=timeout,
                                      allow_http=allow_http).fetch_manifest()
    except (ManifestError, httpx.HTTPError) as exc:
        message = str(exc)
        logger.warning("检查更新失败: %s", message)
        return UpdateCheckResult("error", error=message, current_version=current_version)
    except Exception as exc:  # noqa: BLE001 - 更新检查绝不影响主程序
        logger.warning("检查更新异常: %s", exc)
        return UpdateCheckResult("error", error="检查更新失败: %s" % exc,
                                 current_version=current_version)

    if is_newer(manifest.version, current):
        logger.info("发现新版本: %s（当前 %s，%s 包）",
                    manifest.version, current, manifest.update_type)
        return UpdateCheckResult("update", manifest=manifest,
                                 current_version=current_version)
    logger.info("当前已是最新版本（%s，远端 %s）", current, manifest.version)
    return UpdateCheckResult("latest", manifest=manifest,
                             current_version=current_version)


__all__ = ["UpdateClient", "UpdateCheckResult", "check_for_update",
           "DEFAULT_TIMEOUT"]
