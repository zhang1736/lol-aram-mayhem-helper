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

import httpx

from src.opgg.client import verify_option
from src.update.checksum import sha256_file
from src.update.manifest import UpdateManifest
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

CHUNK_SIZE = 256 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024      # 2GB 上限，防止异常清单


class DownloadError(RuntimeError):
    pass


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
                           timeout: float = 30.0) -> Path:
    """下载并校验更新包；任何失败都会删除 .part 文件。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / ("update-%s.zip" % manifest.version)
    part = target.with_name(target.name + ".part")
    if part.exists():
        part.unlink()

    expected = manifest.package_size
    if expected and expected > MAX_PACKAGE_BYTES:
        raise DownloadError("更新包超过 2GB，拒绝下载")
    if expected:
        free = shutil.disk_usage(dest_dir).free
        if free < expected * 2 + 50 * 1024 * 1024:
            raise DownloadError("磁盘空间不足（需要约 %.0f MB）" % (expected * 2 / 1024 / 1024))

    logger.info("开始下载更新包: %s（预期 %s 字节）", manifest.version, expected)
    downloaded = 0
    started = time.monotonic()
    last_report = 0.0
    try:
        async with httpx.AsyncClient(verify=verify_option(), timeout=timeout,
                                     follow_redirects=True) as client:
            async with client.stream("GET", manifest.package_url) as response:
                if response.status_code != 200:
                    raise DownloadError("下载失败：HTTP %s" % response.status_code)
                header_size = response.headers.get("content-length")
                total = int(header_size) if header_size and header_size.isdigit() else expected
                if total and total > MAX_PACKAGE_BYTES:
                    raise DownloadError("更新包超过 2GB，拒绝下载")
                with open(part, "wb") as fh:
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
                        if on_progress is not None and (now - last_report >= 0.15 or
                                                        (total and downloaded >= total)):
                            last_report = now
                            speed = downloaded / max(0.001, now - started)
                            on_progress(DownloadProgress(downloaded, total, speed))
    except DownloadError:
        _cleanup(part)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup(part)
        raise DownloadError("下载中断：%s: %s" % (type(exc).__name__, exc)) from exc

    try:
        actual_size = part.stat().st_size
        if expected and actual_size != expected:
            raise DownloadError("更新包大小不符（预期 %s，实际 %s）" % (expected, actual_size))
        digest = sha256_file(part)
        if digest.lower() != manifest.sha256.lower():
            raise DownloadError("更新包 SHA-256 校验失败（文件可能损坏或被篡改）")
        os.replace(part, target)
    except DownloadError:
        _cleanup(part)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup(part)
        raise DownloadError("校验更新包失败：%s" % exc) from exc

    logger.info("更新包下载完成并通过校验: %s（%s 字节）", target, target.stat().st_size)
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
