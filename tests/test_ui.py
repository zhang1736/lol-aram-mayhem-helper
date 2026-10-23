"""界面层测试：设置窗口、保存/取消、持久化、悬浮窗预览、重连、状态映射。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.config import load_settings, save_user_settings  # noqa: E402
from src.ui import texts  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    app = qt.QApplication.instance() or qt.QApplication([])
    yield app


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path: Path):
    """把用户数据目录指到临时目录，避免污染真实配置。"""
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------ 设置窗口
def test_settings_window_opens(qapp, isolated_home) -> None:
    """点「设置」必须真的打开对话框（回归：曾经因 QtWidgets.Qt 报错而没反应）。"""
    from src.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(load_settings())
    assert dialog.windowTitle() == "LOL 海克斯助手 - 设置"
    assert dialog.ttl_box.value() == 6
    assert dialog.opacity_slider.value() == 100
    assert dialog.overlay_box.isChecked() is True
    dialog.reject()


def test_main_window_constructs_with_expected_buttons(qapp, isolated_home) -> None:
    """回归：MainWindow 回调参数名必须与 app_ui 调用一致（曾因改名漏改导致启动崩溃）。"""
    from PySide6 import QtWidgets

    from src.ui.main_window import MainWindow

    window = MainWindow(on_settings=lambda: None,
                        on_preview_overlay=lambda: None,
                        on_reconnect=lambda: None,
                        on_quit=lambda: None,
                        on_debug=lambda: None)
    labels = [b.text() for b in window.findChildren(QtWidgets.QPushButton)]
    # v1.0.1 起主窗口增加"检查更新"入口（版本与更新区域）
    assert labels == ["检查更新", "预览悬浮窗", "设置", "重新连接 LOL", "Debug"]
    assert window.version_value.text().startswith("v")
    assert window.update_status.text() == ""
    window.set_feedback("设置已保存")
    assert window.feedback.text() == "设置已保存"


def test_settings_save_writes_user_config(qapp, isolated_home) -> None:
    from src.ui.settings_dialog import SettingsDialog

    settings = load_settings()
    dialog = SettingsDialog(settings)
    dialog.opacity_slider.setValue(65)
    dialog.offset_x_box.setValue(30)
    dialog.offset_y_box.setValue(-20)
    dialog.ttl_box.setValue(3)
    dialog.debug_box.setChecked(True)
    dialog._apply()

    saved = isolated_home / "config" / "user_config.yaml"
    assert saved.exists(), "保存必须写入 config/user_config.yaml"
    reloaded = load_settings()
    assert reloaded.overlay.opacity == 65
    assert reloaded.overlay.offset_x == 30
    assert reloaded.overlay.offset_y == -20
    assert reloaded.cache.opgg_ttl_hours == 3.0
    assert reloaded.debug.enabled is True


def test_settings_cancel_does_not_save(qapp, isolated_home) -> None:
    from src.ui.settings_dialog import SettingsDialog

    settings = load_settings()
    dialog = SettingsDialog(settings)
    dialog.opacity_slider.setValue(40)
    dialog.reject()          # 取消

    assert not (isolated_home / "config" / "user_config.yaml").exists()
    assert settings.overlay.opacity == 100     # 内存里的设置也没被改


def test_settings_persistence_across_restart(qapp, isolated_home) -> None:
    """保存 -> 重新加载（模拟重启）-> 值仍在。"""
    settings = load_settings()
    settings.overlay.opacity = 55
    settings.overlay.offset_y = 44
    save_user_settings(settings)

    reloaded = load_settings()
    assert reloaded.overlay.opacity == 55
    assert reloaded.overlay.offset_y == 44


def test_default_config_is_not_modified(qapp, isolated_home) -> None:
    from src.utils import paths

    default_file = paths.default_config_path()
    before = default_file.read_text(encoding="utf-8")
    settings = load_settings()
    settings.overlay.opacity = 33
    save_user_settings(settings)
    assert default_file.read_text(encoding="utf-8") == before


# ------------------------------------------------------------ 悬浮窗预览
def test_overlay_preview(qapp, isolated_home) -> None:
    """预览悬浮窗用真实样本数据，且恰好一个推荐。"""
    from src.ui.app_ui import PREVIEW_OFFERS

    assert len(PREVIEW_OFFERS) == 3
    assert [o["name_cn"] for o in PREVIEW_OFFERS] == [
        "玻璃大炮", "最万用的瞄准镜", "战争交响乐"]
    recommended = [o for o in PREVIEW_OFFERS if o.get("recommended")]
    assert len(recommended) == 1
    assert recommended[0]["augment_id"] == 1115      # 欢迎度最高
    for offer in PREVIEW_OFFERS:
        assert offer["performance"] is not None
        assert offer["popular"] is not None


def test_overlay_status_text_variants() -> None:
    assert texts.overlay_status_text(False, False, False) == "○ 等待 LOL"
    assert texts.overlay_status_text(True, False, False) == "○ 等待海克斯"
    assert texts.overlay_status_text(True, True, True) == "● 已显示"
    assert texts.overlay_status_text(True, True, True, error=True) == "⚠ 暂不可用"


# ------------------------------------------------------------ 重新连接
def test_reconnect_lol() -> None:
    """点「重新连接 LOL」必须置位重连标志并给出反馈文案。"""
    from src import live
    from src.core.state_hub import StateHub

    control = live.Control()
    assert control.reconnect.is_set() is False
    hub = StateHub()
    live.request_reconnect(control, hub=hub)
    assert control.reconnect.is_set() is True
    assert hub.snapshot().status_message == "正在重新连接 LOL..."


def test_reconnect_feedback_messages_exist() -> None:
    """三种反馈文案必须齐备（正在/成功/失败）。"""
    import inspect

    from src.ui import app_ui

    source = inspect.getsource(app_ui)
    for message in ("正在重新连接 LOL...", "已重新连接 LOL", "未检测到 LOL 客户端"):
        assert message in source, message


# ------------------------------------------------------------ 状态映射
@pytest.mark.parametrize(("raw", "expected"), [
    ("None", "等待游戏"),
    ("Lobby", "房间中"),
    ("Matchmaking", "匹配中"),
    ("ReadyCheck", "等待确认"),
    ("ChampSelect", "英雄选择中"),
    ("GameStart", "游戏加载中"),
    ("InProgress", "游戏中"),
    ("Reconnect", "重连中"),
    ("SomethingNew", "状态检测中"),
    (None, "状态检测中"),
])
def test_gameflow_display_mapping(raw, expected) -> None:
    assert texts.gameflow_text(raw) == expected


def test_stage_display_is_user_friendly() -> None:
    assert texts.stage_text(None, None) == "-"
    assert texts.stage_text(1, 4) == "第 1 次海克斯 · Lv.4"
    assert texts.stage_text(2, 7) == "第 2 次海克斯 · Lv.7"
    assert texts.stage_text(4, 18, 5) == "第 5 次海克斯 · Lv.18"


def test_hero_display_prefers_chinese_name() -> None:
    assert texts.hero_text("皎月女神", "Diana") == "皎月女神"
    assert texts.hero_text(None, "Diana") == "Diana"
    assert texts.hero_text(None, None) == "-"


def test_scope_display_is_chinese() -> None:
    assert texts.hero_scope_text("kr", "ibsg") == "韩国 / 黄金及以下"


def test_patch_display() -> None:
    assert texts.patch_text(None) == "-"
    assert texts.patch_text("16.18") == "16.18"
