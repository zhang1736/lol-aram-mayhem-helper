"""设置对话框：分「数据 / 悬浮窗 / 调试」三段，保存或取消。

修复记录：原先用 QtWidgets.Qt.Horizontal（PySide6 里不存在），
构造函数抛 AttributeError 被 Qt 吞掉，表现成"点设置没反应"。
"""

from __future__ import annotations

from typing import Callable, Optional

from src.config import Settings, save_user_settings
from src.ui.texts import region_text, tier_text
from src.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class SettingsDialog(QtWidgets.QDialog):
        """保存后立即写入 config/user_config.yaml。"""

        def __init__(self, settings: Settings, parent=None,
                     on_applied: Optional[Callable[[Settings], None]] = None) -> None:
            super().__init__(parent)
            self.settings = settings
            self._on_applied = on_applied
            self.setWindowTitle("LOL 海克斯助手 - 设置")
            self.setMinimumWidth(380)

            layout = QtWidgets.QVBoxLayout(self)
            layout.addWidget(_section("数据"))

            data_form = QtWidgets.QFormLayout()
            self.region_value = QtWidgets.QLabel(region_text(settings.data.region))
            self.tier_value = QtWidgets.QLabel(tier_text(settings.data.tier))
            data_form.addRow("地区：", self.region_value)
            data_form.addRow("段位：", self.tier_value)
            note = QtWidgets.QLabel("（统计口径固定，暂不支持修改）")
            note.setStyleSheet("color:#8c96a6;font-size:11px;")
            data_form.addRow("", note)

            self.ttl_box = QtWidgets.QSpinBox()
            self.ttl_box.setRange(1, 168)
            self.ttl_box.setSuffix(" 小时")
            self.ttl_box.setValue(int(settings.cache.opgg_ttl_hours))
            data_form.addRow("缓存时间：", self.ttl_box)
            layout.addLayout(data_form)

            layout.addWidget(_section("Overlay"))
            overlay_form = QtWidgets.QFormLayout()
            self.overlay_box = QtWidgets.QCheckBox()
            self.overlay_box.setChecked(settings.overlay.enabled)
            overlay_form.addRow("启用悬浮窗：", self.overlay_box)

            self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.opacity_slider.setRange(20, 100)
            self.opacity_slider.setValue(settings.overlay.opacity)
            self.opacity_value = QtWidgets.QLabel("%d%%" % settings.overlay.opacity)
            self.opacity_slider.valueChanged.connect(
                lambda v: self.opacity_value.setText("%d%%" % v))
            opacity_row = QtWidgets.QHBoxLayout()
            opacity_row.addWidget(self.opacity_slider)
            opacity_row.addWidget(self.opacity_value)
            overlay_form.addRow("透明度：", opacity_row)

            self.offset_x_box = QtWidgets.QSpinBox()
            self.offset_x_box.setRange(-2000, 2000)
            self.offset_x_box.setValue(settings.overlay.offset_x)
            overlay_form.addRow("X 偏移：", self.offset_x_box)

            self.offset_y_box = QtWidgets.QSpinBox()
            self.offset_y_box.setRange(-2000, 2000)
            self.offset_y_box.setValue(settings.overlay.offset_y)
            overlay_form.addRow("Y 偏移：", self.offset_y_box)
            layout.addLayout(overlay_form)

            layout.addWidget(_section("调试"))
            debug_form = QtWidgets.QFormLayout()
            self.debug_box = QtWidgets.QCheckBox()
            self.debug_box.setChecked(settings.debug.enabled)
            debug_form.addRow("Debug：", self.debug_box)
            layout.addLayout(debug_form)

            buttons = QtWidgets.QDialogButtonBox()
            self.save_button = buttons.addButton("保存",
                                                 QtWidgets.QDialogButtonBox.AcceptRole)
            self.cancel_button = buttons.addButton("取消",
                                                   QtWidgets.QDialogButtonBox.RejectRole)
            self.save_button.clicked.connect(self._apply)
            self.cancel_button.clicked.connect(self.reject)
            layout.addWidget(buttons)

        # -- 保存 / 取消 ----------------------------------------------------
        def _apply(self) -> None:
            """只写用户配置；地区/段位不在此处修改。"""
            self.settings.cache.opgg_ttl_hours = float(self.ttl_box.value())
            self.settings.overlay.enabled = self.overlay_box.isChecked()
            self.settings.overlay.opacity = int(self.opacity_slider.value())
            self.settings.overlay.offset_x = int(self.offset_x_box.value())
            self.settings.overlay.offset_y = int(self.offset_y_box.value())
            self.settings.debug.enabled = self.debug_box.isChecked()
            try:
                path = save_user_settings(self.settings)
                logger.info("设置已保存: %s", path)
            except OSError as exc:
                logger.warning("保存设置失败: %s", exc)
                QtWidgets.QMessageBox.warning(self, "设置", "保存失败：%s" % exc)
                return
            if self._on_applied is not None:
                self._on_applied(self.settings)
            self.accept()

    def _section(text: str):
        label = QtWidgets.QLabel("──────── %s ────────" % text)
        label.setStyleSheet("color:#8c96a6;margin-top:8px;")
        return label

else:  # pragma: no cover

    class SettingsDialog:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["SettingsDialog"]
