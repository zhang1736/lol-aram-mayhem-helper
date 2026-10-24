# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包配置。

    pyinstaller packaging/LOL海克斯助手.spec --noconfirm

产物：dist/LOL海克斯助手/
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(PROJECT_ROOT, "data", "kiwi_augments.json"), "data"),
    (os.path.join(PROJECT_ROOT, "data", "augment_aliases.json"), "data"),
    (os.path.join(PROJECT_ROOT, "config", "config.yaml"), "config"),
    (os.path.join(PROJECT_ROOT, ".python-version"), "."),
    # OP.GG 的 TLS 校验用导出的 Windows CA bundle；打包版必须带上，
    # 否则会退回 httpx 自带的 certifi（本机实测不被信任 → ConnectError）
    (os.path.join(PROJECT_ROOT, "certs", "windows_ca_bundle.pem"), "certs"),
]

# 先用 updater.spec 生成 dist_updater/Updater.exe，再构建主程序。
# 该副本位于 _internal/Updater.exe，供旧版安装首次升级后自更新根目录的
# Updater.exe；根目录旧更新器在首次升级时不能被自身替换。
_staged_updater = os.path.join(PROJECT_ROOT, "dist_updater", "Updater.exe")
if os.path.exists(_staged_updater):
    datas.append((_staged_updater, "."))

# RapidOCR 的 ONNX 模型必须一起打包，否则第一次启动会 FileNotFoundError
datas += collect_data_files("rapidocr_onnxruntime")

# PySide6 只需要 Widgets 相关部分：PyInstaller 自带的 hook 会收集 Qt6Core/Gui/Widgets
# 及其依赖（含 Qt 插件）。实测不需要 collect_all（那会把 QML/Quick/翻译等一起带上，
# 体积从 ~400MB 涨到 900MB+）。
binaries = []
hiddenimports_extra = []

# Qt6Core.dll 依赖 Windows 自带的 ICU（icuuc.dll / icuin.dll / icu.dll）。
# 非打包环境下从 System32 能找到，打包后必须显式带上，
# 否则 PySide6.QtCore 报 "DLL load failed: 找不到指定的程序"。
_system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
for _dll in ("icuuc.dll", "icuin.dll", "icu.dll"):
    _path = os.path.join(_system32, _dll)
    if os.path.exists(_path):
        binaries.append((_path, "PySide6"))

hiddenimports = [
    "rapidocr_onnxruntime",
    "onnxruntime",
    "psutil",
    "yaml",
    "pydantic",
    "httpx",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
] + hiddenimports_extra + collect_submodules("src")

a = Analysis(
    [os.path.join(PROJECT_ROOT, "packaging", "launcher.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[os.path.join(PROJECT_ROOT, "packaging", "rthook_pyside6.py")],
    excludes=[
        "matplotlib", "tkinter", "PyQt5", "PyQt6", "PySide2",
        "notebook", "IPython", "pandas", "scipy", "torch", "tensorflow",
    ],
    noarchive=False,
)

# --- 发布包体积精简（2026-10-23）-------------------------------------------
# 只保留实际用到的部分：
#   * cv2 的 videoio ffmpeg 解码器（29MB）：只用于视频读写，OCR 只用
#     imdecode/resize/cvtColor 等图像函数
#   * Qt Qml / Quick / QuickWidgets / Pdf：本项目只用 Widgets
# 每次精简后都验证：启动、Overlay 预览、OCR 识别、托盘、OP.GG 请求、SQLite。
_DROP_TOKENS = (
    "opencv_videoio_ffmpeg",
    "qt6qml", "qt6quick", "qt6pdf", "qml",
)


def _keep(name: str) -> bool:
    lowered = str(name).replace("\\", "/").lower()
    return not any(token in lowered for token in _DROP_TOKENS)


a.binaries = [entry for entry in a.binaries if _keep(entry[0])]
a.datas = [entry for entry in a.datas if _keep(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LOL海克斯助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LOL海克斯助手",
)
