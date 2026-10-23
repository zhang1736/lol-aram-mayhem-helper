"""Updater.exe 入口（由主程序自动启动，用户无需手动运行）。"""

from __future__ import annotations

import sys

from src.update.updater import main

if __name__ == "__main__":
    sys.exit(main())
