"""新版启动健康握手。

Updater 启动新程序前设置两个环境变量：
    LOL_HEX_HELPER_UPDATE_TOKEN
    LOL_HEX_HELPER_UPDATE_MARKER_DIR

新程序真正进入启动流程后写入带版本号、PID 和 EXE 路径的 JSON。Updater
等待该文件出现并核对版本，避免仅凭 Popen 成功就误判更新完成。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from src.utils import paths
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

TOKEN_ENV = "LOL_HEX_HELPER_UPDATE_TOKEN"
MARKER_DIR_ENV = "LOL_HEX_HELPER_UPDATE_MARKER_DIR"


def marker_path(marker_dir: str | Path, token: str) -> Path:
    return Path(marker_dir) / ("startup-%s.json" % token)


def write_startup_marker(version: str) -> Optional[Path]:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    marker_dir = (os.environ.get(MARKER_DIR_ENV) or "").strip()
    if not token or not marker_dir:
        return None
    try:
        target = marker_path(marker_dir, token)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": str(version),
            "pid": os.getpid(),
            "exe": str(Path(os.sys.executable).resolve()),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_data_dir": str(paths.user_data_dir()),
        }
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, target)
        logger.info("已写入新版启动握手: %s", target)
        return target
    except Exception as exc:  # noqa: BLE001 - 握手失败不能阻止程序启动
        logger.warning("写入新版启动握手失败: %s", exc)
        return None


__all__ = ["TOKEN_ENV", "MARKER_DIR_ENV", "marker_path",
           "write_startup_marker"]
