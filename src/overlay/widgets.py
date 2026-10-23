"""Overlay 内部使用的文本/样式工具（不导入 PySide6）。"""

from __future__ import annotations

STYLE = """
QLabel#title {
    color: #f0e6d2;
    font-size: 15px;
    font-weight: bold;
}
QLabel#status {
    color: #a9b6c8;
    font-size: 12px;
}
QFrame#card {
    background-color: rgba(16, 20, 28, 200);
    border: 1px solid rgba(200, 170, 110, 120);
    border-radius: 8px;
}
"""

#: 单个海克斯卡片样式
CARD_STYLE = """
QFrame#augmentCard {
    background-color: rgba(10, 14, 20, 215);
    border: 1px solid rgba(200, 170, 110, 140);
    border-radius: 8px;
}
QFrame#augmentCardRecommended {
    background-color: rgba(28, 22, 10, 225);
    border: 2px solid rgba(240, 200, 90, 230);
    border-radius: 8px;
}
QLabel#augmentName {
    color: #f0e6d2;
    font-size: 13px;
    font-weight: bold;
}
QLabel#augmentLine {
    color: #c9d4e4;
    font-size: 12px;
}
QLabel#augmentRecommend {
    color: #ffd866;
    font-size: 12px;
    font-weight: bold;
}
QLabel#augmentMissing {
    color: #8c96a6;
    font-size: 12px;
}
"""

#: OP.GG 数据缺失（正在请求 / 请求失败）时显示的占位符。
#: 正式 UI 只显示"稳定结果"：
#:   * 已确认的海克斯 → 名称 + 强度/欢迎度（缺数据显示 "—"）
#:   * 后台识别过程（pending / OCR 重试 / OP.GG 加载）**一律不进入 UI**
NO_DATA_TEXT = "—"


def format_performance(value) -> str:
    """强度：OP.GG 原始数值，不重新归一化。"""
    if value is None:
        return NO_DATA_TEXT
    return f"{value:.2f}"


def format_popular(value) -> str:
    """欢迎度：OP.GG 原始数值直接展示（单位 %）。"""
    if value is None:
        return NO_DATA_TEXT
    return f"{value:.2f}%"


def format_performance_line(value) -> str:
    """Overlay 文本：'强度：71.23' / '强度：—'（缺失不得显示 0）。"""
    return f"强度：{format_performance(value)}"


def format_popular_line(value) -> str:
    """Overlay 文本：'欢迎度：1.93%' / '欢迎度：—'。"""
    return f"欢迎度：{format_popular(value)}"


def format_recommend_label(is_tie: bool = False) -> str:
    return "★ 并列推荐" if is_tie else "★ 推荐"


def overlay_text(app_name: str, lcu_connected: bool,
                 phase: str | None = None) -> tuple[str, str]:
    """返回 (标题, 状态文本)。"""
    status = "LCU: Connected" if lcu_connected else "LCU: Disconnected"
    if phase:
        status = f"{status}\nGameflow: {phase}"
    return app_name, status


__all__ = [
    "STYLE", "CARD_STYLE", "overlay_text", "NO_DATA_TEXT",
    "format_performance", "format_popular", "format_recommend_label",
    "format_performance_line", "format_popular_line",
]
