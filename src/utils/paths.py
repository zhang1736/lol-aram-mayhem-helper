"""路径解析：区分「只读资源目录」与「用户可写数据目录」。

打包后不能依赖开发目录 D:\\lol海克斯选择：
    resource_dir()  只读资源（data/kiwi_augments.json、OCR 模型、默认 config）
                    - 开发态：项目根目录
                    - 打包态：PyInstaller 解包目录 (sys._MEIPASS)
    user_data_dir() 用户可写数据（SQLite / logs / user_config.yaml）
                    - 固定 %LOCALAPPDATA%\\LOLHexHelper
                    - 绝不写 Program Files
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "LOLHexHelper"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """开发态的项目根目录（src/utils/paths.py -> 上溯三层）。"""
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源目录。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return project_root()


def user_data_dir() -> Path:
    """用户可写数据目录（不存在则创建）。"""
    base = os.environ.get("LOL_HEX_HELPER_HOME")
    if base:
        path = Path(base)
    else:
        local = os.environ.get("LOCALAPPDATA")
        path = (Path(local) / APP_DIR_NAME) if local else (Path.home() / "." + APP_DIR_NAME)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_user_subdirs() -> dict[str, Path]:
    root = user_data_dir()
    dirs = {
        "root": root,
        "config": root / "config",
        "logs": root / "logs",
        "data": root / "data",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def user_config_path() -> Path:
    return user_data_dir() / "config" / "user_config.yaml"


def database_path() -> Path:
    return user_data_dir() / "aram_helper.db"


def log_path() -> Path:
    return user_data_dir() / "logs" / "app.log"


def default_config_path() -> Path:
    """默认配置：优先打包资源里的 config/config.yaml。"""
    return resource_dir() / "config" / "config.yaml"


def resource_path(*parts: str) -> Path:
    """只读资源；用户目录下若有同名覆盖则优先使用。"""
    override = user_data_dir().joinpath(*parts)
    if override.exists():
        return override
    return resource_dir().joinpath(*parts)


def augment_dict_path() -> Path:
    return resource_path("data", "kiwi_augments.json")


def bundled_augment_dict_path() -> Path:
    """随程序发布的腾讯字典（不受用户旧缓存覆盖）。"""
    return resource_dir() / "data" / "kiwi_augments.json"


def augment_alias_path() -> Path:
    return resource_path("data", "augment_aliases.json")


def migrate_legacy_data(legacy_root: Path | None = None) -> list[str]:
    """把开发目录里的旧 SQLite 复制到用户目录（只复制一次，不覆盖）。"""
    moved: list[str] = []
    legacy = legacy_root or project_root()
    pairs = [
        (legacy / "data" / "aram_helper.db", database_path()),
        (legacy / "data" / "kiwi_augments.json", user_data_dir() / "data" / "kiwi_augments.json"),
    ]
    for source, target in pairs:
        try:
            if source.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                moved.append(str(target))
        except OSError:
            continue
    return moved


__all__ = [
    "APP_DIR_NAME", "is_frozen", "project_root", "resource_dir", "user_data_dir",
    "ensure_user_subdirs", "user_config_path", "database_path", "log_path",
    "default_config_path", "resource_path", "augment_dict_path",
    "bundled_augment_dict_path", "augment_alias_path", "migrate_legacy_data",
]
