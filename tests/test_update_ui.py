"""更新 UI：弹窗内容、进度、错误重试、自动/手动检查、去重、下载安装握手。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.update.client import UpdateCheckResult  # noqa: E402
from src.update.downloader import DownloadError, DownloadProgress  # noqa: E402
from src.update.manifest import parse_manifest  # noqa: E402


def _manifest(version: str = "1.0.2", update_type: str = "incremental") -> object:
    return parse_manifest({
        "version": version,
        "minimum_supported_version": "1.0.0",
        "release_notes": ["修复连续多局失效", "优化浮层显示"],
        "published_at": "2026-10-23",
        "update_type": update_type,
        "package_url": "https://github.com/example/repo/releases/download/v%s/u.zip" % version,
        "sha256": "a" * 64,
        "package_size": 10 * 1024 * 1024,
        "from_versions": ["1.0.1"],
    })


@pytest.fixture(scope="module")
def qapp():
    qt = pytest.importorskip("PySide6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


def _spin(seconds: float) -> None:
    from PySide6 import QtCore, QtTest

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        QtTest.QTest.qWait(20)


# ------------------------------------------------------------------ 弹窗
def test_dialog_shows_real_manifest_fields(qapp) -> None:
    from src.ui.update_dialog import UpdateDialog

    manifest = _manifest("1.0.2")
    dialog = UpdateDialog(manifest, "1.0.1")
    try:
        assert dialog.windowTitle() == "发现新版本"
        assert dialog.current_label.text() == "v1.0.1"
        assert dialog.latest_label.text() == "v1.0.2"
        assert "10.0 MB" in dialog.size_label.text()
        assert "增量" in dialog.mode_label.text()
        assert "修复连续多局失效" in dialog.notes.toPlainText()
        assert dialog.install_button.text() == "立即更新"
        assert dialog.later_button.text() == "稍后提醒"
        assert dialog.notes_button.text() == "查看更新日志"
        dialog.notes_button.click()
        assert dialog._notes_dialog is not None
        assert "优化浮层显示" in dialog._notes_dialog.browser.toPlainText()
    finally:
        dialog.close()


def test_dialog_progress_and_error_states(qapp) -> None:
    from src.ui.update_dialog import UpdateDialog

    dialog = UpdateDialog(_manifest("1.0.2", "full"), "1.0.1")
    try:
        assert "完整" in dialog.mode_label.text()
        dialog.begin_download()
        assert dialog.progress.isVisible() or not dialog.progress.isHidden()
        dialog.set_progress(DownloadProgress(5 * 1024 * 1024, 10 * 1024 * 1024, 1000))
        assert dialog.progress.value() == 50
        assert "5.0 MB" in dialog.status_label.text()
        dialog.show_download_error("网络中断")
        assert dialog.install_button.text() == "重试下载"
        assert "网络中断" in dialog.status_label.text()
        dialog.set_cancelled()
        assert dialog.install_button.text() == "立即更新"
    finally:
        dialog.close()


# -------------------------------------------------------------- 控制器
def _settings(manifest_url: str = "https://example.com/manifest.json"):
    from src.config import load_settings

    settings = load_settings()
    settings.update.manifest_url = manifest_url
    settings.update.allow_http = True
    settings.update.timeout_seconds = 1.0
    settings.update.enabled = True
    settings.update.check_on_startup = True
    return settings


class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.dialogs: list = []
        self.check_calls = 0
        self.quit_called = 0
        self.launched: list = []

    def feedback(self, text: str, error: bool = False) -> None:
        self.messages.append((text, error))

    def on_quit(self) -> None:
        self.quit_called += 1

    def dialog_factory(self, manifest, current_version, on_install=None,
                       on_cancel=None):
        from src.ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(manifest, current_version,
                              on_install=on_install, on_cancel=on_cancel)
        self.dialogs.append(dialog)
        return dialog

    def launcher(self, updater, job_path, startup_token=""):
        self.launched.append((updater, job_path))
        return object()


def _controller(qapp, isolated_home, rec: Recorder, result: UpdateCheckResult,
                manifest_url: str = "https://example.com/manifest.json"):
    from src.ui.update_controller import UpdateController

    async def fake_check(current, url, timeout=15, allow_http=False):  # noqa: ARG001
        rec.check_calls += 1
        return result

    return UpdateController(_settings(manifest_url), current_version="1.0.1",
                            feedback=rec.feedback, on_quit=rec.on_quit,
                            check_fn=fake_check, dialog_factory=rec.dialog_factory,
                            updater_launcher=rec.launcher)


def test_startup_latest_does_not_open_dialog(qapp, isolated_home) -> None:
    rec = Recorder()
    controller = _controller(qapp, isolated_home, rec,
                             UpdateCheckResult("latest", current_version="1.0.1"))
    controller.check(manual=False)
    _spin(0.6)
    assert rec.check_calls == 1
    assert rec.dialogs == []
    controller.shutdown()


def test_startup_new_version_opens_dialog_once(qapp, isolated_home) -> None:
    rec = Recorder()
    result = UpdateCheckResult("update", manifest=_manifest("1.0.2"),
                               current_version="1.0.1")
    controller = _controller(qapp, isolated_home, rec, result)
    controller.check(manual=False)
    _spin(0.6)
    assert len(rec.dialogs) == 1
    rec.dialogs[0].close()                    # 稍后提醒
    _spin(0.2)
    # 同一次启动内不会再次自动弹同一个版本
    controller.check(manual=False)
    _spin(0.6)
    assert len(rec.dialogs) == 1
    controller.shutdown()


def test_manual_check_latest_message(qapp, isolated_home) -> None:
    rec = Recorder()
    controller = _controller(qapp, isolated_home, rec,
                             UpdateCheckResult("latest", current_version="1.0.1"))
    controller.check(manual=True)
    _spin(0.6)
    assert any("已是最新版本" in text for text, _ in rec.messages)
    assert rec.dialogs == []
    controller.shutdown()


def test_manual_check_without_url_reports_unconfigured(qapp, isolated_home) -> None:
    rec = Recorder()
    controller = _controller(qapp, isolated_home, rec,
                             UpdateCheckResult("latest", current_version="1.0.1"),
                             manifest_url="")
    controller.check(manual=True)
    _spin(0.3)
    assert rec.check_calls == 0
    assert any("未配置" in text for text, _ in rec.messages)
    controller.shutdown()


def test_repeated_manual_checks_do_not_spawn_many_tasks(qapp, isolated_home) -> None:
    rec = Recorder()
    controller = _controller(qapp, isolated_home, rec,
                             UpdateCheckResult("latest", current_version="1.0.1"))
    for _ in range(5):
        controller.check(manual=True)
    _spin(0.8)
    assert rec.check_calls == 1
    controller.shutdown()


def test_sleep_then_next_session_prompts_again(qapp, isolated_home) -> None:
    rec = Recorder()
    result = UpdateCheckResult("update", manifest=_manifest("1.0.2"),
                               current_version="1.0.1")
    controller = _controller(qapp, isolated_home, rec, result)
    controller.check(manual=False)
    _spin(0.5)
    rec.dialogs[0].close()
    controller.shutdown()

    # 模拟"下一次启动"：新控制器重新提示
    rec2 = Recorder()
    controller2 = _controller(qapp, isolated_home, rec2, result)
    controller2.check(manual=False)
    _spin(0.5)
    assert len(rec2.dialogs) == 1
    controller2.shutdown()


def test_download_success_launches_updater_and_quits(qapp, isolated_home,
                                                     tmp_path) -> None:
    rec = Recorder()
    result = UpdateCheckResult("update", manifest=_manifest("1.0.2"),
                               current_version="1.0.1")
    package = tmp_path / "update.zip"
    package.write_bytes(b"zip")

    from src.ui.update_controller import UpdateController

    async def fake_check(*args, **kwargs):  # noqa: ARG001
        rec.check_calls += 1
        return result

    def fake_download(manifest, dest, on_progress=None, cancel=None, timeout=30):
        if on_progress:
            on_progress(DownloadProgress(5, 10, 100))
            on_progress(DownloadProgress(10, 10, 100))
        return package

    controller = UpdateController(_settings(), current_version="1.0.1",
                                  feedback=rec.feedback, on_quit=rec.on_quit,
                                  check_fn=fake_check, download_fn=fake_download,
                                  updater_launcher=rec.launcher,
                                  dialog_factory=rec.dialog_factory)
    updater = tmp_path / "Updater.exe"
    updater.write_bytes(b"exe")
    controller.updater_path = lambda: updater
    controller.install_dir = lambda: tmp_path

    controller.check(manual=True)
    _spin(0.5)
    assert rec.dialogs
    rec.dialogs[0].install_button.click()
    _spin(1.2)

    assert rec.launched, "必须启动 Updater.exe"
    updater_path, job_path = rec.launched[0]
    assert Path(job_path).exists()
    assert rec.quit_called >= 1
    controller.shutdown()


def test_download_error_offers_retry(qapp, isolated_home, tmp_path) -> None:
    rec = Recorder()
    result = UpdateCheckResult("update", manifest=_manifest("1.0.2"),
                               current_version="1.0.1")

    from src.ui.update_controller import UpdateController

    async def fake_check(*args, **kwargs):  # noqa: ARG001
        return result

    def failing_download(manifest, dest, on_progress=None, cancel=None, timeout=30):
        raise DownloadError("网络中断")

    controller = UpdateController(_settings(), current_version="1.0.1",
                                  feedback=rec.feedback, on_quit=rec.on_quit,
                                  check_fn=fake_check, download_fn=failing_download,
                                  updater_launcher=rec.launcher,
                                  dialog_factory=rec.dialog_factory)
    controller.check(manual=True)
    _spin(0.5)
    rec.dialogs[0].install_button.click()
    _spin(0.8)
    assert rec.dialogs[0].install_button.text() == "重试下载"
    assert "网络中断" in rec.dialogs[0].status_label.text()
    assert rec.launched == []
    controller.shutdown()


def test_controller_runs_updater_from_temp_copy(qapp, isolated_home,
                                                tmp_path, monkeypatch) -> None:
    """安装目录中的 Updater 不能被自身锁定，必须从临时副本运行。"""
    from src.ui import update_controller

    rec = Recorder()
    controller = update_controller.UpdateController(
        _settings(), current_version="1.0.2",
        feedback=rec.feedback, on_quit=rec.on_quit)
    app_dir = tmp_path / "已安装 目录"
    app_dir.mkdir()
    updater = app_dir / "Updater.exe"
    updater.write_bytes(b"updater")
    package = tmp_path / "update.zip"
    package.write_bytes(b"package")
    controller.install_dir = lambda: app_dir
    controller.updater_path = lambda: updater
    controller._manifest = _manifest("1.0.3")

    class Proc:
        pid = 4321

        def poll(self):
            return None

    called = {}

    def fake_popen(args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(update_controller.subprocess, "Popen", fake_popen)
    controller._prepare_and_launch(package)

    launched = Path(called["args"][0]).resolve()
    assert launched.parent == (isolated_home / "updates").resolve()
    assert launched != updater.resolve()
    assert launched.exists()
    assert Path(called["args"][2]).exists()
    controller.shutdown()
