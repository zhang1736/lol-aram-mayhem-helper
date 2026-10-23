"""PyInstaller 运行时钩子：在任何 PySide6 导入之前把 Qt 的 DLL 目录
加入 DLL 搜索路径。

实测：不打这个钩子时 PySide6.QtCore 会报
    ImportError: DLL load failed while importing QtCore: 找不到指定的程序
（Qt6Core.dll 能被找到，但其依赖没有按正确顺序解析）。
"""

import os
import sys


def _add_dll_dirs() -> None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    candidates = [
        os.path.join(base, "PySide6"),
        os.path.join(base, "shiboken6"),
        base,
    ]
    for path in candidates:
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)
            except (AttributeError, OSError):
                pass


_add_dll_dirs()
