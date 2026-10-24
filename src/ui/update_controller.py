"""更新控制器（主线程）：

* 后台线程做网络检查与下载（绝不阻塞 Qt/游戏）
* QTimer 在主线程轮询结果并更新 UI
* 同一时间只允许一个检查/下载任务（重复点击复用）
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import src as src_pkg
from src.update.client import UpdateCheckResult, check_for_update
from src.update.downloader import DownloadError, DownloadProgress, download_sync
from src.update.job import UpdateJob, write_job
from src.update.manifest import UpdateManifest
from src.update.version import parse_version
from src.utils import paths
from src.utils.logger import get_logger, get_tagged

logger = get_logger(__name__)
update_log = get_tagged("update")

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False

STARTUP_CHECK_DELAY_MS = 2500
POLL_INTERVAL_MS = 150
UPDATER_EXE_NAME = "Updater.exe"


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class UpdateController(QtCore.QObject):
        """把"检查 → 弹窗 → 下载 → 交给 Updater.exe → 退出"串起来。"""

        def __init__(self, settings, *, current_version: Optional[str] = None,
                     parent=None,
                     feedback: Optional[Callable[[str, bool], None]] = None,
                     on_quit: Optional[Callable[[], None]] = None,
                     check_fn=None, download_fn=None, updater_launcher=None,
                     dialog_factory=None) -> None:
            super().__init__(parent)
            self.settings = settings
            self.current_version = current_version or src_pkg.__version__
            self._feedback = feedback or (lambda text, error=False: None)
            self._on_quit = on_quit or (lambda: None)
            self._check_fn = check_fn or check_for_update
            self._download_fn = download_fn or download_sync
            self._launch_updater = updater_launcher or self._spawn_updater
            self._dialog_factory = dialog_factory or self._make_dialog

            self._lock = threading.Lock()
            self._check_result: Optional[UpdateCheckResult] = None
            self._download_result = None
            self._progress: Optional[DownloadProgress] = None
            self._checking = False
            self._downloading = False
            self._manual_pending = False
            self._cancel = threading.Event()
            self._dialog = None
            self._manifest: Optional[UpdateManifest] = None
            self._prompted: set[str] = set()
            self._shutdown = False
            self._updater_process = None

            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(POLL_INTERVAL_MS)
            self._timer.timeout.connect(self._poll)

        # -- 启动 -----------------------------------------------------------
        def schedule_startup_check(self) -> None:
            if not (self.settings.update.enabled
                    and self.settings.update.check_on_startup):
                update_log.info("启动自动检查更新已关闭")
                return
            QtCore.QTimer.singleShot(STARTUP_CHECK_DELAY_MS,
                                      lambda: self.check(manual=False))

        # -- 检查 -----------------------------------------------------------
        def check(self, manual: bool = False) -> None:
            if self._shutdown:
                return
            if self._checking:
                self._manual_pending = self._manual_pending or manual
                update_log.info("检查更新已在进行中，复用当前任务")
                return
            if not self.settings.update.enabled:
                if manual:
                    self._feedback("自动更新已关闭", error=True)
                return
            url = self.settings.update.manifest_url
            if not url:
                result = UpdateCheckResult("disabled", current_version=self.current_version,
                                           error="未配置更新源（config.yaml: update.manifest_url）")
                self._handle_result(result, manual)
                return

            self._checking = True
            self._manual_pending = manual
            if manual:
                self._feedback("正在检查更新…", False)
            update_log.info("开始检查更新（manual=%s）", manual)
            timeout = float(self.settings.update.timeout_seconds)
            allow_http = bool(self.settings.update.allow_http)

            def worker() -> None:
                try:
                    result = asyncio.run(self._check_fn(
                        self.current_version, url, timeout=timeout,
                        allow_http=allow_http))
                except Exception as exc:  # noqa: BLE001
                    result = UpdateCheckResult("error", current_version=self.current_version,
                                               error="检查更新异常: %s" % exc)
                with self._lock:
                    self._check_result = result

            threading.Thread(target=worker, name="update-check", daemon=True).start()
            self._timer.start()

        # -- 轮询（主线程） ---------------------------------------------------
        def _poll(self) -> None:
            with self._lock:
                result = self._check_result
                self._check_result = None
                progress = self._progress
                self._progress = None
                download_result = self._download_result
                self._download_result = None

            if progress is not None and self._dialog is not None:
                self._dialog.set_progress(progress)
            if result is not None:
                manual = self._manual_pending
                self._manual_pending = False
                self._checking = False
                self._handle_result(result, manual)
            if download_result is not None:
                self._handle_download(download_result)

            if not self._checking and not self._downloading:
                self._timer.stop()

        def _handle_result(self, result: UpdateCheckResult, manual: bool) -> None:
            if result.status == "error":
                update_log.warning("检查更新失败: %s", result.error)
                if manual:
                    self._feedback("检查更新失败：%s" % (result.error or "未知错误"),
                                   True)
                return
            if result.status == "disabled":
                update_log.info("未配置更新源（update.manifest_url 为空），跳过%s检查",
                                "手动" if manual else "启动")
                if manual:
                    self._feedback("更新源未配置，无法检查更新", True)
                return
            if result.status == "latest":
                update_log.info("当前已是最新版本: v%s", result.current_version)
                if manual:
                    self._feedback("当前已是最新版本（v%s）" % result.current_version)
                return
            if not result.has_update:
                return

            manifest = result.manifest
            # 增量包与当前版本不兼容 → 自动回退完整包
            if manifest.update_type == "incremental":
                try:
                    current = parse_version(result.current_version)
                except Exception:  # noqa: BLE001
                    current = None
                if current is not None and not manifest.can_apply_incrementally(current):
                    full = manifest.as_full_package()
                    if full is None:
                        update_log.warning("增量包不适用于当前版本，且清单未提供完整包")
                        if manual:
                            self._feedback("当前版本不支持增量更新，请下载完整安装包", True)
                        return
                    update_log.info("增量包不适用于 v%s，自动改用完整包", result.current_version)
                    manifest = full
            version = str(manifest.version)
            update_log.info("发现新版本 v%s（%s）", version, manifest.update_type)
            if not manual and version in self._prompted:
                return
            self._prompted.add(version)
            if manual:
                self._feedback("发现新版本 v%s" % version)
            self._manifest = manifest
            self._show_dialog(manifest, result.current_version)

        # -- 弹窗 -----------------------------------------------------------
        def _make_dialog(self, manifest: UpdateManifest, current_version: str,
                         on_install=None, on_cancel=None):
            from src.ui.update_dialog import UpdateDialog

            return UpdateDialog(manifest, current_version,
                                on_install=on_install or self._start_download,
                                on_cancel=on_cancel or self.cancel_download,
                                parent=self.parent())

        def _show_dialog(self, manifest: UpdateManifest, current_version: str) -> None:
            if self._dialog is not None:
                self._dialog.close()
            dialog = self._dialog_factory(manifest, current_version,
                                          on_install=self._start_download,
                                          on_cancel=self.cancel_download)
            self._dialog = dialog
            dialog.finished.connect(lambda *_: self._on_dialog_closed(dialog))
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        def _on_dialog_closed(self, dialog) -> None:
            if self._dialog is dialog:
                self._dialog = None
            if self._downloading:
                self.cancel_download()

        # -- 下载 -----------------------------------------------------------
        def _start_download(self) -> None:
            if self._downloading or self._manifest is None:
                return
            self._downloading = True
            self._cancel.clear()
            manifest = self._manifest
            dest = paths.user_data_dir() / "updates"
            timeout = float(self.settings.update.timeout_seconds)

            def on_progress(progress: DownloadProgress) -> None:
                with self._lock:
                    self._progress = progress

            def worker() -> None:
                try:
                    path = self._download_fn(manifest, dest, on_progress=on_progress,
                                             cancel=self._cancel, timeout=timeout)
                    payload = ("done", path)
                except DownloadError as exc:
                    fallback = manifest.as_full_package()
                    if (manifest.update_type == "incremental"
                            and exc.fallback_allowed and fallback is not None):
                        update_log.warning(
                            "增量包不可用，回退完整包：reason=%s asset=%s",
                            exc, Path(fallback.package_url.split("?", 1)[0]).name)
                        try:
                            path = self._download_fn(
                                fallback, dest, on_progress=on_progress,
                                cancel=self._cancel, timeout=timeout)
                            with self._lock:
                                self._manifest = fallback
                            payload = ("done", path)
                        except Exception as fallback_exc:  # noqa: BLE001
                            payload = ("error", "增量包失败（%s）；完整包回退失败：%s"
                                       % (exc, fallback_exc))
                    else:
                        payload = ("error", str(exc))
                except Exception as exc:  # noqa: BLE001
                    payload = ("error", str(exc))
                with self._lock:
                    self._download_result = payload

            update_log.info("开始下载更新包 v%s", manifest.version)
            update_log.info(
                "下载选择 type=%s asset=%s expected_bytes=%s from_versions=%s",
                manifest.update_type,
                Path(manifest.package_url.split("?", 1)[0]).name,
                manifest.package_size, manifest.from_versions)
            threading.Thread(target=worker, name="update-download", daemon=True).start()
            self._timer.start()

        def cancel_download(self) -> None:
            self._cancel.set()

        def _handle_download(self, payload) -> None:
            status, value = payload
            self._downloading = False
            if status == "error":
                message = str(value)
                update_log.warning("更新下载失败: %s", message)
                if self._dialog is not None:
                    if self._cancel.is_set():
                        self._dialog.set_cancelled()
                    else:
                        self._dialog.show_download_error(message)
                self._feedback("更新下载失败：%s" % message, True)
                return

            package: Path = Path(value)
            if self._dialog is not None:
                self._dialog.set_installing()
            try:
                self._prepare_and_launch(package)
            except Exception as exc:  # noqa: BLE001
                update_log.error("启动更新器失败: %s", exc)
                if self._dialog is not None:
                    self._dialog.show_download_error("无法启动更新器：%s" % exc)
                self._feedback("无法启动更新器：%s" % exc, True)
                return
            QtCore.QTimer.singleShot(600, self._on_quit)

        # -- 交给 Updater.exe ------------------------------------------------
        def install_dir(self) -> Path:
            if getattr(sys, "frozen", False):
                return Path(sys.executable).resolve().parent
            return paths.project_root()

        def updater_path(self) -> Path:
            return self.install_dir() / UPDATER_EXE_NAME

        def _prepare_and_launch(self, package: Path) -> None:
            assert self._manifest is not None
            updater = self.updater_path()
            if not updater.exists():
                raise RuntimeError("缺少更新组件 %s（请重新下载完整安装包）"
                                   % UPDATER_EXE_NAME)
            exe_name = Path(sys.executable).name if getattr(sys, "frozen", False) \
                else "LOL海克斯助手.exe"
            job = UpdateJob(
                version=str(self._manifest.version),
                from_version=self.current_version,
                package_path=str(package),
                sha256=self._manifest.sha256,
                install_dir=str(self.install_dir()),
                exe_name=exe_name,
                pid=os.getpid(),
                package_size=self._manifest.package_size,
                startup_token=secrets.token_hex(16),
                work_dir=str(paths.user_data_dir() / "updates"),
                log_path=str(paths.user_data_dir() / "logs" / "update.log"),
            )
            job_path = paths.user_data_dir() / "updates" / "update-job.json"
            write_job(job_path, job)
            update_log.info("启动更新器: %s（job=%s）", updater, job_path)
            self._updater_process = self._launch_updater(
                updater, job_path, startup_token=job.startup_token)

        def _spawn_updater(self, updater: Path, job_path: Path,
                           startup_token: str = "") -> object:
            # 关键：不能直接运行安装目录里的 Updater.exe。更新包会替换它，
            # 从原路径运行会导致 Windows WinError 32（文件被自身占用）。
            runtime = paths.user_data_dir() / "updates" / (
                "Updater-runtime-%s.exe" % os.getpid())
            runtime.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(updater, runtime)
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = subprocess.Popen(
                [str(runtime), "--job", str(job_path)],
                cwd=str(runtime.parent), close_fds=True, creationflags=flags)
            # 只等待很短时间确认进程仍在运行；Updater 会继续等待主程序退出。
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                code = process.poll()
                if code is not None:
                    raise RuntimeError(
                        "更新器启动后立即退出（code=%s，日志见 update.log）" % code)
                time.sleep(0.05)
            update_log.info("更新器已启动并从临时路径运行: %s (pid=%s)",
                            runtime, process.pid)
            return process

        # -- 生命周期 -------------------------------------------------------
        def shutdown(self) -> None:
            self._shutdown = True
            self._cancel.set()
            self._timer.stop()
            if self._dialog is not None:
                try:
                    self._dialog.close()
                except Exception:  # noqa: BLE001
                    pass
                self._dialog = None

else:  # pragma: no cover

    class UpdateController:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["UpdateController", "STARTUP_CHECK_DELAY_MS", "UPDATER_EXE_NAME"]
