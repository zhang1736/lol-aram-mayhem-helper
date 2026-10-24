"""用随新版本携带的 Updater 更新安装目录中的旧 Updater。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from src.update.checksum import sha256_file
from src.utils import paths
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)


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
        if (staged.stat().st_size == target.stat().st_size
                and sha256_file(staged) == sha256_file(target)):
            return target
        temp = target.with_name(target.name + ".new")
        shutil.copy2(staged, temp)
        os.replace(temp, target)
        logger.info("已更新内置 Updater.exe: %s", target)
        return target
    except Exception as exc:  # noqa: BLE001 - 下次更新可从临时副本运行旧 Updater
        logger.warning("更新内置 Updater.exe 失败（不影响主程序）: %s", exc)
        return None


__all__ = ["ensure_embedded_updater"]
