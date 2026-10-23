"""守卫测试：正式 UI 里**永远**不出现"识别中"这类过程状态。

用户要求（2026-09-22）：
    后台可以有 pending / ocr_retry / stats_loading / stats_error，
    但前台只允许显示"稳定结果"：
        * 英雄：中文名，或 "—"
        * 海克斯卡片：名称 + 强度 + 欢迎度（缺数据显示 "—"）
        * 没有完整新快照时，保持上一份已确认快照

本文件用两层守卫：
    1. AST 扫描 UI 相关模块的**字符串字面量**（不含注释/文档字符串）
    2. 用真实牌面回放跑一局，检查所有会进入 UI 的文本
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.replay_harness import real_ocr_engine  # noqa: E402,F401 - pytest fixture

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 正式 UI 禁止出现的字样
FORBIDDEN = ("识别中", "正在识别", "等待识别", "OCR 中", "数据识别中")

#: 会直接决定 UI 文案的模块
UI_MODULES = (
    "src/ui/texts.py",
    "src/ui/main_window.py",
    "src/ui/debug_window.py",
    "src/overlay/widgets.py",
    "src/overlay/window.py",
    "src/overlay/manager.py",
)


def _string_literals(path: Path) -> list[tuple[int, str]]:
    """取文件里的字符串字面量（跳过文档字符串），返回 [(行号, 文本)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("relative", UI_MODULES)
def test_ui_modules_have_no_process_state_literals(relative: str) -> None:
    """UI 模块的字符串字面量里不允许出现"识别中"之类字样。"""
    path = PROJECT_ROOT / relative
    assert path.exists(), path
    for lineno, text in _string_literals(path):
        for word in FORBIDDEN:
            assert word not in text, "%s:%d 出现了 %r：%r" % (relative, lineno, word, text)


def test_overlay_widgets_no_longer_exports_pending_text() -> None:
    """PENDING_TEXT 常量必须被删除：UI 不再有"识别中…"这个文案。"""
    from src.overlay import widgets

    assert not hasattr(widgets, "PENDING_TEXT")
    assert "识别中" not in str(getattr(widgets, "__all__", []))


def test_payload_pending_flag_does_not_change_ui_text() -> None:
    """payload 里的内部 pending 标记不得改变任何界面文案。"""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtWidgets

    from src.overlay.window import OverlayWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rows = [{"slot": i, "augment_id": aid, "name_cn": name,
             "performance": 70.0 + i, "popular": 1.0 + i, "recommended": i == 1,
             "tie": False, "pending": i == 1, "data_state": "READY"}
            for i, (aid, name) in enumerate([(1325, "玻璃大炮"),
                                             (1115, "最万用的瞄准镜"),
                                             (1079, "战争交响乐")])]
    window = OverlayWindow(screen_index=0, opacity=100, offset_x=0, offset_y=0)
    try:
        window.show_overlay()
        window.update_offers(rows, pending_slots={1})
        texts = []
        for card in window._cards:            # noqa: SLF001
            texts += [card.name.text(), card.performance.text(), card.popular.text(),
                      card.recommend.text()]
        # 每张卡 4 行：名称/强度/欢迎度/推荐说明
        assert texts[0] == "玻璃大炮", texts
        assert texts[4] == "最万用的瞄准镜", texts   # pending 也要显示正常名称
        assert texts[8] == "战争交响乐", texts
        for text in texts:
            for word in FORBIDDEN:
                assert word not in text, "卡片出现 %r：%r" % (word, text)
    finally:
        window.hide()
        window.close()
        _ = app


def test_main_window_texts_never_show_process_state() -> None:
    """主窗口用到的全部文案函数都不允许产出过程状态。"""
    from src.ui import texts

    samples = []
    for raw in ("None", "Lobby", "Matchmaking", "ReadyCheck", "ChampSelect",
                "GameStart", "InProgress", "Reconnect", "Unknown", None, ""):
        samples.append(texts.gameflow_text(raw))
    for stage, level in ((None, None), (1, 4), (4, 18)):
        samples.append(texts.stage_text(stage, level))
    for name, alias in ((None, None), ("疾风剑豪", "Yasuo"), (None, "Varos")):
        samples.append(texts.hero_text(name, alias))
    samples.append(texts.patch_text(None))
    for connected in (True, False):
        for has_offers in (True, False):
            samples.append(texts.overlay_status_text(connected, has_offers, has_offers))
    for text in samples:
        for word in FORBIDDEN:
            assert word not in text, "主界面文案出现 %r：%r" % (word, text)
    # 英雄未知时是 "-"，不是任何过程状态
    assert texts.hero_text(None, None) == "-"


def test_full_game_events_never_show_process_state(monkeypatch, temp_database,
                                                   real_ocr_engine) -> None:
    """真实回放一局（含 OP.GG 失败 + 单帧失败）：所有 UI 文本都不含过程状态。"""
    from src.ui import texts
    from tests.replay_harness import FakeClock, ReplayScreen, cut_at_game_end, run_replay

    class DeadOpggClient:
        async def get_champion_augments(self, champion_id):  # noqa: ARG002
            from src.opgg.client import OpggError

            raise OpggError("OP.GG 请求失败: ConnectError: ")

        async def get_champion_stats(self, *args, **kwargs):  # noqa: ARG002
            from src.opgg.client import OpggError

            raise OpggError("OP.GG 请求失败: ConnectError: ")

        async def aclose(self) -> None:
            pass

    from src.opgg.provider import OpggProvider

    screen = ReplayScreen()
    clock = FakeClock()
    provider = OpggProvider(client=DeadOpggClient())

    def on_call(index, screen, clock):
        clock.advance(0.30)
        if index == 3:
            screen.animate(0, delta=4)          # 动画/光照

    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=on_call, clock=clock, provider=provider,
                        max_reads=6)
    events = cut_at_game_end(result["events"])
    assert events
    for event in events:
        texts_to_check = [
            texts.gameflow_text(event.get("gameflow")),
            texts.hero_text(event.get("champion_name"), event.get("champion_alias")),
            texts.stage_text(event.get("stage"), event.get("level")),
            texts.patch_text(event.get("data_patch")),
            texts.overlay_status_text(True, bool(event.get("offers")),
                                      bool(event.get("overlay_visible"))),
        ]
        for item in event.get("offers") or []:
            texts_to_check += [item.get("name_cn") or "",
                               str(item.get("performance")),
                               str(item.get("popular"))]
        for text in texts_to_check:
            for word in FORBIDDEN:
                assert word not in text, "UI 文本出现 %r：%r" % (word, text)
    # 名称必须一直在（OP.GG 失败不影响识别）
    assert [o["name_cn"] for o in events[-1]["offers"]] == [
        "玻璃大炮", "最万用的瞄准镜", "战争交响乐"]
    assert all(o["performance"] is None for o in events[-1]["offers"])


__all__: list[str] = []
