# -*- mode: python ; coding: utf-8 -*-
"""更新辅助器单独打包（onefile，独立运行时，便于替换主程序文件）。

    pyinstaller packaging/updater.spec --noconfirm

产物：dist_updater/Updater.exe —— 由发布脚本复制进主程序目录。
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = [
    "src.update.updater",
    "src.update.installer",
    "src.update.job",
    "src.update.checksum",
    "src.utils.paths",
    "src.utils.logger",
    "psutil",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "packaging", "updater_launcher.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6", "cv2", "onnxruntime", "numpy", "rapidocr_onnxruntime",
        "httpx", "PIL", "yaml", "pydantic", "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
