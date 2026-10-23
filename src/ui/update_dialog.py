"""更新提示 / 下载进度对话框（与主程序一致的简洁 PySide6 风格）。"""

from __future__ import annotations

from typing import Callable, Optional

from src.update.downloader import DownloadProgress
from src.update.manifest import UpdateManifest, format_size
from src.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False


def update_type_text(manifest: UpdateManifest) -> str:
    return "增量更新（只下载变更文件）" if manifest.update_type == "incremental" \
        else "完整更新包"


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class ReleaseNotesDialog(QtWidgets.QDialog):
        """完整更新日志。"""

        def __init__(self, version: str, notes: list[str], parent=None) -> None:
            super().__init__(parent)
            self.setWindowTitle("更新日志 - v%s" % version)
            self.resize(520, 360)
            layout = QtWidgets.QVBoxLayout(self)
            browser = QtWidgets.QTextBrowser()
            browser.setPlainText("\n".join("• " + line for line in notes) or "（无更新说明）")
            layout.addWidget(browser)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.accept)
            buttons.clicked.connect(lambda *_: self.accept())
            layout.addWidget(buttons)
            self.browser = browser

    class UpdateDialog(QtWidgets.QDialog):
        """发现新版本 / 下载进度 / 下载失败重试。"""

        def __init__(self, manifest: UpdateManifest, current_version: str,
                     on_install: Optional[Callable[[], None]] = None,
                     on_cancel: Optional[Callable[[], None]] = None,
                     parent=None) -> None:
            super().__init__(parent)
            self.manifest = manifest
            self.current_version = current_version
            self._on_install = on_install
            self._on_cancel = on_cancel
            self._notes_dialog = None
            self._downloading = False

            self.setWindowTitle("发现新版本")
            self.setMinimumWidth(440)
            layout = QtWidgets.QVBoxLayout(self)

            title = QtWidgets.QLabel("LOL 海克斯助手发现新版本")
            title.setStyleSheet("font-size:15px;font-weight:bold;")
            layout.addWidget(title)

            form = QtWidgets.QFormLayout()
            self.current_label = QtWidgets.QLabel("v%s" % current_version)
            self.latest_label = QtWidgets.QLabel("v%s" % manifest.version)
            self.latest_label.setStyleSheet("font-weight:bold;color:#3f8f4f;")
            self.size_label = QtWidgets.QLabel(format_size(manifest.package_size))
            self.mode_label = QtWidgets.QLabel(update_type_text(manifest))
            form.addRow("当前版本：", self.current_label)
            form.addRow("最新版本：", self.latest_label)
            form.addRow("更新包大小：", self.size_label)
            form.addRow("更新方式：", self.mode_label)
            layout.addLayout(form)

            layout.addWidget(QtWidgets.QLabel("更新内容："))
            self.notes = QtWidgets.QTextBrowser()
            self.notes.setMaximumHeight(150)
            self.notes.setPlainText("\n".join("• " + line
                                              for line in manifest.release_notes)
                                    or "（无更新说明）")
            layout.addWidget(self.notes)

            self.status_label = QtWidgets.QLabel("")
            self.status_label.setStyleSheet("color:#8c96a6;font-size:12px;")
            layout.addWidget(self.status_label)

            self.progress = QtWidgets.QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.hide()
            layout.addWidget(self.progress)

            buttons = QtWidgets.QHBoxLayout()
            self.install_button = QtWidgets.QPushButton("立即更新")
            self.later_button = QtWidgets.QPushButton("稍后提醒")
            self.notes_button = QtWidgets.QPushButton("查看更新日志")
            self.cancel_button = QtWidgets.QPushButton("取消下载")
            self.cancel_button.hide()
            for button in (self.install_button, self.later_button,
                           self.notes_button, self.cancel_button):
                buttons.addWidget(button)
            layout.addLayout(buttons)

            self.install_button.clicked.connect(self._install)
            self.later_button.clicked.connect(self._later)
            self.notes_button.clicked.connect(self._show_notes)
            self.cancel_button.clicked.connect(self._cancel_download)

        # -- 按钮 -----------------------------------------------------------
        def _install(self) -> None:
            if self._on_install is not None:
                self.begin_download()
                self._on_install()

        def _later(self) -> None:
            if self._downloading:
                return
            self.reject()

        def _show_notes(self) -> None:
            if self._notes_dialog is None:
                self._notes_dialog = ReleaseNotesDialog(
                    str(self.manifest.version), self.manifest.release_notes, self)
            self._notes_dialog.show()
            self._notes_dialog.raise_()

        def _cancel_download(self) -> None:
            if self._on_cancel is not None:
                self._on_cancel()
            self.status_label.setText("正在取消…")

        # -- 状态 -----------------------------------------------------------
        def begin_download(self) -> None:
            self._downloading = True
            self.install_button.setEnabled(False)
            self.later_button.setEnabled(False)
            self.notes_button.setEnabled(False)
            self.cancel_button.show()
            self.progress.show()
            self.status_label.setText("正在下载更新包…")

        def set_progress(self, progress: DownloadProgress) -> None:
            total = progress.total or self.manifest.package_size
            if total:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(progress.percent or 0))
                self.status_label.setText(
                    "已下载 %s / %s（%d%%）"
                    % (format_size(progress.downloaded), format_size(total),
                       int(progress.percent or 0)))
            else:
                self.progress.setRange(0, 0)
                self.status_label.setText("已下载 %s" % format_size(progress.downloaded))

        def show_download_error(self, message: str) -> None:
            self._downloading = False
            self.progress.hide()
            self.cancel_button.hide()
            self.install_button.setText("重试下载")
            self.install_button.setEnabled(True)
            self.later_button.setEnabled(True)
            self.notes_button.setEnabled(True)
            self.status_label.setStyleSheet("color:#c0563f;font-size:12px;")
            self.status_label.setText("下载失败：%s" % message)

        def set_installing(self) -> None:
            self._downloading = True
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.cancel_button.hide()
            self.status_label.setStyleSheet("color:#3f8f4f;font-size:12px;")
            self.status_label.setText("下载完成，正在安装并重启…")

        def set_cancelled(self) -> None:
            self._downloading = False
            self.progress.hide()
            self.cancel_button.hide()
            self.install_button.setText("立即更新")
            self.install_button.setEnabled(True)
            self.later_button.setEnabled(True)
            self.notes_button.setEnabled(True)
            self.status_label.setStyleSheet("color:#8c96a6;font-size:12px;")
            self.status_label.setText("已取消下载")

else:  # pragma: no cover

    class ReleaseNotesDialog:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 is not installed.")

    class UpdateDialog:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["UpdateDialog", "ReleaseNotesDialog", "update_type_text"]
