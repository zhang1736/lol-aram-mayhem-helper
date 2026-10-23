"""打包入口：双击 exe 即进入常驻模式（带托盘 GUI）。

参数写死为正式默认值，用户不需要命令行。
"""

from __future__ import annotations

import sys

from src.app import run

RESIDENT_ARGS = [
    "--resident",
    "--wait-client", "600",
    "--wait-game", "600",
    "--champ-timeout", "600",
    "--scan-interval", "0.25",
]


def main() -> int:
    if "--selftest" in sys.argv:
        from src.diagnose import run_selftest

        return run_selftest()
    return run(RESIDENT_ARGS)


if __name__ == "__main__":
    sys.exit(main())
