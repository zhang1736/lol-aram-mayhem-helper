"""OCR 文本 -> augmentId 匹配。

链路：OCR -> 文本标准化 -> 匹配 -> augmentId -> 本地数据

匹配优先级（严格）：
    1. exact     原文完全一致
    2. normalized 标准化后一致
    3. alias     命中所维护的 OCR 别名表（只记录真实出现过的错误）
    4. fuzzy     difflib 相似度 >= 阈值
    5. none      返回 augment_id = None，绝不抛异常

字典来源：data/kiwi_augments.json（腾讯 KIWI，原始响应未修改）
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from src.core.models import AugmentInfo
from src.data.augment_repository import AugmentRepository
from src.utils.logger import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_alias_path() -> Path:
    from src.utils import paths

    return paths.augment_alias_path()

FUZZY_THRESHOLD = 0.80

#: 常见繁体 -> 简体（只覆盖海克斯名称里可能出现的字，可继续补）
TRADITIONAL_TO_SIMPLIFIED = {
    "靈": "灵", "魂": "魂", "擊": "击", "質": "质", "變": "变", "無": "无",
    "階": "阶", "龍": "龙", "劍": "剑", "與": "与", "個": "个", "為": "为",
    "這": "这", "會": "会", "來": "来", "時": "时", "傷": "伤", "護": "护",
    "點": "点", "術": "术", "強": "强", "體": "体", "數": "数", "團": "团",
    "隊": "队", "對": "对", "從": "从", "實": "实", "現": "现", "開": "开",
    "關": "关", "過": "过", "進": "进", "選": "选", "擇": "择", "學": "学",
    "習": "习", "練": "练", "級": "级", "幣": "币", "買": "买", "賣": "卖",
    "價": "价", "錢": "钱", "銀": "银", "鐵": "铁", "黃": "黄", "藍": "蓝",
    "綠": "绿", "紅": "红", "聖": "圣", "裝": "装", "備": "备", "藥": "药",
    "詛": "诅", "咒": "咒", "彈": "弹", "銀": "银", "鋒": "锋", "銳": "锐",
    "隱": "隐", "偵": "侦", "查": "查", "襲": "袭", "奪": "夺", "獵": "猎",
    "箭": "箭", "槍": "枪", "砲": "炮", "導": "导", "彈": "弹", "響": "响",
    "聲": "声", "靜": "静", "電": "电", "氣": "气", "風": "风", "雲": "云",
    "霧": "雾", "霜": "霜", "雪": "雪", "熱": "热", "燼": "烬", "焰": "焰",
    "輝": "辉", "耀": "耀", "閃": "闪", "爍": "烁", "虛": "虚", "實": "实",
    "夢": "梦", "魘": "魇", "惡": "恶", "懼": "惧", "驚": "惊", "嚇": "吓",
    "襲": "袭", "詛": "诅", "護盾": "护盾", "癒": "愈", "療": "疗",
}

#: OCR 常见的包裹性噪声
NOISE_CHARS = "\"'“”‘’`·。，、,.;；!！?？-_—|/\\()（）[]【】{}《》 \t\r\n\u3000"


def to_simplified(text: str) -> str:
    return "".join(TRADITIONAL_TO_SIMPLIFIED.get(ch, ch) for ch in text)


def normalize_text(text: str) -> str:
    """把 OCR 文本标准化成可比较形式。

    处理：全角/半角(NFKC) -> 去所有空白 -> 繁体转简体 -> 英文小写
          -> 冒号统一为 ":" -> 去掉首尾噪声字符

    注意：冒号本身可能是名称的一部分（如「升级：无尽之刃」），
    因此只做“统一”不做“删除”。
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"\s+", "", value)
    value = to_simplified(value)
    value = value.lower()
    # 冒号统一：NFKC 之后全角冒号已变成 ':'，这里再兜底几种变体
    value = value.replace("﹕", ":").replace("︓", ":")
    value = value.strip(NOISE_CHARS)
    # 常见 OCR 尾巴：名称后面跟着多余的标点或数字
    value = re.sub(r"[.\-_·]+$", "", value)
    return value


