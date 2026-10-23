"""界面文案与状态映射（不把内部状态直接暴露给用户）。"""

from __future__ import annotations

from typing import Optional

#: gameflow 内部值 -> 用户友好中文
GAMEFLOW_TEXT = {
    "None": "等待游戏",
    "Lobby": "房间中",
    "Matchmaking": "匹配中",
    "ReadyCheck": "等待确认",
    "ChampSelect": "英雄选择中",
    "GameStart": "游戏加载中",
    "InProgress": "游戏中",
    "Reconnect": "重连中",
    "PreEndOfGame": "结算中",
    "EndOfGame": "对局结束",
    "WaitingForStats": "结算中",
    "TerminatedInError": "状态检测中",
    "Unknown": "状态检测中",
    "": "等待游戏",
}

REGION_TEXT = {"kr": "韩国", "global": "全球"}
TIER_TEXT = {"ibsg": "黄金及以下", "gold_plus": "黄金以上"}

UNKNOWN_TEXT = "状态检测中"


def gameflow_text(raw: Optional[str]) -> str:
    """把内部 gameflow 值映射成用户看得懂的中文。"""
    if raw is None:
        return UNKNOWN_TEXT
    return GAMEFLOW_TEXT.get(str(raw).strip(), UNKNOWN_TEXT)


def hero_text(name: Optional[str], alias: Optional[str]) -> str:
    """主界面只用中文名；没有英雄时显示 '-'。"""
    return name or alias or "-"


def stage_text(stage: Optional[int], level: Optional[int]) -> str:
    """普通用户优先看"第 N 次海克斯"。"""
    if not stage:
        return "-"
    text = "第 %d 次海克斯" % stage
    if level:
        text += " · Lv.%d" % level
    return text


def patch_text(patch: Optional[str]) -> str:
    return patch or "-"


def hero_scope_text(region: str, tier: str) -> str:
    return "%s / %s" % (REGION_TEXT.get(region, region), TIER_TEXT.get(tier, tier))


def region_text(region: str) -> str:
    return REGION_TEXT.get(region, region)


def tier_text(tier: str) -> str:
    return TIER_TEXT.get(tier, tier)


def overlay_status_text(connected: bool, has_offers: bool,
                        overlay_visible: bool, error: bool = False) -> str:
    """悬浮窗状态文案（§10）。"""
    if error:
        return "⚠ 暂不可用"
    if not connected:
        return "○ 等待 LOL"
    if has_offers and overlay_visible:
        return "● 已显示"
    return "○ 等待海克斯"


__all__ = [
    "GAMEFLOW_TEXT", "REGION_TEXT", "TIER_TEXT", "UNKNOWN_TEXT",
    "gameflow_text", "hero_text", "stage_text", "patch_text",
    "hero_scope_text", "region_text", "tier_text", "overlay_status_text",
]
