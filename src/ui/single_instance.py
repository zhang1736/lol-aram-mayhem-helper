"""单实例保护（Windows 命名互斥体，无需第三方依赖）。"""

from __future__ import annotations

import ctypes
import sys

MUTEX_NAME = "Global\\LOLHexHelper_SingleInstance"
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """第二次启动时 acquire() 返回 False。"""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":      # pragma: no cover
            return True
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return True                   # 拿不到互斥体时不阻塞启动
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


__all__ = ["SingleInstance", "MUTEX_NAME"]
