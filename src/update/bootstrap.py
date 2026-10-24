"""用随新版本携带的 Updater 更新安装目录中的旧 Updater。"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from src.update.checksum import sha256_file
from src.utils import paths
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)
_RETRY_LOCK = threading.Lock()
_RETRY_THREAD: Optional[threading.Thread] = None


def _replace_staged_updater(target: Path, staged: Path) -> Path:
    if (staged.stat().st_size == target.stat().st_size
            and sha256_file(staged) == sha256_file(target)):
        return target
    temp = target.with_name(target.name + ".new")
    shutil.copy2(staged, temp)
    os.replace(temp, target)
    return target


def _retry_until_unlocked(target: Path, staged: Path) -> None:
    for _ in range(40):
        time.sleep(1.0)
        try:
            _replace_staged_updater(target, staged)
            logger.info("已延迟更新内置 Updater.exe: %s", target)
            return
        except Exception:  # noqa: BLE001 - 仍被旧 Updater 占用，继续等待
            continue
    logger.warning("延迟更新 Updater.exe 超时；现有旧 Updater 仍可安全地从临时副本运行")


def ensure_embedded_updater() -> Optional[Path]:
    """若 _internal 中存在新版 Updater，则安全替换安装根目录的旧版。

    这一步发生在新版主程序启动时；旧 Updater 已经是从安装目录外的临时副本
    启动，因此不会锁住目标文件。更新失败只记录日志，不影响主程序运行。
    """
    if not getattr(sys, "frozen", False):
        return None
    target = Path(sys.executable).resolve().parent / "Updater.exe"
    staged = paths.resource_dir() / "Updater.exe"
    if not staged.exists() or not target.exists():
        return None
    try:
        _replace_staged_updater(target, staged)
        logger.info("已更新内置 Updater.exe: %s", target)
        return target
    except Exception as exc:  # noqa: BLE001 - 下次更新可从临时副本运行旧 Updater
        logger.warning("更新内置 Updater.exe 暂时失败，后台重试: %s", exc)
        global _RETRY_THREAD
        with _RETRY_LOCK:
            if _RETRY_THREAD is None or not _RETRY_THREAD.is_alive():
                _RETRY_THREAD = threading.Thread(
                    target=_retry_until_unlocked, args=(target, staged),
                    name="updater-bootstrap", daemon=True)
                _RETRY_THREAD.start()
        return None


__all__ = ["ensure_embedded_updater"]
