"""新版启动握手回归。"""

from __future__ import annotations

from pathlib import Path

from src.update import startup


def test_startup_marker_written_when_updater_env_present(
        tmp_path: Path, monkeypatch) -> None:
    marker_dir = tmp_path / "updates"
    monkeypatch.setenv(startup.TOKEN_ENV, "abc123")
    monkeypatch.setenv(startup.MARKER_DIR_ENV, str(marker_dir))
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path / "home"))

    target = startup.write_startup_marker("1.0.3")
    assert target == marker_dir / "startup-abc123.json"
    payload = target.read_text(encoding="utf-8")
    assert '"version": "1.0.3"' in payload
    assert '"pid":' in payload


def test_startup_marker_is_noop_without_update_env(tmp_path: Path,
                                                   monkeypatch) -> None:
    monkeypatch.delenv(startup.TOKEN_ENV, raising=False)
    monkeypatch.delenv(startup.MARKER_DIR_ENV, raising=False)
    assert startup.write_startup_marker("1.0.3") is None
