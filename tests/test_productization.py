"""Day 6 成品化测试：路径、设置持久化、单实例、日志轮转、状态中枢。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from src.config import Settings, load_settings, save_user_settings
from src.core.state_hub import HUB, PerfMonitor, StateHub
from src.overlay.widgets import format_performance_line, format_popular_line
from src.ui.single_instance import SingleInstance
from src.utils import paths
from src.utils.process import mask_token


# ------------------------------------------------------------------ paths
def test_user_data_dir_is_writable_and_not_program_files() -> None:
    root = paths.user_data_dir()
    assert root.exists()
    assert "Program Files" not in str(root)
    assert paths.user_data_dir().is_dir()


def test_user_data_subdirs_created() -> None:
    dirs = paths.ensure_user_subdirs()
    for key in ("config", "logs", "data"):
        assert dirs[key].is_dir()


def test_database_and_log_live_under_user_data_dir() -> None:
    root = paths.user_data_dir()
    assert str(paths.database_path()).startswith(str(root))
    assert str(paths.log_path()).startswith(str(root))
    assert paths.database_path().name == "aram_helper.db"


def test_resource_dir_dev_mode_points_to_project() -> None:
    assert paths.resource_dir() == paths.project_root()
    assert paths.augment_dict_path().exists()


def test_resource_path_prefers_user_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    override = tmp_path / "data" / "kiwi_augments.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("[]", encoding="utf-8")
    assert paths.augment_dict_path() == override


# ------------------------------------------------------- settings persistence
def test_user_settings_are_saved_and_reloaded(tmp_path: Path) -> None:
    target = tmp_path / "user_config.yaml"
    settings = load_settings()
    settings.overlay.opacity = 70
    settings.overlay.offset_x = 12
    settings.overlay.offset_y = -8
    settings.cache.opgg_ttl_hours = 3.0
    settings.debug.enabled = True
    save_user_settings(settings, target)
    assert target.exists()

    reloaded = load_settings(user_path=target)
    assert reloaded.overlay.opacity == 70
    assert reloaded.overlay.offset_x == 12
    assert reloaded.overlay.offset_y == -8
    assert reloaded.cache.opgg_ttl_hours == 3.0
    assert reloaded.debug.enabled is True


def test_saving_does_not_touch_default_config(tmp_path: Path) -> None:
    """用户设置写 user_config.yaml，绝不改默认 config.yaml。"""
    default_copy = tmp_path / "config.yaml"
    default_copy.write_text("overlay:\n  opacity: 100\n", encoding="utf-8")
    before = default_copy.read_text(encoding="utf-8")

    settings = load_settings(path=default_copy, user_path=tmp_path / "u.yaml")
    settings.overlay.opacity = 55
    save_user_settings(settings, tmp_path / "u.yaml")
    assert default_copy.read_text(encoding="utf-8") == before


def test_overlay_defaults_match_validated_values() -> None:
    settings = Settings()
    assert settings.overlay.opacity == 100     # 默认不透明
    assert settings.overlay.offset_x == 0
    assert settings.overlay.offset_y == 0      # 默认贴在卡片下方
    assert settings.overlay.enabled is True
    assert settings.cache.opgg_ttl_hours == 6.0
    assert settings.debug.enabled is False


# ------------------------------------------------------------ single instance
def test_single_instance_blocks_second_acquire() -> None:
    name = "Global\\LOLHexHelper_TestMutex"
    first = SingleInstance(name)
    second = SingleInstance(name)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        second.release()
        first.release()
    third = SingleInstance(name)
    assert third.acquire() is True
    third.release()


# ------------------------------------------------------------------- logging
def test_log_rotation_10mb_5_files(tmp_path: Path, monkeypatch) -> None:
    import src.utils.logger as logger_module

    monkeypatch.setattr(logger_module, "_CONFIGURED", False)
    root = logger_module.setup_logging(tmp_path / "app.log", "INFO", console=False)
    handlers = [h for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers, "必须配置轮转文件handler"
    handler = handlers[0]
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
    monkeypatch.setattr(logger_module, "_CONFIGURED", False)


def test_token_is_masked_in_logs() -> None:
    line = 'LeagueClientUx.exe "--remoting-auth-token=TEST-TOKEN-NOT-REAL-02" "--app-port=3485"'
    masked = mask_token(line)
    assert "abcDEF123" not in masked
    assert "***" in masked


# ------------------------------------------------------------------- overlay
def test_overlay_missing_values_show_placeholder_not_zero() -> None:
    # 数据缺失（OP.GG 请求中 / 请求失败）显示 "—"，与"识别中…"彻底分离
    assert format_performance_line(None) == "强度：—"
    assert format_popular_line(None) == "欢迎度：—"
    assert format_performance_line(71.23) == "强度：71.23"
    assert format_popular_line(0) == "欢迎度：0.00%"     # 0 是合法值


def test_overlay_scope_labels_are_distinct() -> None:
    settings = Settings()
    assert settings.hero_scope_label == "Korea / Iron–Gold"
    assert settings.augment_scope_label == "OP.GG Aggregate"
    assert settings.hero_scope_label != settings.augment_scope_label


# --------------------------------------------------------------- state hub
def test_state_hub_update_and_snapshot() -> None:
    hub = StateHub()
    hub.update(client_connected=True, gameflow="InProgress", stage=2,
               offers=[{"augment_id": 1}])
    snap = hub.snapshot()
    assert snap.client_connected is True
    assert snap.gameflow == "InProgress"
    assert snap.stage == 2
    assert snap.offers == [{"augment_id": 1}]
    snap.offers = []                      # 快照是副本，不影响内部状态
    assert hub.snapshot().offers == [{"augment_id": 1}]


def test_state_hub_ignores_unknown_fields() -> None:
    hub = StateHub()
    hub.update(not_a_field=123)
    assert not hasattr(hub.snapshot(), "not_a_field")


def test_perf_monitor_records_latencies() -> None:
    monitor = PerfMonitor(interval=999)
    monitor.record("ocr", 0.25)
    monitor.record("opgg", 1.5)
    monitor.record("overlay", 0.01)
    assert monitor.stats.ocr_ms == pytest.approx(250.0)
    assert monitor.stats.opgg_ms == pytest.approx(1500.0)
    assert monitor.stats.overlay_ms == pytest.approx(10.0)
    assert "OCR: 250 ms" in monitor.stats.describe()


def test_global_hub_exists() -> None:
    assert HUB is not None
    assert HUB.snapshot().augment_scope == "OP.GG Aggregate"
