"""OP.GG 响应解析（独立模块，负责 schema 漂移检测）。

原则（Day 5 §5）：
  * data 为空 -> 抛 SchemaMismatchError，**不能**静默返回 []
  * 必需字段缺失 -> SchemaMismatchError
  * 绝不把"字段缺失"当成 popular = 0（0 是合法数值，缺失是错误）
"""

from __future__ import annotations

import json
from typing import Any

from src.core.models import DataScope, OPGGAugmentStats
from src.utils.logger import get_tagged

logger = get_tagged("opgg", __name__)

#: 海克斯统计响应每条记录必须具备的字段
REQUIRED_AUGMENT_FIELDS = ("id", "tier", "performance", "popular")


class SchemaMismatchError(RuntimeError):
    """OP.GG 响应结构与预期不符。"""

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


def _as_payload(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise SchemaMismatchError(f"响应不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaMismatchError(f"响应根节点应为对象，实际是 {type(payload).__name__}")
    return payload


def parse_augments(payload: str | bytes | dict[str, Any],
                   strict: bool = True) -> list[dict[str, Any]]:
    """把 aram-augments 响应解析成记录列表。

    strict=True（默认）时，空数组或字段缺失都会抛 SchemaMismatchError。
    """
    data = _as_payload(payload)
    if "data" not in data:
        raise SchemaMismatchError("响应缺少 data 字段", details=sorted(data.keys()))
    rows = data["data"]
    if not isinstance(rows, list):
        raise SchemaMismatchError(f"data 应为数组，实际是 {type(rows).__name__}")
    if not rows:
        raise SchemaMismatchError("data 为空数组（OP.GG 结构可能已变更）")

    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SchemaMismatchError(f"data[{index}] 不是对象")
        missing = [field for field in REQUIRED_AUGMENT_FIELDS if field not in row]
        if missing and strict:
            raise SchemaMismatchError(
                f"data[{index}] 缺少字段 {missing}", details=sorted(row.keys()))
        if missing:
            continue
        # §E：augment_id 内部统一用 int（OP.GG 偶发返回字符串 "1325"，
        # 绝不能出现 1325 / "1325" / 1325.0 三种类型混着比较）
        raw_id = row["id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str, float)):
            raise SchemaMismatchError(f"data[{index}].id 类型不支持: {raw_id!r}")
        try:
            augment_id = int(str(raw_id).strip())
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError(f"data[{index}].id 不是整数: {raw_id!r}") from exc
        for field in ("performance", "popular"):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaMismatchError(
                    f"data[{index}].{field} 应为数值，实际是 {value!r}"
                    "（字段缺失不得当成 0）")
        tier = row["tier"]
        if tier is not None and not isinstance(tier, int):
            raise SchemaMismatchError(f"data[{index}].tier 应为整数或 null，实际 {tier!r}")
        out.append({
            "augment_id": augment_id,
            "opgg_augment_tier": tier,
            "performance": float(row["performance"]),
            "popular": float(row["popular"]),
        })
    return out


def parse_augment_stats(payload: str | bytes | dict[str, Any],
                        patch: str | None = None,
                        strict: bool = True) -> list[OPGGAugmentStats]:
    """解析成 OPGGAugmentStats 列表。

    海克斯统计接口无法按 region/tier 过滤，
    因此 scope 固定为 DataScope.aggregate_scope()（"OP.GG Aggregate"）。
    """
    scope = DataScope.aggregate_scope()
    stats: list[OPGGAugmentStats] = []
    for row in parse_augments(payload, strict=strict):
        stats.append(OPGGAugmentStats(
            augment_id=row["augment_id"],
            performance=row["performance"],
            popular=row["popular"],
            opgg_augment_tier=row["opgg_augment_tier"],
            source="opgg",
            scope=scope,
            patch=patch,
        ))
    return stats


def parse_meta_patch(payload: dict[str, Any]) -> str | None:
    """从 meta.version 取 data_patch。

    注意：海克斯接口没有 meta；英雄统计接口才有。
    data_patch != client_version。
    """
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("version"), str):
        return meta["version"]
    return None


__all__ = [
    "parse_augments", "parse_augment_stats", "parse_meta_patch",
    "SchemaMismatchError", "REQUIRED_AUGMENT_FIELDS",
]
