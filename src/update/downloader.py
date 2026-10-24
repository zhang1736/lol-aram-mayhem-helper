"""更新包下载：流式写入临时目录 + 大小/SHA-256 校验（失败不留半包）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from src.opgg.client import verify_option
from src.update.checksum import sha256_file
from src.update.manifest import UpdateManifest
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

CHUNK_SIZE = 1024 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024      # 2GB 上限，防止异常清单
DEFAULT_ATTEMPTS = 3


class DownloadError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False,
                 fallback_allowed: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed


@dataclass
class DownloadProgress:
    downloaded: int = 0
    total: Optional[int] = None
    speed_bps: float = 0.0

    @property
    def percent(self) -> Optional[float]:
        if not self.total:
            return None
        return min(100.0, self.downloaded * 100.0 / self.total)


async def download_package(manifest: UpdateManifest, dest_dir: Path,
                           on_progress: Optional[Callable[[DownloadProgress], None]] = None,
                           cancel: Optional[threading.Event] = None,
                           timeout: float = 30.0,
                           attempts: int = DEFAULT_ATTEMPTS) -> Path:
    """下载并校验更新包；任何失败都会删除 .part 文件。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / ("update-%s.zip" % manifest.version)
    part = target.with_name(target.name + ".part")

    expected = manifest.package_size
    if expected and expected > MAX_PACKAGE_BYTES:
        raise DownloadError("更新包超过 2GB，拒绝下载")
    if expected:
        free = shutil.disk_usage(dest_dir).free
        if free < expected * 2 + 50 * 1024 * 1024:
            raise DownloadError("磁盘空间不足（需要约 %.0f MB）" % (expected * 2 / 1024 / 1024))

    parsed = urlparse(manifest.package_url)
    asset_name = Path(parsed.path).name or "unknown"
    logger.info("开始下载 type=%s version=%s asset=%s host=%s expected_bytes=%s",
                manifest.update_type, manifest.version, asset_name,
                parsed.netloc, expected)

    if target.exists():
        try:
            if ((expected is None or target.stat().st_size == expected)
                    and sha256_file(target).lower() == manifest.sha256.lower()):
                logger.info("复用已下载且校验通过的更新包: %s", target)
                return target
        except OSError:
            pass
        target.unlink(missing_ok=True)

    started = time.monotonic()
    last_error: Optional[DownloadError] = None
    for attempt in range(1, max(1, attempts) + 1):
        last_error = None
        downloaded = part.stat().st_size if part.exists() else 0
        last_report = 0.0
        logger.info("下载尝试 %d/%d，已有 %d/%s 字节", attempt,
                    max(1, attempts), downloaded, expected or "?")
        try:
            headers = {"Range": "bytes=%d-" % downloaded} if downloaded else {}
            async with httpx.AsyncClient(verify=verify_option(), timeout=timeout,
                                         follow_redirects=True) as client:
                async with client.stream("GET", manifest.package_url,
                                         headers=headers) as response:
                    if response.status_code in (404, 410):
                        raise DownloadError(
                            "下载失败：HTTP %s（资产不存在）" % response.status_code,
                            fallback_allowed=True)
                    if response.status_code == 416 and expected and downloaded == expected:
                        pass
                    elif response.status_code not in (200, 206):
                        raise DownloadError(
                            "下载失败：HTTP %s" % response.status_code,
                            retryable=response.status_code >= 500)
                    else:
                        if response.status_code == 200 and downloaded:
                            downloaded = 0
                        mode = "ab" if downloaded else "wb"
                        header_size = response.headers.get("content-length")
                        remaining = (int(header_size)
                                     if header_size and header_size.isdigit() else None)
                        total = (downloaded + remaining if remaining is not None
                                 else expected)
                        if total and total > MAX_PACKAGE_BYTES:
                            raise DownloadError("更新包超过 2GB，拒绝下载")
                        with open(part, mode) as fh:
                            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                                if cancel is not None and cancel.is_set():
                                    raise DownloadError("下载已取消")
                                if not chunk:
                                    continue
                                fh.write(chunk)
                                downloaded += len(chunk)
                                if downloaded > MAX_PACKAGE_BYTES:
                                    raise DownloadError("更新包超过 2GB，拒绝下载")
                                now = time.monotonic()
                                if on_progress is not None and (
                                        now - last_report >= 0.15
                                        or (total and downloaded >= total)):
                                    last_report = now
                                    speed = downloaded / max(0.001, now - started)
                                    on_progress(DownloadProgress(
                                        downloaded, total, speed))
        except DownloadError as exc:
            last_error = exc
            if not exc.retryable or attempt >= max(1, attempts):
                break
        except Exception as exc:  # noqa: BLE001
            last_error = DownloadError(
                "下载中断：%s: %s" % (type(exc).__name__, exc),
                retryable=True)
        if last_error is not None and last_error.retryable and attempt < attempts:
            delay = min(3.0, 0.5 * attempt)
            logger.warning("%s；%.1fs 后重试", last_error, delay)
            await asyncio.sleep(delay)
            continue
        break

    if last_error is not None:
        _cleanup(part)
        raise last_error

    try:
        actual_size = part.stat().st_size
        if expected and actual_size != expected:
            raise DownloadError(
                "更新包大小不符（预期 %s，实际 %s）" % (expected, actual_size),
                fallback_allowed=True)
        digest = sha256_file(part)
        if digest.lower() != manifest.sha256.lower():
            raise DownloadError(
                "更新包 SHA-256 校验失败（文件可能损坏或被篡改）",
                fallback_allowed=True)
        os.replace(part, target)
    except DownloadError:
        _cleanup(part)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup(part)
        raise DownloadError("校验更新包失败：%s" % exc) from exc

    elapsed = max(0.001, time.monotonic() - started)
    logger.info("更新包下载完成 asset=%s bytes=%s elapsed=%.1fs avg_bps=%.0f",
                asset_name, target.stat().st_size, elapsed,
                target.stat().st_size / elapsed)
    if on_progress is not None:
        on_progress(DownloadProgress(target.stat().st_size, target.stat().st_size,
                                     target.stat().st_size / max(0.001, time.monotonic() - started)))
    return target


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def download_sync(manifest: UpdateManifest, dest_dir: Path,
                  on_progress=None, cancel=None, timeout: float = 30.0) -> Path:
    """给后台线程用的同步入口。"""
    return asyncio.run(download_package(manifest, dest_dir, on_progress, cancel, timeout))


__all__ = ["DownloadProgress", "DownloadError", "download_package", "download_sync",
           "sha256_file", "MAX_PACKAGE_BYTES"]
