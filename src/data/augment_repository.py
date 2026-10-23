"""腾讯 KIWI 海克斯字典仓库。

数据来源：
    https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json
    data/kiwi_augments.json 保存腾讯最新条目，并保留已被腾讯下架但历史版本
    仍可能引用的旧 ID。使用 tools/update_kiwi_dictionary.py 可安全合并更新。

字段：augmentID / name_en / name_cn / mode / level / desc / tooltip / large_Icon / small_Icon
    level : kSilver / kGold / kPrismatic
    mode  : 逗号分隔的模式标记
            KIWI       = ARAM: Mayhem
            KIWI_JADE  = 另一个模式（不属于本项目）
            CHERRY     = Arena

只保留「ARAM: Mayhem 有效」的海克斯：mode 中包含 KIWI 标记。
（实测 248 条里 KIWI_JADE-only 25 条、空 mode 5 条应被排除。）
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Optional

from src.core.models import AugmentInfo
from src.utils import paths
from src.utils.logger import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_raw_path() -> Path:
    """字典是只读资源；若用户目录下有覆盖版本则优先。"""
    return paths.augment_dict_path()

#: 本项目关心的模式标记
MAYHEM_MODE = "KIWI"
KNOWN_MODES = ("KIWI", "KIWI_JADE", "CHERRY")


def parse_raw_dict(payload: list[dict]) -> list[AugmentInfo]:
    """把原始 JSON 数组转换成 AugmentInfo（字段名原样映射）。"""
    items: list[AugmentInfo] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        augment_id = row.get("augmentID")
        if not isinstance(augment_id, int):
            continue
        items.append(AugmentInfo(
            augment_id=augment_id,
            name_cn=row.get("name_cn") or None,
            name_en=row.get("name_en") or None,
            level=row.get("level") or None,
            mode=row.get("mode") or None,
            desc=row.get("desc") or None,
            tooltip=row.get("tooltip") or None,
            icon=row.get("large_Icon") or row.get("small_Icon") or None,
            is_pbe=bool(row.get("isPBE")),
            is_new=bool(row.get("isNew")),
        ))
    return items


def mode_distribution(items: Iterable[AugmentInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.mode or ""] = counts.get(item.mode or "", 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


class AugmentRepository:
    """内存字典：id 索引 + 名称索引。"""

    def __init__(self, raw_path: str | Path | None = None,
                 mode: str = MAYHEM_MODE) -> None:
        self._merge_bundled = raw_path is None
        self.raw_path = Path(raw_path) if raw_path else default_raw_path()
        self.mode = mode
        self.all_items: list[AugmentInfo] = []
        self.items: list[AugmentInfo] = []
        self._by_id: dict[int, AugmentInfo] = {}
        self.loaded = False

    # -- 载入 ---------------------------------------------------------------
    def load(self, require_file: bool = True) -> "AugmentRepository":
        if self.loaded:
            return self
        if not self.raw_path.exists():
            if require_file:
                raise FileNotFoundError(f"海克斯字典不存在: {self.raw_path}")
            logger.warning("海克斯字典不存在: %s", self.raw_path)
            self.loaded = True
            return self

        payload = self._read_payload(self.raw_path)
        if not isinstance(payload, list):
            raise ValueError("kiwi_augments.json 根节点应为数组")

        # 正式安装可能保留旧版迁移到用户目录的字典。若只读它，新补丁新增的
        # 海克斯会永久无法匹配。合并时让随程序发布的字典覆盖同 ID 字段，
        # 同时保留用户文件里额外的 ID（可作为未来补丁的前向补充）。
        if self._merge_bundled:
            bundled_path = paths.bundled_augment_dict_path()
            if bundled_path.exists() and bundled_path.resolve() != self.raw_path.resolve():
                bundled = self._read_payload(bundled_path)
                if isinstance(bundled, list):
                    user_count = len(payload)
                    merged: OrderedDict[int, dict] = OrderedDict()
                    for row in payload:
                        try:
                            merged[int(row["augmentID"])] = row
                        except (KeyError, TypeError, ValueError):
                            continue
                    for row in bundled:
                        try:
                            merged[int(row["augmentID"])] = row
                        except (KeyError, TypeError, ValueError):
                            continue
                    payload = [merged[augment_id] for augment_id in sorted(merged)]
                    logger.info("合并用户字典与内置字典: 用户 %d + 内置 %d -> %d 条",
                                user_count, len(bundled), len(payload))

        self.all_items = parse_raw_dict(payload)
        self.items = [item for item in self.all_items if item.in_mode(self.mode)]
        self._by_id = {item.augment_id: item for item in self.items}
        self.loaded = True
        logger.info("海克斯字典载入: 原始 %d 条, %s 有效 %d 条 (%s)",
                    len(self.all_items), self.mode, len(self.items), self.raw_path.name)
        return self

    @staticmethod
    def _read_payload(path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # -- 查询 ---------------------------------------------------------------
    def get(self, augment_id: int) -> Optional[AugmentInfo]:
        self.load()
        return self._by_id.get(augment_id)

    def ids(self) -> set[int]:
        self.load()
        return set(self._by_id)

    def by_level(self) -> dict[str, list[AugmentInfo]]:
        self.load()
        out: dict[str, list[AugmentInfo]] = {}
        for item in self.items:
            out.setdefault(item.level or "unknown", []).append(item)
        return out

    def get_by_name(self, name: str) -> Optional[AugmentInfo]:
        """按 name_cn / name_en 精确查找（大小写不敏感）。"""
        self.load()
        target = (name or "").strip().lower()
        if not target:
            return None
        for item in self.items:
            if (item.name_cn or "").strip().lower() == target:
                return item
            if (item.name_en or "").strip().lower() == target:
                return item
        return None

    def __len__(self) -> int:
        self.load()
        return len(self.items)


__all__ = [
    "AugmentRepository",
    "AugmentInfo",
    "parse_raw_dict",
    "mode_distribution",
    "default_raw_path",
    "MAYHEM_MODE",
    "KNOWN_MODES",
]