@dataclass(frozen=True)
class AugmentMatch:
    augment_id: Optional[int]
    name_cn: Optional[str]
    confidence: float
    method: str          # exact / normalized / alias / fuzzy / none
    matched_text: str = ""

    @property
    def matched(self) -> bool:
        return self.augment_id is not None


def load_aliases(path: Path | None = None) -> dict[str, int]:
    """读取 data/augment_aliases.json（不存在则为空）。

    格式：{"<别名文本>": <augment_id>, ...}
    只记录真实出现过的 OCR 错误，不要手工批量编造。
    """
    target = path or default_alias_path()
    if not target.exists():
        return {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("别名表读取失败 %s: %s", target, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int):
            out[normalize_text(key)] = value
    return out


class AugmentMatcher:
    """基于腾讯字典的海克斯匹配器。"""

    def __init__(self, repository: AugmentRepository | None = None,
                 aliases: dict[str, int] | None = None,
                 fuzzy_threshold: float = FUZZY_THRESHOLD) -> None:
        self.repository = repository or AugmentRepository()
        self.aliases = aliases if aliases is not None else load_aliases()
        self.fuzzy_threshold = fuzzy_threshold
        self._index: list[tuple[str, AugmentInfo]] | None = None

    # -- 索引 ---------------------------------------------------------------
    def _candidates(self) -> list[tuple[str, AugmentInfo]]:
        if self._index is None:
            self.repository.load()
            index: list[tuple[str, AugmentInfo]] = []
            for item in self.repository.items:
                for name in (item.name_cn, item.name_en):
                    normalized = normalize_text(name or "")
                    if normalized:
                        index.append((normalized, item))
            self._index = index
        return self._index

    # -- 匹配 ---------------------------------------------------------------
    def match(self, text: str) -> AugmentMatch:
        raw = (text or "").strip()
        if not raw:
            return AugmentMatch(None, None, 0.0, "none")

        # 1) exact
        direct = self.repository.get_by_name(raw)
        if direct is not None:
            return AugmentMatch(direct.augment_id, direct.name_cn, 1.0, "exact", raw)

        normalized = normalize_text(raw)
        if not normalized:
            return AugmentMatch(None, None, 0.0, "none", raw)

        # 2) normalized
        for candidate, item in self._candidates():
            if candidate == normalized:
                return AugmentMatch(item.augment_id, item.name_cn, 0.99,
                                    "normalized", raw)

        # 3) alias
        alias_hit = self.aliases.get(normalized)
        if alias_hit is not None:
            item = self.repository.get(alias_hit)
            if item is not None:
                return AugmentMatch(item.augment_id, item.name_cn, 0.95, "alias", raw)

        # 4) fuzzy
        best_ratio = 0.0
        best_item: AugmentInfo | None = None
        for candidate, item in self._candidates():
            ratio = SequenceMatcher(None, normalized, candidate).ratio()
            if ratio > best_ratio:
                best_ratio, best_item = ratio, item
        if best_item is not None and best_ratio >= self.fuzzy_threshold:
            return AugmentMatch(best_item.augment_id, best_item.name_cn,
                                round(best_ratio, 4), "fuzzy", raw)

        # 5) none
        return AugmentMatch(None, None, round(best_ratio, 4), "none", raw)


_DEFAULT_MATCHER: AugmentMatcher | None = None


def get_default_matcher() -> AugmentMatcher:
    global _DEFAULT_MATCHER
    if _DEFAULT_MATCHER is None:
        _DEFAULT_MATCHER = AugmentMatcher()
    return _DEFAULT_MATCHER


def match_augment(text: str, matcher: AugmentMatcher | None = None) -> AugmentMatch:
    """便捷入口：永不抛异常，失败时 augment_id = None。"""
    try:
        return (matcher or get_default_matcher()).match(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("海克斯匹配异常: %s", exc)
        return AugmentMatch(None, None, 0.0, "none", text or "")


__all__ = [
    "AugmentMatch",
    "AugmentMatcher",
    "match_augment",
    "normalize_text",
    "load_aliases",
    "to_simplified",
    "FUZZY_THRESHOLD",
    "default_alias_path",
]
